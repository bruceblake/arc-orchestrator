"""Hermetic fake of `opencode serve` for tests — 127.0.0.1 only.

Speaks the protocol subset measured against opencode 1.18.29 (see the module
docstring in ocserve.py): GET /global/health, POST /session, POST
/session/<id>/prompt_async, GET /event (SSE), POST /instance/dispose.

It records what it saw — the x-opencode-directory header on every route, every
prompt body — so tests can assert the header actually reaches the server rather
than trusting the client's intent. Scenarios are driven by the prompt text:

  * `"hi"`           -> one text part, one step-finish receipt, session.idle
  * `"capacity!"`    -> session.error with an APIError, isRetryable false
  * `"boom"`         -> session.error with UnknownError (no isRetryable)
  * `"emit:<json>"`  -> the frames in <json> (a list, or an SSE script string)
  * anything else    -> echo of the prompt text as the assistant answer

NEVER spawns the real binary; everything is in-process on loopback.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = "127.0.0.1"

# Message ids the scripted streams attribute parts to, so the client's role
# filter has something real to key on (the live server sends these too).
USER_MSG_ID = "msg_fakeuser0001"
ASSISTANT_MSG_ID = "msg_fakeassistant0001"


def _model_key_error(body, route, wanted):
    """The real server's model-route contract, enforced on the fake.

    opencode 1.18.29 spells the model-name key DIFFERENTLY per route and
    rejects the other with 400 (measured): `/session` wants `id`,
    `/session/<id>/prompt_async` wants `modelID`, and both require
    `providerID`. A fake that accepted either key would let a client pass its
    tests and still fail against the binary, so the fake enforces the contract
    and the wrong shape is a loud 400 here too.
    """
    model = body.get("model") if isinstance(body, dict) else None
    if model is None:
        return None                       # omitting the model is legal
    if not isinstance(model, dict):
        return "model must be an object"
    if not model.get("providerID"):
        return 'Missing key at ["model"]["providerID"]'
    if not model.get(wanted):
        return 'Missing key at ["model"]["%s"] on %s' % (wanted, route)
    return None


class FakeOcserve:
    """Start/stoppable fake server.

    `scenario` may be a plain string prompt name or a dict {"text": ..., ...};
    `frames_for(prompt_text)` is overridable to script an arbitrary stream.
    """

    def __init__(self, scenario="hi", health_ok=True, delay=0.0):
        self.scenario = scenario
        self.health_ok = health_ok
        self.delay = delay
        self.sessions = {}          # session id -> directory header it was created with
        self.session_models = {}    # session id -> model object /session received
        self.prompt_models = []     # model object each prompt_async received
        self.directories = {}       # route -> most recent x-opencode-directory seen
        self.prompts = []           # list of {"session", "directory", "body"}
        self.disposed = []          # directories dispose was called for
        self.hits = []              # route names, in order
        self._next = 0
        self._sse_clients = []
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        handler = _handler_for(self)
        self._httpd = ThreadingHTTPServer((HERE, 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="fake-ocserve", daemon=True)
        self._thread.start()
        return self

    @property
    def port(self):
        return self._httpd.server_address[1]

    @property
    def base_url(self):
        return f"http://{HERE}:{self.port}"

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        for client in list(self._sse_clients):
            self.release_stream(client)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- stream control ----------------------------------------------------
    def hold_stream(self, raw_socket):
        with self._lock:
            self._sse_clients.append(raw_socket)

    def release_stream(self, raw_socket):
        """Close a held SSE response so the client's reader thread ends."""
        try:
            raw_socket.shutdown(1)
        except OSError:
            pass
        try:
            raw_socket.close()
        except OSError:
            pass

    def release_all_streams(self):
        for client in list(self._sse_clients):
            self.release_stream(client)

    # -- scripting ---------------------------------------------------------
    def new_session_id(self):
        with self._lock:
            self._next += 1
            return f"ses_fake{self._next:04d}"

    def frames_for(self, text):
        """The SSE frames one prompt produces, as a list of dicts.

        Mirrors the REAL server's ordering (measured on opencode 1.18.29):
        `message.updated` announces a message's role, and the user's own prompt
        comes back as a `message.part.updated` text part BEFORE the assistant's
        answer — so a client that ignores roles collects the prompt echo as if
        it were the answer.
        """
        if text == "capacity!":
            return [{"type": "session.error", "properties": {
                "sessionID": None, "error": {"name": "APIError", "data": {
                    "message": "concurrent session limit reached",
                    "statusCode": 400, "isRetryable": False}}}}]
        if text == "boom":
            return [{"type": "session.error", "properties": {
                "sessionID": None, "error": {"name": "UnknownError", "data": {
                    "message": "Model not found: nope/nope."}}}}]
        if text.startswith("emit:"):
            payload = json.loads(text[len("emit:"):])
            return payload
        if text == "hi":
            answer, tokens = "Hello from the fake server.", {
                "total": 57, "input": 11, "output": 22,
                "reasoning": 3, "cache": {"read": 20, "write": 1}}
        else:
            answer, tokens = f"echo: {text}", {
                "input": 5, "output": 7, "reasoning": 0,
                "cache": {"read": 0, "write": 0}}
        return [
            # the user's turn, echoed back exactly as the real stream does
            {"type": "message.updated", "properties": {"info": {
                "id": USER_MSG_ID, "role": "user"}}},
            {"type": "message.part.updated", "properties": {"part": {
                "type": "text", "messageID": USER_MSG_ID, "text": text}}},
            {"type": "message.updated", "properties": {"info": {
                "id": ASSISTANT_MSG_ID, "role": "assistant"}}},
            {"type": "message.part.updated", "properties": {"part": {
                "type": "text", "messageID": ASSISTANT_MSG_ID, "text": answer}}},
            {"type": "message.part.updated", "properties": {"part": {
                "type": "step-finish", "messageID": ASSISTANT_MSG_ID,
                "tokens": tokens}}},
            {"type": "session.idle", "properties": {}},
        ]


