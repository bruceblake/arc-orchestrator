"""Regression tests for corrupt on-disk state: taskfiles, event log, transcripts, db.

Every case here has actually bitten this box. A restart truncated a taskfile
mid-write and every later gate then failed with "Expecting value: line 1
column 1"; a git merge left conflict markers inside logs/events.jsonl; a
killed run left 0-byte harness transcripts; a db path pointed through a
directory that did not exist; and the dashboard plus a live `code run` hold
separate Store objects on orchestrator.db at the same time. These tests pin
the RECOVERY behaviour — what the operator still sees after the corruption —
not merely that nothing raises: the projects page must still list healthy
work, event readers must still surface good events, transcript parsers must
still show the last real output, and a clean failure must not leave partial
state behind.
"""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import capture_events  # noqa: F401  (sys.path + event redirect)
from helpers import ENTRY, STRONGEST  # noqa: E402,F401

import code_tasks
import config
import dashboard
import drivers
from store import Store


GOOD_TASK = {"id": "t1", "prompt": "p", "model": config.ESCALATION_PATH[0],
             "reviewer": "glm", "verify_cmd": "true"}


class _FakeHandler(dashboard.Handler):
    """A socket-less Handler that captures status/headers/body in memory.

    BaseHTTPRequestHandler.__init__ requires a live socket; we skip it and
    set only what do_GET reads, then override the write path so nothing is
    sent over the network. Mirrors tests/test_http_read.py.
    """

    def __init__(self):
        self.status = None
        self.response_headers = {}
        self.body = b""
        self.wfile = self
        self.path = "/"

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data


