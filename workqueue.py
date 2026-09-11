"""Durable, idempotent, push-notified work queue (sqlite + unix datagrams).

Named `workqueue`, not `queue`: this repo's modules live in the root and every
entrypoint runs with that root on sys.path, so a file called queue.py SHADOWS
the standard library's for the whole process. Verified, not guessed — `import
queue` from this directory resolves here, and concurrent.futures imports it.

Replaces two things that were neither durable nor push:

* ``run-queue.sh`` held its pending list in the shell's argv. Killing the queue
  lost every task file that had not started yet, which is why a restart meant
  retyping the list from scratch and why an interrupted wave silently shrank.
* ``drivers._lease_acquire`` polls every 20 s. A slot freed one second after a
  poll is not noticed for another nineteen, and with PR reviewers as the
  scarcest resource in the fleet that delay lands on exactly the handoffs that
  matter most.

Three properties, in the order they matter:

**Durable.** State lives in the same sqlite database as everything else, so a
killed process loses nothing and a restarted one sees the true queue.

**Idempotent.** Enqueuing is keyed on ``(topic, dedupe_key)`` and a partial
unique index makes a second enqueue of live work a no-op that returns the
existing id rather than a second item. The index covers only ``pending`` and
``claimed`` rows, so finishing an item frees its key for a legitimate future
re-run — "run this task file again tomorrow" must still work.

Delivery is at-least-once, which is the only thing a crash-safe queue can
honestly promise: a worker that dies mid-item has its lease reclaimed and the
item handed to someone else. Exactly-once *effect* is the caller's job, and the
dedupe key is the tool for it — see ``claim``.

**Push, with a poll fallback.** Notification is a datagram to an AF_UNIX socket
per waiter. A datagram can be lost and a subscriber can die without unregistering,
so ``wait`` always takes a timeout and callers keep their polling loop as a
backstop. Push turns a 20 s wait into a millisecond one; it is not load-bearing
for correctness, and building it as though it were would be a worse system.
"""
import contextlib
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_items(
    id          INTEGER PRIMARY KEY,
    topic       TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'pending',
    priority    INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    claimed_by  TEXT,
    lease_until REAL,
    created_at  REAL NOT NULL,
    finished_at REAL,
    result      TEXT
);
-- Idempotency. Partial so a FINISHED item frees its key: enqueuing the same
-- work again later is legitimate, enqueuing it twice while it is live is not.
CREATE UNIQUE INDEX IF NOT EXISTS queue_active_key
    ON queue_items(topic, dedupe_key) WHERE state IN ('pending', 'claimed');
CREATE INDEX IF NOT EXISTS queue_ready
    ON queue_items(topic, state, priority DESC, id);