def _handler_for(fake):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):   # keep test output clean
            pass

        # -- helpers --
        def _directory(self):
            return self.headers.get("x-opencode-directory")

        def _record(self, route):
            with fake._lock:
                fake.hits.append(route)
                fake.directories[route] = self._directory()

        def _body(self):
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw.decode() or "{}")
            except ValueError:
                return {}

        def _json(self, status, obj):
            payload = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _empty(self, status=204):
            self.send_response(status)
            self.send_header("content-length", "0")
            self.end_headers()

        # -- routes --
        def do_GET(self):
            route = self.path.split("?")[0]
            if route == "/global/health":
                self._record("health")
                if not fake.health_ok:
                    self._json(503, {"error": "not ready"})
                    return
                self._json(200, {"healthy": True})
            elif route == "/event":
                self._event_stream()
            else:
                self._record(route)
                self._json(404, {"error": "not found"})

        def do_POST(self):
            route = self.path.split("?")[0]
            if route == "/session":
                self._record("session.create")
                body = self._body()
                directory = self._directory()
                # The REAL server takes {"providerID", "id"} here and answers
                # 400 for "modelID" (measured on 1.18.29) — so the fake must
                # refuse it too, or a wrong payload shape passes the tests and
                # fails against the binary.
                bad = _model_key_error(body, route, "id")
                if bad:
                    self._json(400, {"_tag": "BadRequest", "message": bad})
                    return
                sid = fake.new_session_id()
                with fake._lock:
                    fake.sessions[sid] = directory
                    fake.session_models[sid] = (body.get("model") or {})
                self._json(200, {"id": sid, "slug": "fake", "projectID": "global",
                                 "directory": directory, "version": "fake"})
            elif route.endswith("/prompt_async"):
                self._record("prompt_async")
                sid = route.split("/")[2]
                body = self._body()
                directory = self._directory()
                bad = _model_key_error(body, route, "modelID")
                with fake._lock:
                    fake.prompts.append({"session": sid, "directory": directory,
                                         "body": body})
                if sid not in fake.sessions:
                    self._json(404, {"error": "unknown session"})
                    return
                if bad:
                    # The real server's wording for a model without providerID
                    # is `Missing key at ["model"]["providerID"]`.
                    self._json(400, {"name": "BadRequest", "data": {
                        "message": bad, "kind": "Payload"}})
                    return
                # Accept first (204), then the answer arrives on /event — the
                # order the real server uses.
                self._empty(204)
                self._emit_for(sid, body, directory)
            elif route == "/instance/dispose":
                self._record("instance.dispose")
                with fake._lock:
                    fake.disposed.append(self._directory())
                self._json(200, True)
            else:
                self._record(route)
                self._json(404, {"error": "not found"})

        # -- SSE --
        def _event_stream(self):
            self._record("event")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "keep-alive")
            self.end_headers()
            # Register BEFORE the first frame: the real server only writes to
            # subscribers that already exist, and the client waits for this
            # frame before prompting.
            fake.hold_stream(self.connection)
            self._write_frame({"type": "server.connected", "properties": {}})
            try:
                # Hold the connection open until the client closes it: the
                # real server streams until the subscriber goes away.
                self.rfile.read(1)
            except OSError:
                pass

        def _emit_for(self, sid, body, directory=None):
            parts = body.get("parts") or []
            text = parts[0].get("text") if parts and isinstance(parts[0], dict) else ""
            frames = fake.frames_for(text or "")
            # Record the model object the client sent, so a test can assert the
            # per-route key (the real server 400s on the wrong one).
            with fake._lock:
                fake.prompt_models.append(body.get("model") or {})
            for frame in frames:
                props = dict(frame.get("properties") or {})
                props.setdefault("sessionID", sid)
                self._write_frame({"type": frame.get("type"), "properties": props})

        def _write_frame(self, obj):
            with fake._lock:
                sockets = list(fake._sse_clients)
            if not sockets:
                return
            if fake.delay:
                import time
                time.sleep(fake.delay)
            payload = f"data: {json.dumps(obj)}\n\n".encode()
            for sock in sockets:
                try:
                    sock.sendall(payload)
                except OSError:
                    pass

    return Handler