class CorruptStateTaskfileTests(unittest.TestCase):
    """Killed restarts and bad hand-edits leave broken taskfiles in ~/tasks.

    The projects page scans every *.json in TASKS_DIR on every load; one
    broken file must not blank the whole fleet view, and the loader must
    fail with a precise, catchable error instead of a mystery crash deep
    inside a gate.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._orig_tasks = config.TASKS_DIR
        self._orig_log = config.EVENTS_LOG
        config.TASKS_DIR = str(self.tmp / "tasks")
        config.EVENTS_LOG = str(self.tmp / "events.jsonl")
        Path(config.TASKS_DIR).mkdir(parents=True)
        dashboard._lines_cache["key"] = None

    def tearDown(self):
        config.TASKS_DIR = self._orig_tasks
        config.EVENTS_LOG = self._orig_log
        dashboard._lines_cache["key"] = None
        self._dir.cleanup()

    def _good_taskfile(self):
        tdir = Path(config.TASKS_DIR)
        f = tdir / "good.json"
        f.write_text(json.dumps({"project": {"repo": "/tmp", "title": "G",
                                             "tasks": [GOOD_TASK]}}),
                     encoding="utf-8")
        return f

    def _projects(self):
        store = Store(":memory:")
        try:
            return dashboard._projects(store)
        finally:
            store.conn.close()

    def test_zero_byte_taskfile_is_skipped_and_good_projects_listed(self):
        """A restart-truncated 0-byte file must vanish from the page, not it.

        The dashboard is the one screen watched mid-run; if one empty file
        took down /api/projects the operator is blind to every healthy task.
        """
        tdir = Path(config.TASKS_DIR)
        (tdir / "zero.json").write_bytes(b"")
        self._good_taskfile()
        out = self._projects()
        self.assertEqual([p["file"] for p in out], ["good.json"])
        self.assertEqual(out[0]["n_tasks"], 1)

    def test_truncated_taskfile_is_skipped_and_good_projects_listed(self):
        """A file cut mid-write is unparseable JSON; it must be dropped.

        Same recovery as the 0-byte case: the corrupt file is contained, the
        rest of the fleet still renders.
        """
        tdir = Path(config.TASKS_DIR)
        (tdir / "trunc.json").write_bytes(b'{"project": {"repo": "/tmp", "ta')
        self._good_taskfile()
        out = self._projects()
        self.assertEqual([p["file"] for p in out], ["good.json"])
        self.assertEqual(out[0]["n_tasks"], 1)

    def test_missing_project_key_lists_empty_project_not_crash(self):
        """Valid JSON without `project` is a shell, not a page-killer.

        The file was probably hand-edited wrong; the page should still show
        it (so the operator can see and fix it) with zero tasks, and the
        healthy project must keep its task.
        """
        tdir = Path(config.TASKS_DIR)
        (tdir / "noproject.json").write_text(json.dumps({"title": "x"}),
                                              encoding="utf-8")
        self._good_taskfile()
        out = self._projects()
        self.assertEqual(sorted(p["file"] for p in out),
                         ["good.json", "noproject.json"])
        by_file = {p["file"]: p for p in out}
        self.assertEqual(by_file["noproject.json"]["n_tasks"], 0)
        self.assertEqual(by_file["good.json"]["n_tasks"], 1)

    def test_non_list_tasks_lists_zero_tasks_not_crash(self):
        """A mangled `tasks` value (string/dict/None) degrades to no tasks.

        Realistic hand-edit damage; iterating a string or dict yields no
        task dicts, which must surface as an empty project while the good
        file keeps exactly its own task — corruption stays contained.
        """
        for bad in ("abc", {"a": 1}, None):
            with self.subTest(bad=bad):
                tdir = Path(config.TASKS_DIR)
                (tdir / "bad.json").write_text(
                    json.dumps({"project": {"repo": "/tmp", "tasks": bad}}),
                    encoding="utf-8")
                self._good_taskfile()
                out = self._projects()
                by_file = {p["file"]: p for p in out}
                self.assertEqual(by_file["bad.json"]["n_tasks"], 0)
                self.assertEqual(by_file["good.json"]["n_tasks"], 1)

    def test_load_taskfile_raises_json_error_for_zero_byte_file(self):
        """The loader must report the empty file as a parse error at line 1.

        The incident: every later gate failed with "Expecting value: line 1
        column 1". That is the contract — a catchable JSONDecodeError
        pointing at the file's start, not a KeyError somewhere deeper or a
        silently empty plan.
        """
        tdir = Path(config.TASKS_DIR)
        f = tdir / "zero.json"
        f.write_bytes(b"")
        with self.assertRaises(json.JSONDecodeError) as caught:
            code_tasks.load_taskfile(str(f))
        self.assertIn("Expecting value", str(caught.exception))

    def test_load_taskfile_raises_for_missing_project_and_non_list_tasks(self):
        """Malformed-but-parseable files must fail loudly, not half-load.

        load_taskfile is the entry to a governed run; returning a plan
        built from a missing `project` or a non-list `tasks` would produce
        a graph with phantom tasks. A precise KeyError/TypeError is the
        report the operator can act on.
        """
        tdir = Path(config.TASKS_DIR)
        no_project = tdir / "no_project.json"
        no_project.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        with self.assertRaises(KeyError):
            code_tasks.load_taskfile(str(no_project))
        bad_tasks = tdir / "bad_tasks.json"
        bad_tasks.write_text(json.dumps({"project": {"repo": "/tmp",
                                                     "tasks": "abc"}}),
                             encoding="utf-8")
        with self.assertRaises(TypeError):
            code_tasks.load_taskfile(str(bad_tasks))


class CorruptStateEventLogTests(unittest.TestCase):
    """logs/events.jsonl is append-only state that merges and crashes mangle.

    A git merge once left conflict markers inside it. Every reader of the
    log must skip lines that do not parse and still return the good events
    — losing the good lines would hide real driver activity (tokens,
    seconds, verdicts) from the operator.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self._orig_tasks = config.TASKS_DIR
        self._orig_log = config.EVENTS_LOG
        config.TASKS_DIR = str(self.tmp / "tasks")
        config.EVENTS_LOG = str(self.tmp / "events.jsonl")
        Path(config.TASKS_DIR).mkdir(parents=True)
        dashboard._lines_cache["key"] = None
        dashboard.Handler.store = Store(":memory:")

    def tearDown(self):
        config.TASKS_DIR = self._orig_tasks
        config.EVENTS_LOG = self._orig_log
        dashboard._lines_cache["key"] = None
        store = dashboard.Handler.store
        dashboard.Handler.store = None
        if store is not None:
            store.conn.close()
        self._dir.cleanup()

    def _write_log(self, lines):
        Path(config.EVENTS_LOG).write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")
        dashboard._lines_cache["key"] = None

    def test_api_events_skips_bad_lines_and_returns_good_events(self):
        """/api/events must answer 200 with only the parseable events.

        The console polls this endpoint; a corrupt line in the middle must
        neither 500 the poll nor stall it. `next` must still advance past
        the raw line count so the poller never re-reads the garbage.
        """
        good_a = {"type": "driver.done", "task": "t1", "tokens": 5,
                  "seconds": 2.0, "harness": "opencode", "model": "GLM-5.3",
                  "role": "implementer", "attempt": 1}
        good_b = {"type": "driver.start", "task": "t1"}
        self._write_log([
            "<<<<<<< HEAD",
            json.dumps(good_a),
            "not json at all {{{",
            "=======",
            json.dumps(good_b),
            ">>>>>>> task/abc",
            "",
        ])
        handler = _FakeHandler()
        handler.path = "/api/events"
        handler.do_GET()
        self.assertEqual(handler.status, 200)
        body = json.loads(handler.body.decode("utf-8"))
        self.assertEqual([e["type"] for e in body["events"]],
                         ["driver.done", "driver.start"])
        self.assertEqual(body["events"][0]["task"], "t1")
        self.assertEqual(body["next"], 7)

    def test_projects_counts_driver_done_despite_corrupt_lines(self):
        """driver.done accounting must survive conflict markers around it.

        The per-task tokens/seconds on the DAG come from driver.done lines;
        if the corrupt lines poisoned the loop the operator would see a
        healthy finished task as 0 tokens / 0 seconds. Both good lines
        among the garbage must still land (5+7 tokens, 2.0+1.5 s).
        """
        tdir = Path(config.TASKS_DIR)
        f = tdir / "good.json"
        f.write_text(json.dumps({"project": {"repo": "/tmp", "title": "G",
                                             "tasks": [GOOD_TASK]}}),
                     encoding="utf-8")
        st = dashboard.Handler.store
        st.upsert_code_task(str(f), "t1", "T", "gpt-oss-120b", "glm",
                            "merged")
        self._write_log([
            "<<<<<<< HEAD",
            json.dumps({"ts": 1, "type": "driver.done", "task": "t1",
                        "tokens": 5, "seconds": 2.0, "harness": "opencode",
                        "model": "GLM-5.3", "role": "implementer",
                        "attempt": 1}),
            "not json at all {{{",
            "=======",
            json.dumps({"ts": 2, "type": "driver.start", "task": "t1"}),
            ">>>>>>> task/abc",
            json.dumps({"ts": 3, "type": "driver.done", "task": "t1",
                        "tokens": 7, "seconds": 1.5,
                        "harness": config.MODEL_HARNESS[STRONGEST],
                        "model": STRONGEST, "role": "implementer",
                        "attempt": 1}),
        ])
        out = dashboard._projects(st)
        node = next(n for n in out[0]["dag"]["nodes"] if n["id"] == "t1")
        self.assertEqual(node["status"], "merged")
        self.assertEqual(node["tokens"], 12)
        self.assertEqual(node["seconds"], 3.5)


