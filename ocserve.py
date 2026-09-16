"""Client for the persistent `opencode serve` HTTP/SSE server.

Today every OpencodeDriver run spawns a fresh one-shot `opencode run` process
(drivers.py). `opencode serve` is the same binary in server mode: one process
per orchestrator serves many sessions over HTTP, prompts are asynchronous, and
results arrive on a server-sent event stream. This module is the lifecycle +
client layer a later task migrates the driver onto; it touches no driver code.

Wire protocol as MEASURED against opencode 1.18.29 (2026-09-16, `opencode
serve --port 4097`, probed with curl; the server's own `/doc` is the schema):

  * readiness       GET  /global/health -> 200. `opencode serve` also prints
                    "opencode server listening on http://127.0.0.1:<port>" on
                    stdout, which is NOT parsed: the health route states the
                    same fact without depending on log wording.
  * session create  POST /session, JSON `{}` -> 200 with the session object
                    (`{"id": "ses_...", "directory": "<the bound dir>", ...}`).
  * directory bind  the header is literally `x-opencode-directory`. A session
                    created with it comes back with `directory` set to that
                    path, so the HEADER — not a query parameter — is what binds
                    a session to a worktree. (`/session` also lists
                    `directory`/`workspace` query parameters; the header is the
                    mechanism encoded here, and the tests' fake server asserts
                    it actually arrives.)
  * prompt          POST /session/<id>/prompt_async -> 204, empty body. It
                    returns at once; the answer cannot be read off the
                    response. 404 = unknown session, 400 = malformed body.
  * stream          GET  /event?directory=<worktree> -> text/event-stream,
                    frames `data: {json}\n\n`, first frame `server.connected`.
                    Events are NOT per-session: every frame carries
                    `properties.sessionID` and this client filters on its own.
  * completion      a `session.idle` frame for that session (preceded by
                    `session.status` `{"status": {"type": "idle"}}`).
  * failure         `session.error` with `properties.error` shaped
                    `{"name": "<Type>", "data": {...}}` — observed live as
                    `{"name": "UnknownError", "data": {"message": "Model not
                    found: nope/nope. ..."}}`. An API error carries
                    `data.isRetryable` (`APIError` in `/doc`), which is what
                    the capacity classification keys on.
  * tokens          `message.part.updated` whose `part.type == "step-finish"`
                    carries `part.tokens` =
                    `{"total","input","output","reasoning","cache":{"read",
                    "write"}}` — the shape drivers.transcript_tokens folds.
  * dispose         POST /instance/dispose -> 200 `true`, instantly, and the
                    server stays up (verified: health 200 afterwards). It
                    releases the instance bound to the request's directory, so
                    it is sent WITH the same `x-opencode-directory`.

stdlib only (requirements.txt has just openai and python-dotenv): HTTP is
`http.client` driven from asyncio threads, and the SSE reader consumes the
response incrementally on its own daemon thread.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

import config

log = logging.getLogger(__name__)

READY_POLL = 0.25           # seconds between readiness probes
_HTTP_TIMEOUT = 30.0        # per-request budget for the short routes


class OcserveError(RuntimeError):
    """The server refused, or the stream ended without an answer."""


class CapacityFull(OcserveError):
    """The provider turned the request away for concurrency (isRetryable false).

    Same meaning as the capacity refusals the one-shot path classifies:
    expected weather, worth retrying later, not the task's fault.
    """


class StartupError(OcserveError):
    """`opencode serve` never became ready; its process has been cleaned up."""


# --------------------------------------------------------------------------
# server lifecycle
# --------------------------------------------------------------------------

class ServeHandle:
    """A running `opencode serve` process, reached over HTTP on loopback."""

    def __init__(self, base_url, proc, argv=(), log_path=None, log_fh=None):
        self.base_url = base_url
        self.proc = proc
        self.argv = list(argv)
        self.log_path = log_path
        self.log_fh = log_fh
        self._stop_lock = threading.Lock()
        self._stopped = False

    @property
    def stopped(self):
        return self._stopped

    def stop(self):
        """Kill the server process and its group. Idempotent: SIGTERM, SIGKILL."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            # It was started with start_new_session=True, so its process group
            # is its own: signalling the group reaches any worker the server
            # spawned, which signalling the leader alone would leave behind.
            pgid = None
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pass
            _signal_group(proc, pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _signal_group(proc, pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover - unkillable
                    log.warning("ocserve: server pid %s survived SIGKILL", proc.pid)
            if pgid is not None:
                # Whatever the leader left in its group goes too, or a dead run
                # leaks a listener on a loopback port (Rule 6 hygiene).
                try:
                    if pgid != os.getpgid(0):
                        os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
        if self.log_fh is not None:
            try:
                self.log_fh.close()
            except Exception:
                pass
            self.log_fh = None
        if self.log_path:
            _unlink(self.log_path)

    def __repr__(self):  # pragma: no cover - diagnostic
        return f"<ServeHandle {self.base_url} pid={getattr(self.proc, 'pid', None)}>"


def _signal_group(proc, pgid, sig):
    """Signal the child's process group, falling back to the process alone."""
    if pgid is not None:
        try:
            if pgid != os.getpgid(0):
                os.killpg(pgid, sig)
                return
        except OSError:
            return
    try:
        proc.send_signal(sig)
    except OSError:
        pass


def _free_port():
    """A bound-then-released :0 port. Racy in theory, one process in practice."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health_ok(base_url, timeout=2.0):
    try:
        status, _ = _request_sync("GET", base_url + "/global/health", timeout=timeout)
        return status == 200
    except Exception:
        return False


def _kill(proc):
    """Kill a process that never became a handle (startup failures)."""
    if proc is None or proc.poll() is not None:
        return
    pgid = None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pass
    _signal_group(proc, pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _signal_group(proc, pgid, signal.SIGKILL)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
    if pgid is not None:
        try:
            if pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


def _drain_brief(path, limit=400):
    """The last bytes the process wrote — startup diagnostics only.

    stdout/stderr go to a file rather than a pipe: a server that outlives the
    startup poll would eventually fill a 64 KiB unread pipe and block on its
    own logging.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            raw = fh.read()
    except OSError:
        return ""
    text = (raw or b"").decode("utf-8", "replace")
    return " ".join(text.split())[-limit:]


def start_server(binary=None, port=None, startup_timeout=None, cwd=None):
    """Start `opencode serve` on loopback and wait until it answers.

    Returns a ServeHandle. On timeout or startup failure the process is killed
    and StartupError raised — a failed start never leaves a server behind.
    """
    binary = binary or config.OPENCODE_SERVE_BIN
    if startup_timeout is None:
        startup_timeout = config.OPENCODE_SERVE_STARTUP_TIMEOUT
    port = port or _free_port()
    argv = [binary, "serve", "--port", str(port)]
    log.info("ocserve: starting %s", " ".join(argv))
    log_fh = tempfile.NamedTemporaryFile(
        prefix="ocserve-server-", suffix=".log", delete=False)
    log_path = log_fh.name
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,       # own process group: stop() reaps it
        )
    except OSError as exc:
        log_fh.close()
        _unlink(log_path)
        raise StartupError(f"could not spawn {binary!r}: {exc}") from exc

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = _drain_brief(log_path)
            _kill(proc)
            log_fh.close()
            _unlink(log_path)
            raise StartupError(
                f"{binary} serve exited during startup with code {proc.returncode}"
                + (f" (output: {output})" if output else ""))
        if _health_ok(base_url):
            log.info("ocserve: ready at %s (pid %s)", base_url, proc.pid)
            return ServeHandle(base_url, proc, argv, log_path=log_path,
                               log_fh=log_fh)
        time.sleep(READY_POLL)
    output = _drain_brief(log_path)
    _kill(proc)
    log_fh.close()
    _unlink(log_path)
    raise StartupError(
        f"{binary} serve was not ready within {startup_timeout:g}s"
        + (f" (output: {output})" if output else ""))


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


_shared_lock = threading.Lock()
_shared = None


def get_shared_server():
    """The one server this orchestrator process uses, started on first use.

    No idle TTL in v1: the server starts no model and is cheap when idle, so
    tearing it down between tasks would only pay the startup cost per task.
    Callers that need a private server (tests) call start_server() directly.
    """
    global _shared
    with _shared_lock:
        if (_shared is not None and not _shared.stopped
                and _shared.proc.poll() is None):
            return _shared
        _shared = start_server()
        return _shared


# --------------------------------------------------------------------------
# HTTP plumbing (http.client, called from asyncio via threads)
# --------------------------------------------------------------------------

def _request_sync(method, url, body=None, headers=None, timeout=_HTTP_TIMEOUT):
    """(status, body-bytes) for one short request. Raises OSError on transport."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


async def _request(method, url, body=None, headers=None, timeout=_HTTP_TIMEOUT):
    return await asyncio.to_thread(_request_sync, method, url, body, headers, timeout)


def _json_or_raw(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw.decode("utf-8", "replace")


def _split_model(model):
    """(providerID, model_name) from `model`, or (None, None) if unsplittable.

    Accepts "provider/model" ("ARC/GLM-5.3") or a dict carrying either naming
    ("id" or "modelID", as the server's two routes each use). A dict missing
    providerID is unusable — the server's own error for a payload without one
    is `Missing key at ["model"]["providerID"]` — so it yields (None, None)
    rather than a half-built object.
    """
    if isinstance(model, str):
        provider, _, name = model.partition("/")
        return (provider or None), (name or None)
    if isinstance(model, dict):
        provider = model.get("providerID") or model.get("provider")
        name = model.get("id") or model.get("modelID") or model.get("model")
        if provider and name:
            return provider, name
    return None, None


def _model_for_session(model):
    """The `model` object POST /session accepts: {"providerID", "id"}.

    The two routes spell the same field DIFFERENTLY, verified against opencode
    1.18.29: /session takes `id` and rejects `modelID` with 400
    {"_tag":"BadRequest"}, while /session/<id>/prompt_async takes `modelID`.
    One shared object cannot satisfy both.
    """
    provider, name = _split_model(model)
    if not provider:
        return None
    return {"providerID": provider, "id": name}


def _model_for_prompt(model):
    """The `model` object prompt_async accepts: {"providerID", "modelID"}."""
    provider, name = _split_model(model)
    if not provider:
        return None
    return {"providerID": provider, "modelID": name}


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------

def _response_socket(resp, conn):
    """The live socket behind an HTTP response, wherever http.client put it.

    A read-until-EOF response closes over its own socket: `conn.getresponse()`
    sets `conn.sock` to None and the socket moves to `resp.fp.raw._sock`. A
    Content-Length response leaves it on the connection. Both are checked so
    close() always gets the object whose shutdown() actually wakes a blocked
    read (measured: shutting the connection down does nothing when the socket
    was detached — the reader thread, its fd and the server's subscriber all
    survive; shutting THIS one ends the read in EOF).
    """
    for getter in (lambda: resp.fp.raw._sock, lambda: conn.sock):
        try:
            sock = getter()
        except (AttributeError, ValueError, OSError):
            continue
        if sock is not None:
            return sock
    return None


class _SSEStream:
    """Incremental reader over one text/event-stream response.

    `http.client` is blocking and line-oriented; a daemon thread reads it and
    hands parsed frames to the event loop. The reader NEVER closes the socket
    itself — close() does, from the consumer side — so a reader that outlives
    the loop cannot raise out of an unrelated stack frame.
    """

    _END = object()

    def __init__(self, url, directory, sock_timeout=None):
        self.url = url
        self.directory = directory
        # No socket timeout by default: the stream must survive a long prompt
        # (Rule 7 — a total cap here would kill healthy work). Stall detection
        # belongs to the caller, which applies its own asyncio deadline.
        self.sock_timeout = sock_timeout
        self._queue = asyncio.Queue()
        self._loop = None
        self._conn = None
        self._resp = None
        self._sock = None
        self._connected = threading.Event()
        self._thread = None

    def _bind_loop(self):
        if self._loop is None:
            self._loop = asyncio.get_event_loop()

    def _headers(self):
        h = {"accept": "text/event-stream", "cache-control": "no-cache"}
        if self.directory:
            h["x-opencode-directory"] = self.directory
        return h

    def _read_loop(self):
        try:
            parts = urlsplit(self.url)
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80,
                                              timeout=self.sock_timeout)
            self._conn = conn
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            conn.request("GET", path, headers=self._headers())
            resp = conn.getresponse()
            if resp.status != 200:
                self._connected.set()
                raise OcserveError(f"event stream returned HTTP {resp.status}")
            # Hand the consumer the pieces it must close from its side. For a
            # read-until-EOF stream http.client DETACHES the socket from the
            # connection (`conn.sock` is None) and gives it to the response's
            # buffered reader, so the response is where the live socket is.
            self._resp = resp
            self._sock = _response_socket(resp, conn)
            # The subscription is live once the first line is read; prompt()
            # waits for this so no event can race ahead of it.
            data_lines = []
            first = True
            while True:
                raw = resp.fp.readline()
                if not raw:                      # server closed the stream
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if first and line != "":
                    self._connected.set()
                    first = False
                if line == "":                   # frame boundary
                    if data_lines:
                        payload = "\n".join(data_lines)
                        data_lines = []
                        if payload.strip():
                            try:
                                obj = json.loads(payload)
                            except ValueError:
                                obj = None
                            if obj is not None:
                                self._deliver(obj)
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                # `event:` / `id:` / comment lines carry nothing we need.
        except Exception as exc:                 # transport death is normal
            self._connected.set()
            self._deliver(exc)
        finally:
            self._connected.set()
            self._deliver(self._END)

    def _deliver(self, item):
        """Hand one item to the loop, or drop it if the consumer is gone."""
        def put():
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:            # pragma: no cover - unbounded
                pass
        try:
            self._loop.call_soon_threadsafe(put)
        except (RuntimeError, AttributeError):   # loop closed/never bound
            pass

    def start(self):
        self._bind_loop()
        self._thread = threading.Thread(target=self._read_loop,
                                        name="ocserve-sse", daemon=True)
        self._thread.start()
        return self

    async def wait_connected(self, timeout=10.0):
        """Block until the subscription is live, or fail if it never is."""
        self._bind_loop()
        if not self._connected.is_set():
            ok = await asyncio.to_thread(self._connected.wait, timeout)
            if not ok:
                raise OcserveError(
                    f"event stream did not subscribe within {timeout:g}s")
        return True

    async def next_event(self):
        """The next frame, or None when the stream ended."""
        self._bind_loop()
        item = await self._queue.get()
        if item is self._END:
            return None
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        """Release the reader. Idempotent, from the consumer side only.

        `conn.close()` ALONE DOES NOT WORK: the reader blocks in
        `resp.fp.readline()`, and `HTTPConnection.close()` drops only the
        connection's reference to the socket — the response holds its own, so
        the thread, its fd and the server-side subscriber all survive
        (measured against tests/fake_ocserve.py: 11 prompts -> 11 live reader
        threads, 11 extra fds, 11 sockets still held by the fake). Shutting the
        socket down is what wakes the blocked read; closing the response drops
        the last reference so the fd actually goes away.
        """
        sock, resp, conn = self._sock, self._resp, self._conn
        self._sock = self._resp = self._conn = None
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        # Join the reader BEFORE closing the response/connection: both take the
        # buffered reader's lock, and a reader still parked in `readline()`
        # holds it, so closing first blocks the caller instead of releasing
        # anything. After shutdown the read returns and the thread exits.
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        for closer in (
                lambda: resp.close() if resp is not None else None,
                lambda: conn.close() if conn is not None else None,
        ):
            try:
                closer()
            except Exception:
                pass


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class ServeResult:
    """The outcome of one prompt: the assistant text plus the token receipt."""

    def __init__(self, text="", tokens=None, session_id=None):
        self.text = text
        self.tokens = tokens or _empty_tokens()
        self.session_id = session_id

    def __repr__(self):  # pragma: no cover - diagnostic
        return (f"<ServeResult {len(self.text)} chars, "
                f"{self.tokens.get('input', 0)}+{self.tokens.get('output', 0)} tok>")


class OcserveClient:
    """One bound session on an `opencode serve` instance.

    Usage::

        client = await OcserveClient.create(handle, worktree="/path/to/wt")
        result = await client.prompt("do the thing")
        await client.dispose()
    """

    def __init__(self, handle, directory, session_id=None):
        self.handle = handle
        self.directory = str(directory)
        self.base_url = handle.base_url
        self.session_id = session_id

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    async def create(cls, handle, worktree, model=None, agent=None):
        """Create the session bound to `worktree` via x-opencode-directory."""
        client = cls(handle, worktree)
        payload = {}
        session_model = _model_for_session(model)
        if session_model:
            payload["model"] = session_model
        if agent:
            payload["agent"] = agent
        status, raw = await _request(
            "POST", handle.base_url + "/session",
            body=json.dumps(payload).encode(),
            headers=client._headers(json_body=True))
        if status not in (200, 201):
            raise OcserveError(f"session create failed: HTTP {status}: {_brief(raw)}")
        info = _json_or_raw(raw)
        if not isinstance(info, dict) or not info.get("id"):
            raise OcserveError(f"session create returned no session id: {_brief(raw)}")
        client.session_id = info["id"]
        log.info("ocserve: session %s bound to %s", client.session_id, worktree)
        return client

    def _headers(self, json_body=False):
        h = {"x-opencode-directory": self.directory}
        if json_body:
            h["content-type"] = "application/json"
        return h

    # -- prompting ---------------------------------------------------------
    async def prompt(self, text, model=None, timeout=None):
        """Send `text` and consume the stream until this prompt finishes.

        Returns a ServeResult. Raises CapacityFull when the provider refused on
        concurrency, OcserveError for any other reported error or a stream that
        ended without an answer.
        """
        if not self.session_id:
            raise OcserveError("no session: call OcserveClient.create() first")
        stream = _SSEStream(self.base_url + "/event", self.directory).start()
        try:
            # Subscribe BEFORE prompting: the server only writes a frame to
            # subscribers that already exist, so prompting first would drop the
            # very events this call is waiting for.
            await stream.wait_connected()
            payload = {"parts": [{"type": "text", "text": text}]}
            prompt_model = _model_for_prompt(model)
            if prompt_model:
                payload["model"] = prompt_model
            status, raw = await _request(
                "POST", f"{self.base_url}/session/{self.session_id}/prompt_async",
                body=json.dumps(payload).encode(),
                headers=self._headers(json_body=True))
            if status != 204:
                raise OcserveError(f"prompt_async failed: HTTP {status}: {_brief(raw)}")
            return await self._consume(stream, timeout=timeout)
        finally:
            stream.close()

    async def _consume(self, stream, timeout=None):
        texts, tokens, roles = [], _empty_tokens(), {}
        deadline = None if not timeout else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.01, deadline - time.monotonic())
            try:
                if remaining is None:
                    event = await stream.next_event()
                else:
                    event = await asyncio.wait_for(stream.next_event(), remaining)
            except asyncio.TimeoutError:
                raise OcserveError(f"timed out after {timeout:g}s waiting for the stream")
            except OcserveError:
                raise
            except Exception as exc:
                raise OcserveError(f"event stream failed: {exc}") from exc
            if event is None:
                raise OcserveError("event stream ended before the prompt finished")
            if not isinstance(event, dict):
                continue
            props = event.get("properties")
            props = props if isinstance(props, dict) else {}
            if props.get("sessionID") not in (None, self.session_id):
                continue                          # another session's traffic
            etype = event.get("type")
            if etype == "session.error":
                self._raise_for_error(props.get("error"))
            elif etype == "message.updated":
                self._note_role(props.get("info"), roles)
            elif etype == "message.part.updated":
                self._absorb_part(props.get("part"), texts, tokens, roles)
            elif etype == "session.idle" and props.get("sessionID") == self.session_id:
                return ServeResult("".join(texts), tokens, self.session_id)

    @staticmethod
    def _note_role(info, roles):
        """Remember each message's role — the stream carries the USER's text too.

        The /event stream is not assistant-only: the user's own prompt comes
        back as a `message.part.updated` text part before the answer starts
        (measured live: the prompt echo is frame 3 of 83). Without the role,
        `result.text` would open with the prompt and hand that echo to verdict
        parsing and transcript_tokens downstream.
        """
        if isinstance(info, dict) and info.get("id"):
            roles[info["id"]] = info.get("role")

    def _absorb_part(self, part, texts, tokens, roles=None):
        if not isinstance(part, dict):
            return
        ptype = part.get("type")
        if ptype == "text" and part.get("text"):
            # Unknown role is kept: a stream that never announced the message
            # must not silently drop the answer.
            if roles is None or roles.get(part.get("messageID"), "assistant") == "assistant":
                texts.append(part["text"])
        elif ptype == "step-finish":
            _merge_tokens(tokens, part.get("tokens"))

    @staticmethod
    def _raise_for_error(error):
        """session.error -> CapacityFull when explicitly non-retryable."""
        if isinstance(error, dict):
            data = error.get("data")
            data = data if isinstance(data, dict) else {}
            message = data.get("message") or error.get("name") or "unknown error"
            if data.get("isRetryable") is False:
                raise CapacityFull(f"concurrency refusal: {message}")
            raise OcserveError(str(message))
        raise OcserveError(str(error or "unknown error"))

    # -- teardown ----------------------------------------------------------
    async def dispose(self):
        """POST /instance/dispose for THIS directory. Instant, no drain.

        The server keeps running; this releases the instance bound to the
        request's x-opencode-directory header.
        """
        status, raw = await _request(
            "POST", self.base_url + "/instance/dispose", body=b"",
            headers=self._headers())
        if status != 200:
            raise OcserveError(f"dispose failed: HTTP {status}: {_brief(raw)}")
        return True


def _empty_tokens():
    return {"input": 0, "output": 0, "reasoning": 0,
            "cache": {"read": 0, "write": 0}}


def _merge_tokens(into, step_tokens):
    """Fold one step-finish receipt in, keeping the server's field names.

    Mirrors drivers.transcript_tokens: `total` is its documented sum —
    input + output + reasoning + cache.read + cache.write.
    """
    if not isinstance(step_tokens, dict):
        return
    for key in ("input", "output", "reasoning"):
        into[key] = into.get(key, 0) + (step_tokens.get(key) or 0)
    cache = step_tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    for key in ("read", "write"):
        into["cache"][key] = into["cache"].get(key, 0) + (cache.get(key) or 0)
    into["total"] = _token_total(into)


def _token_total(tokens):
    return (tokens.get("input", 0) + tokens.get("output", 0)
            + tokens.get("reasoning", 0)
            + tokens["cache"].get("read", 0) + tokens["cache"].get("write", 0))


def _brief(raw, limit=300):
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return " ".join(text.split())[:limit]
