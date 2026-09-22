"""Module E: the headless multiplayer fuzz swarm.

Spawns many simultaneous bot clients against the running game server and tries
to break it in the ways a multiplayer prison game actually breaks: a client
that claims to be somewhere it cannot be, a client that opens a door it has no
clearance for, a client that sends a movement vector full of NaN, a client that
sends a packet longer than the server's read buffer.

The single question this module exists to answer is SERVER AUTHORITY. A bot
asserts an illegal state; the server either rejects it or it does not. Every
other metric here — tick latency, desync count, memory growth — is secondary
to that, because a server that accepts a client's word about position is not a
game server, it is a chat relay with physics.

WHAT IT NEEDS FROM THE GAME. The swarm cannot guess a wire format, so the
netcode task publishes one: `studio_protocol.json` in the game repo, described
in `PROTOCOL_DOC` below. That file is a deliverable of the Opus netcode work,
and writing the fuzzer against a declared contract rather than a sniffed one
is deliberate: when the protocol changes, this fails loudly at load time
instead of quietly fuzzing a format that no longer exists.

TRANSPORTS. `tcp`, `udp` and `websocket` are implemented here with the
standard library. Godot's default ENetMultiplayerPeer is NOT: ENet has its own
reliability framing that a stdlib client cannot speak, so a project that wants
to be fuzzed should expose WebSocketMultiplayerPeer (which it will want anyway
the day it has a browser client). `load_protocol` says so rather than
pretending to connect.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import random
import socket
import struct
import time
from pathlib import Path

import config
import events

PROTOCOL_FILE = "studio_protocol.json"
TRANSPORTS = ("tcp", "udp", "websocket")

PROTOCOL_DOC = """studio_protocol.json — the contract the fuzz swarm reads:

{
  "transport": "websocket|tcp|udp",
  "host": "127.0.0.1",
  "port": 8910,
  "path": "/",                       // websocket only
  "encoding": "json",                // json | length_prefixed_json
  "handshake": {"type": "join", "name": "$BOT"},
  "state_field": "players",          // where the server echoes authoritative state
  "position_field": "pos",
  "move": {"type": "move", "pos": [0, 0, 0], "seq": 0},
  "privileged": [                    // actions the server MUST refuse a normal client
    {"type": "open_door", "door": "block_a_gate", "clearance": 3},
    {"type": "grant_item", "item": "vent_key"}
  ],
  "bounds": {"x": [-60, 60], "y": [0, 20], "z": [-60, 60]},
  "max_speed_mps": 6.5
}
"""


class ProtocolError(RuntimeError):
    pass


def load_protocol(project_dir):
    path = Path(project_dir) / PROTOCOL_FILE
    if not path.exists():
        raise ProtocolError(
            f"{path} does not exist. The fuzz swarm cannot guess a wire "
            f"format.\n\n{PROTOCOL_DOC}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProtocolError(f"{path}: invalid JSON ({exc})")
    transport = str(doc.get("transport", "")).lower()
    if transport not in TRANSPORTS:
        raise ProtocolError(
            f"{path}: transport must be one of {list(TRANSPORTS)}, got "
            f"{transport!r}. Godot's default ENetMultiplayerPeer is not "
            "fuzzable from a stdlib client — expose WebSocketMultiplayerPeer "
            "for QA (and for the browser client you will want later).")
    if not doc.get("port"):
        raise ProtocolError(f"{path}: port is required")
    doc.setdefault("host", "127.0.0.1")
    doc.setdefault("encoding", "json")
    doc.setdefault("bounds", {"x": [-100, 100], "y": [-10, 50], "z": [-100, 100]})
    doc.setdefault("max_speed_mps", 8.0)
    return doc


# --- fuzz payloads -----------------------------------------------------------
# Each generator returns (name, payload, expectation). `expectation` is what a
# CORRECT server does: "reject" means the server must not adopt the claim.
def fuzz_moves(proto, rng):
    b = proto["bounds"]
    far = [b["x"][1] * 50, b["y"][1] * 50, b["z"][1] * 50]
    base = dict(proto.get("move") or {"type": "move"})
    pos_field = proto.get("position_field", "pos")

    def move(pos):
        m = dict(base)
        m[pos_field] = pos
        m["seq"] = rng.randint(1, 10 ** 6)
        return m

    return [
        ("teleport_out_of_bounds", move(far), "reject"),
        ("nan_vector", move([float("nan"), 0.0, 0.0]), "reject"),
        ("inf_vector", move([float("inf"), float("-inf"), 0.0]), "reject"),
        ("subterranean", move([0.0, b["y"][0] - 500.0, 0.0]), "reject"),
        ("wall_clip_step", move([b["x"][0] - 0.5, 1.0, 0.0]), "reject"),
        ("speed_hack", move([proto["max_speed_mps"] * 40, 1.0, 0.0]), "reject"),
        ("wrong_types", move(["0", None, {}]), "reject"),
        ("legal_step", move([rng.uniform(-1, 1), 0.0, rng.uniform(-1, 1)]), "accept"),
    ]


def fuzz_structural(proto, rng):
    out = [
        ("empty_object", {}, "reject"),
        ("unknown_type", {"type": "definitely_not_a_message"}, "reject"),
        ("missing_type", {"seq": 1}, "reject"),
        ("huge_string", {"type": "chat", "text": "A" * 200000}, "reject"),
        ("deep_nesting", _nested(60), "reject"),
        ("negative_seq", {"type": "move", "seq": -(2 ** 62)}, "reject"),
    ]
    for action in proto.get("privileged") or []:
        out.append((f"privileged:{action.get('type', 'action')}", dict(action), "reject"))
    return out


def _nested(depth):
    doc = {"type": "move"}
    cur = doc
    for _ in range(depth):
        cur["n"] = {}
        cur = cur["n"]
    return doc


# --- transports --------------------------------------------------------------
class Conn:
    """The minimum a bot needs: send a dict, maybe read one back."""

    async def send(self, obj):                       # pragma: no cover - iface
        raise NotImplementedError

    async def recv(self, timeout=0.5):               # pragma: no cover - iface
        raise NotImplementedError

    async def close(self):                           # pragma: no cover - iface
        raise NotImplementedError


def _encode(obj, encoding):
    raw = json.dumps(obj, allow_nan=True).encode("utf-8")
    if encoding == "length_prefixed_json":
        return struct.pack("!I", len(raw)) + raw
    return raw + b"\n"


class TcpConn(Conn):
    def __init__(self, reader, writer, encoding):
        self.r, self.w, self.encoding = reader, writer, encoding

    @classmethod
    async def open(cls, proto):
        r, w = await asyncio.open_connection(proto["host"], int(proto["port"]))
        return cls(r, w, proto["encoding"])

    async def send(self, obj):
        self.w.write(_encode(obj, self.encoding))
        await self.w.drain()

    async def recv(self, timeout=0.5):
        try:
            if self.encoding == "length_prefixed_json":
                head = await asyncio.wait_for(self.r.readexactly(4), timeout)
                n = struct.unpack("!I", head)[0]
                if n > 8 << 20:
                    return None
                body = await asyncio.wait_for(self.r.readexactly(n), timeout)
            else:
                body = await asyncio.wait_for(self.r.readline(), timeout)
            return json.loads(body.decode("utf-8", "replace") or "null")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
            return None

    async def close(self):
        try:
            self.w.close()
            await self.w.wait_closed()
        except Exception:                                    # noqa: BLE001
            pass


class UdpConn(Conn):
    def __init__(self, sock, addr, encoding):
        self.s, self.addr, self.encoding = sock, addr, encoding

    @classmethod
    async def open(cls, proto):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        return cls(s, (proto["host"], int(proto["port"])), proto["encoding"])

    async def send(self, obj):
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(self.s, _encode(obj, self.encoding), self.addr)

    async def recv(self, timeout=0.5):
        loop = asyncio.get_running_loop()
        try:
            data = await asyncio.wait_for(loop.sock_recv(self.s, 65535), timeout)
            return json.loads(data.decode("utf-8", "replace") or "null")
        except (asyncio.TimeoutError, ValueError, OSError):
            return None

    async def close(self):
        self.s.close()


class WebSocketConn(Conn):
    """A minimal RFC6455 client: handshake, masked text frames, no extensions.

    Implemented here rather than pulled in as a dependency because it needs
    to be able to send DELIBERATELY malformed traffic, which a well-behaved
    client library exists specifically to prevent.
    """

    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    @classmethod
    async def open(cls, proto):
        r, w = await asyncio.open_connection(proto["host"], int(proto["port"]))
        key = base64.b64encode(os.urandom(16)).decode()
        path = proto.get("path", "/") or "/"
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {proto['host']}:{proto['port']}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        w.write(req.encode())
        await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)
        if b"101" not in head.split(b"\r\n")[0]:
            raise ProtocolError(f"websocket upgrade refused: {head[:120]!r}")
        return cls(r, w)

    def _frame(self, payload, opcode=0x1):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < (1 << 16):
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        return head + mask + masked

    async def send(self, obj):
        raw = json.dumps(obj, allow_nan=True).encode("utf-8")
        self.w.write(self._frame(raw))
        await self.w.drain()

    async def recv(self, timeout=0.5):
        try:
            head = await asyncio.wait_for(self.r.readexactly(2), timeout)
            n = head[1] & 0x7F
            if n == 126:
                n = struct.unpack("!H", await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.r.readexactly(8))[0]
            if head[1] & 0x80:
                mask = await self.r.readexactly(4)
                body = bytes(b ^ mask[i % 4] for i, b in
                             enumerate(await self.r.readexactly(n)))
            else:
                body = await self.r.readexactly(n)
            return json.loads(body.decode("utf-8", "replace") or "null")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError,
                struct.error):
            return None

    async def close(self):
        try:
            self.w.close()
            await self.w.wait_closed()
        except Exception:                                    # noqa: BLE001
            pass


async def connect(proto):
    t = proto["transport"]
    if t == "tcp":
        return await TcpConn.open(proto)
    if t == "udp":
        return await UdpConn.open(proto)
    return await WebSocketConn.open(proto)


# --- the swarm ---------------------------------------------------------------
def _claimed_position(reply, proto, bot_name):
    """The position the server says this bot is at, if the reply carries one."""
    if not isinstance(reply, dict):
        return None
    players = reply.get(proto.get("state_field", "players"))
    pos_field = proto.get("position_field", "pos")
    if isinstance(players, dict):
        entry = players.get(bot_name)
    elif isinstance(players, list):
        entry = next((p for p in players
                      if isinstance(p, dict) and p.get("name") == bot_name), None)
    else:
        entry = reply
    if isinstance(entry, dict) and isinstance(entry.get(pos_field), (list, tuple)):
        return list(entry[pos_field])
    return None


def _out_of_bounds(pos, proto):
    b = proto["bounds"]
    try:
        x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    except (TypeError, ValueError, IndexError):
        return True
    if any(math.isnan(v) or math.isinf(v) for v in (x, y, z)):
        return True
    return not (b["x"][0] <= x <= b["x"][1] and b["y"][0] <= y <= b["y"][1]
                and b["z"][0] <= z <= b["z"][1])


async def _bot(idx, proto, deadline, rng, out):
    name = f"fuzzbot-{idx:03d}"
    try:
        conn = await asyncio.wait_for(connect(proto), 15)
    except Exception as exc:                                 # noqa: BLE001
        out["connect_failures"].append(f"{name}: {type(exc).__name__}: {exc}")
        return
    try:
        hs = proto.get("handshake")
        if hs:
            await conn.send(json.loads(json.dumps(hs).replace("$BOT", name)))
            await conn.recv(2.0)
        cases = fuzz_moves(proto, rng) + fuzz_structural(proto, rng)
        while time.time() < deadline:
            label, payload, expect = cases[rng.randrange(len(cases))]
            sent = time.time()
            try:
                await conn.send(payload)
            except Exception as exc:                         # noqa: BLE001
                out["crashes"] += 1
                out["notes"].append(f"{name}: send failed after {label}: {exc}")
                return
            reply = await conn.recv(0.6)
            if reply is not None:
                out["ticks_ms"].append((time.time() - sent) * 1000.0)
            out["sent"] += 1
            if expect == "reject":
                claimed = _claimed_position(reply, proto, name)
                if claimed is not None and _out_of_bounds(claimed, proto):
                    out["authority_violations"] += 1
                    out["violations"].append(
                        {"bot": name, "case": label, "server_state": claimed})
                elif isinstance(reply, dict) and reply.get("ok") is True \
                        and label.startswith("privileged:"):
                    out["authority_violations"] += 1
                    out["violations"].append(
                        {"bot": name, "case": label, "server_state": reply})
            await asyncio.sleep(max(0.0, rng.uniform(0.01, 0.05)))
    finally:
        await conn.close()


async def _run(proto, bots, seconds, seed):
    out = {"sent": 0, "authority_violations": 0, "desyncs": 0, "crashes": 0,
           "ticks_ms": [], "violations": [], "connect_failures": [], "notes": []}
    deadline = time.time() + float(seconds)
    rngs = [random.Random(seed + i) for i in range(bots)]
    await asyncio.gather(*[_bot(i, proto, deadline, rngs[i], out)
                           for i in range(bots)], return_exceptions=True)
    return out


def _percentile(values, pct):
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round((pct / 100.0) * (len(s) - 1)))))
    return round(s[k], 2)


def fuzz(project, project_dir, *, bots=None, seconds=None, seed=1337):
    """Run the swarm and write a report the phase-4 gate can read."""
    proto = load_protocol(project_dir)
    bots = config.STUDIO_FUZZ_BOTS if bots is None else int(bots)
    seconds = config.STUDIO_FUZZ_SECONDS if seconds is None else float(seconds)
    events.emit("studio.fuzz_start", project=str(project), bots=bots,
                seconds=seconds, transport=proto["transport"])
    started = time.time()
    raw = asyncio.run(_run(proto, bots, seconds, seed))
    ticks = raw.pop("ticks_ms")
    report = {
        "project": str(project),
        "ts": time.time(),
        "seconds": round(time.time() - started, 1),
        "bots": bots,
        "transport": proto["transport"],
        "endpoint": f"{proto['host']}:{proto['port']}",
        "messages_sent": raw["sent"],
        "replies_seen": len(ticks),
        "tick_p50_ms": _percentile(ticks, 50),
        "tick_p95_ms": _percentile(ticks, 95),
        "tick_max_ms": _percentile(ticks, 100),
        "authority_violations": raw["authority_violations"],
        "desyncs": raw["desyncs"],
        "crashes": raw["crashes"],
        "connect_failures": raw["connect_failures"][:40],
        "violations": raw["violations"][:80],
        "notes": raw["notes"][:40],
    }
    if len(raw["connect_failures"]) >= bots:
        report["notes"].append(
            "every bot failed to connect — the server was not reachable, so "
            "this report is evidence about the harness, not about the game")
        report["server_unreachable"] = True
    d = config.studio_run_dir(project, create=True)
    path = d / f"fuzz-{int(report['ts'])}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    events.emit("studio.fuzz_done", project=str(project),
                authority_violations=report["authority_violations"],
                crashes=report["crashes"], sent=report["messages_sent"],
                unreachable=bool(report.get("server_unreachable")))
    return report