class CorruptStateTranscriptTests(unittest.TestCase):
    """Killed harnesses leave damaged transcripts under logs/harness/.

    The dashboard tails these mid-run. An empty file, a final line cut
    mid-JSON, or one giant line must each degrade to "what we can show"
    — never a crash, and never invented usage numbers.
    """

    def test_empty_transcript_yields_no_activity(self):
        """A 0-byte transcript renders as no output, not an exception.

        Killed runs are routine; the tail view must show nothing rather
        than take the run row down with it.
        """
        self.assertEqual(drivers.parse_transcript(""), (None, ""))
        self.assertEqual(drivers.transcript_tokens(""), (0, 0, 0))
        self.assertEqual(drivers.activity_tail(""), [])

    def test_truncated_last_line_falls_back_to_raw_tail(self):
        """A half-written final line still shows its raw text to the operator.

        The `or raw` fallback is the recovery: the last thing the harness
        printed stays visible even though it will not parse. Token usage
        must NOT be invented from it.
        """
        truncated = '{"type":"step_finish","part":{"tokens":{"total":10,input'
        sid, tail = drivers.parse_transcript(truncated)
        self.assertIsNone(sid)
        self.assertEqual(tail, truncated)
        self.assertEqual(drivers.transcript_tokens(truncated), (0, 0, 0))
        self.assertEqual(drivers.activity_tail(truncated), [])

    def test_single_huge_line_is_capped_to_3000_chars(self):
        """A 50 KB single-line transcript must not flood the tail view.

        The tail is capped at 3000 chars and activity_tail yields one
        label for the line — bounded output, not an unbounded dump.
        """
        big = json.dumps({"type": "text", "part": {"text": "x" * 50000}}) + "\n"
        sid, tail = drivers.parse_transcript(big)
        self.assertIsNone(sid)
        self.assertEqual(tail, "x" * 3000)
        self.assertEqual(drivers.activity_tail(big), ["text"])
        self.assertEqual(drivers.transcript_tokens(big), (0, 0, 0))

    def test_usage_and_activity_read_from_intact_lines_among_garbage(self):
        """One intact step_finish line among garbage still reports its usage.

        Token accounting and the activity feed must recover whatever is
        parseable — skipping the garbage, keeping the order of the rest.
        """
        usage = json.dumps({"type": "step_finish",
                            "part": {"tokens": {"total": 10, "input": 4,
                                                "output": 6}}})
        text = json.dumps({"type": "text", "part": {"text": "hello"}})
        raw = ("garbage line {\n" + usage + "\n<<<<<<< HEAD\n" + text
               + "\n>>>>>>> task/abc\n")
        self.assertEqual(drivers.transcript_tokens(raw), (10, 4, 6))
        self.assertEqual(drivers.activity_tail(raw),
                         ["step_finish", "text"])