CREATE TABLE IF NOT EXISTS queue_subscribers(
    path       TEXT PRIMARY KEY,
    topic      TEXT NOT NULL,
    pid        INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""

ACTIVE = ("pending", "claimed")
DEFAULT_LEASE_S = 900.0


class Queue:
    """One connection to the queue tables. Safe to share across threads."""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        self.lock = threading.RLock()
        # isolation_level=None hands transaction control to us. claim() needs
        # BEGIN IMMEDIATE to take its row-lock up front, and the default
        # implicit-transaction mode refuses a BEGIN inside the one it already
        # opened. Single statements autocommit, so the commit() calls below
        # stay correct and simply become no-ops.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                    timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---- writing ---------------------------------------------------------

    def enqueue(self, topic, dedupe_key, payload=None, priority=0):
        """Add work. Returns (id, created).

        ``created`` is False when live work with this key already exists, and
        the id returned is that existing item's. Callers can therefore enqueue
        freely — on a retry, on a restart, from two places at once — without
        checking first and without racing.
        """
        now = time.time()
        body = json.dumps(payload or {}, default=str)
        with self.lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO queue_items(topic, dedupe_key, payload, priority,"
                    " created_at) VALUES(?,?,?,?,?)",
                    (topic, dedupe_key, body, int(priority), now))
                self.conn.commit()
                return cur.lastrowid, True
            except sqlite3.IntegrityError:
                row = self.conn.execute(
                    "SELECT id FROM queue_items WHERE topic=? AND dedupe_key=?"
                    " AND state IN ('pending','claimed')", (topic, dedupe_key)
                ).fetchone()
                if row is None:
                    raise
                return row["id"], False

    def claim(self, topic, worker=None, lease_s=DEFAULT_LEASE_S):
        """Take the next ready item, or None.

        The claim is a LEASE, not a handoff: if this worker dies the item comes
        back (see reclaim). That is what makes delivery at-least-once, and it is
        why a caller whose work is not naturally repeatable should make its
        effect idempotent — keying the side effect on ``dedupe_key`` is usually
        enough, and it is the same key the queue already dedupes on.
        """
        worker = worker or f"{os.getpid()}"
        now = time.time()
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT id FROM queue_items WHERE topic=? AND state='pending'"
                    " ORDER BY priority DESC, id LIMIT 1", (topic,)).fetchone()
                if row is None:
                    self.conn.execute("COMMIT")
                    return None
                self.conn.execute(
                    "UPDATE queue_items SET state='claimed', claimed_by=?,"
                    " lease_until=?, attempts=attempts+1 WHERE id=?",
                    (worker, now + float(lease_s), row["id"]))
                item = self.conn.execute(
                    "SELECT * FROM queue_items WHERE id=?", (row["id"],)).fetchone()
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return _as_item(item)

    def complete(self, item_id, result=None):
        return self._finish(item_id, "done", result)

    def fail(self, item_id, error=None, retry=False):
        """Finish as failed, or put it back for another attempt.

        ``retry`` returns the item to `pending` rather than burying it, which is
        the right answer for a transient failure — and the attempts counter it
        already carries is what stops that from looping forever.
        """
        if retry:
            with self.lock:
                self.conn.execute(
                    "UPDATE queue_items SET state='pending', claimed_by=NULL,"
                    " lease_until=NULL, result=? WHERE id=?",
                    (json.dumps({"error": str(error)[:500]}), item_id))
                self.conn.commit()
            return True
        return self._finish(item_id, "failed", {"error": str(error)[:500]})

    def _finish(self, item_id, state, result):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE queue_items SET state=?, finished_at=?, result=?"
                " WHERE id=? AND state='claimed'",
                (state, time.time(), json.dumps(result or {}, default=str), item_id))
            self.conn.commit()
        # Zero rows means someone already finished it (a reclaimed lease whose
        # original worker came back). Report it rather than pretending.
        return cur.rowcount > 0

    def reclaim(self, topic=None, now=None):
        """Return expired claims to `pending`. How a dead worker's work survives."""
        now = now if now is not None else time.time()
        sql = ("UPDATE queue_items SET state='pending', claimed_by=NULL,"
               " lease_until=NULL WHERE state='claimed' AND lease_until < ?")
        args = [now]
        if topic:
            sql += " AND topic=?"
            args.append(topic)
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
        return cur.rowcount

    # ---- reading ---------------------------------------------------------

    def stats(self, topic=None):
        sql = "SELECT topic, state, COUNT(*) n FROM queue_items"
        args = []
        if topic:
            sql += " WHERE topic=?"
            args.append(topic)
        sql += " GROUP BY topic, state"
        out = {}
        with self.lock:
            for r in self.conn.execute(sql, args):
                out.setdefault(r["topic"], {})[r["state"]] = r["n"]
        return out

    def pending(self, topic, limit=100):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM queue_items WHERE topic=? AND state='pending'"
                " ORDER BY priority DESC, id LIMIT ?", (topic, limit)).fetchall()
        return [_as_item(r) for r in rows]

    # ---- push notification ----------------------------------------------

    def notify(self, topic):
        """Wake every live subscriber to `topic`. Returns how many were reached.

        Best effort on purpose: a subscriber that died without unregistering is
        pruned here rather than being allowed to accumulate, and a send that
        fails for any other reason is not an error worth failing a release over.
        """
        with self.lock:
            subs = self.conn.execute(
                "SELECT path FROM queue_subscribers WHERE topic=?", (topic,)
            ).fetchall()
        reached, dead = 0, []
        for s in subs:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sk:
                    sk.settimeout(0.2)
                    sk.sendto(b"1", s["path"])
                reached += 1
            except OSError:
                dead.append(s["path"])
        if dead:
            with self.lock:
                self.conn.executemany(
                    "DELETE FROM queue_subscribers WHERE path=?",
                    [(p,) for p in dead])
                self.conn.commit()
                for p in dead:
                    with contextlib.suppress(OSError):
                        os.unlink(p)
        return reached

    def subscribe(self, topic):
        """A Subscription that wakes on notify(topic). Use as a context manager."""
        return Subscription(self, topic)


class Subscription:
    """An AF_UNIX datagram socket registered against a topic.

    Deliberately not a guarantee. A datagram can be dropped, a process can die
    between registering and listening, and a notify can race a subscribe. Every
    ``wait`` therefore takes a timeout and returns False rather than blocking
    forever, so a caller's existing poll loop stays the source of truth and this
    only makes it arrive sooner.
    """

    def __init__(self, q, topic):
        self.q = q
        self.topic = topic
        self.path = str(Path(tempfile.gettempdir()) /
                        f"arcq-{topic}-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
        self.sock = None

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.setblocking(False)
        with self.q.lock:
            self.q.conn.execute(
                "INSERT OR REPLACE INTO queue_subscribers(path, topic, pid, created_at)"
                " VALUES(?,?,?,?)", (self.path, self.topic, os.getpid(), time.time()))
            self.q.conn.commit()
        return self

    def __exit__(self, *exc):
        with self.q.lock:
            self.q.conn.execute("DELETE FROM queue_subscribers WHERE path=?",
                                (self.path,))
            self.q.conn.commit()
        if self.sock is not None:
            self.sock.close()
        with contextlib.suppress(OSError):
            os.unlink(self.path)
        return False

    def wait(self, timeout):
        """True if woken, False if the timeout expired. Drains coalesced wakes."""
        import select
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            r, _, _ = select.select([self.sock], [], [], left)
            if not r:
                return False
            woken = False
            while True:
                try:
                    self.sock.recv(64)
                    woken = True
                except (BlockingIOError, OSError):
                    break
            if woken:
                return True

    async def wait_async(self, timeout):
        """asyncio flavour — the drivers are async and must not block the loop."""
        import asyncio
        loop = asyncio.get_running_loop()
        fired = loop.create_future()

        def on_readable():
            while True:
                try:
                    self.sock.recv(64)
                except (BlockingIOError, OSError):
                    break
            if not fired.done():
                fired.set_result(True)

        loop.add_reader(self.sock.fileno(), on_readable)
        try:
            return await asyncio.wait_for(fired, timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(self.sock.fileno())


def _as_item(row):
    if row is None:
        return None
    d = dict(row)
    for k in ("payload", "result"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                pass
    return d