class CorruptStateStoreDirTests(unittest.TestCase):
    """A db path whose parent directory does not exist must fail cleanly.

    The failure must be a specific sqlite3.OperationalError and leave NO
    partial directory or file behind — a half-created db would make the
    next start appear to work while writing state to the wrong place.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_missing_parent_dir_raises_clean_operational_error(self):
        """Construction fails with 'unable to open database file' and no residue."""
        db = self.tmp / "no" / "such" / "dir" / "t.db"
        with self.assertRaises(sqlite3.OperationalError) as caught:
            Store(str(db))
        self.assertIn("unable to open database file", str(caught.exception))
        self.assertFalse((self.tmp / "no").exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_store_recovers_once_parent_dir_created(self):
        """After mkdir the same path opens and persists normally.

        Recovery is one mkdir away — the operator's next action after the
        clean error. A roundtrip write/read proves the store is usable,
        not just openable.
        """
        db = self.tmp / "deep" / "dir" / "t.db"
        db.parent.mkdir(parents=True)
        store = Store(str(db))
        self.addCleanup(store.conn.close)
        store.upsert_code_task("tf.json", "t1", "T", "GLM-5.3",
                               config.cross_family_reviewer("GLM-5.3"), "merged")
        rows = store.code_tasks_all()
        self.assertEqual([(r["id"], r["status"]) for r in rows],
                         [("t1", "merged")])


class CorruptStateConcurrentStoreTests(unittest.TestCase):
    """The dashboard and a live `code run` hold separate Stores on one db.

    Both write (dashboard archives/updates, the run upserts tasks and
    harness runs) at the same time. A lost write means a merged task
    invisible to the operator. Every row from both writers must land and
    be visible to a fresh reader.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_two_store_instances_writing_from_threads_all_rows_land(self):
        """30 interleaved writes per instance survive with zero errors.

        A generous busy timeout on both writers keeps sqlite's 5 s
        default from failing this test spuriously when the box is
        under heavy fleet load.
        """
        db = self.tmp / "cc.db"
        s1, s2 = Store(str(db)), Store(str(db))
        for s in (s1, s2):
            s.conn.execute("PRAGMA busy_timeout=60000")
        self.addCleanup(s1.conn.close)
        self.addCleanup(s2.conn.close)
        errors = []

        def write(store, tag):
            try:
                for i in range(30):
                    store.upsert_code_task("tf.json", f"{tag}{i}", "T",
                                           "GLM-5.3",
                                           config.cross_family_reviewer("GLM-5.3"),
                                           "merged")
                    store.save_harness_run(f"{tag}{i}", "opencode", "GLM-5.3",
                                           "implementer", 1, 0, "x.jsonl",
                                           1.0)
            except Exception as exc:  # surfaced by the assertions below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(s1, "a")),
                   threading.Thread(target=write, args=(s2, "b"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        reader = Store(str(db))
        self.addCleanup(reader.conn.close)
        task_rows = reader.code_tasks_all()
        run_rows = reader.harness_runs_all()
        self.assertEqual(len(task_rows), 60)
        self.assertEqual({r["id"] for r in task_rows},
                         {f"{tag}{i}" for tag in "ab" for i in range(30)})
        self.assertEqual(len(run_rows), 60)


if __name__ == "__main__":
    unittest.main()