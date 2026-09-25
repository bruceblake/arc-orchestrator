"""Pagination and conditional-refresh services.

These are pure: no event log, no git, no dashboard handler. The HTTP
wiring is covered by the activity offset test and by not_modified's
contract (a missing If-None-Match is never a 304).
"""

import unittest

import config
import dashboard
from dashboard_services.page import apply, requested
from dashboard_services.refresh import etag_for, not_modified, revision


class PageTests(unittest.TestCase):
    def test_absent_params_mean_the_full_list(self):
        self.assertIsNone(requested({}, default_limit=20, max_limit=50))
        self.assertIsNone(requested({"range": ["1h"]}, default_limit=20, max_limit=50))

    def test_limit_and_offset_are_clamped(self):
        self.assertEqual(requested({"limit": ["999"], "offset": ["-3"]},
                                   default_limit=10, max_limit=20), (20, 0))
        self.assertEqual(requested({"limit": ["abc"]},
                                   default_limit=10, max_limit=20), (10, 0))
        self.assertEqual(requested({"offset": ["5"]},
                                   default_limit=10, max_limit=20), (10, 5))

    def test_apply_slices_without_mutating(self):
        src = {"prs": [{"n": i} for i in range(5)], "ready": True}
        out = apply(src, "prs", 2, 2)
        self.assertEqual([p["n"] for p in src["prs"]], [0, 1, 2, 3, 4])
        self.assertEqual([p["n"] for p in out["prs"]], [2, 3])
        self.assertEqual(out["page"], {
            "key": "prs", "limit": 2, "offset": 2, "total": 5, "next_offset": 4,
        })
        self.assertNotIn("page", src)

    def test_last_page_has_no_next_offset(self):
        out = apply({"prs": [1, 2, 3]}, "prs", 10, 0)
        self.assertEqual(out["prs"], [1, 2, 3])
        self.assertIsNone(out["page"]["next_offset"])


class RefreshTests(unittest.TestCase):
    def test_clock_fields_do_not_change_the_etag(self):
        a = {"now": 1, "ts": 1, "prs": [{"n": 1, "idle_s": 4, "seconds": 9}]}
        b = {"now": 99, "ts": 99, "prs": [{"n": 1, "idle_s": 40, "seconds": 90}]}
        self.assertEqual(revision(a), revision(b))
        self.assertFalse(not_modified(None, a))
        self.assertTrue(not_modified(etag_for(a), b))

    def test_a_real_change_misses(self):
        a = {"now": 1, "prs": [{"n": 1}]}
        b = {"now": 1, "prs": [{"n": 2}]}
        self.assertNotEqual(etag_for(a), etag_for(b))
        self.assertFalse(not_modified(etag_for(a), b))

    def test_elapsed_time_does_not_change_the_etag(self):
        a = {"agents": [{"task": "t", "started": 10, "elapsed_s": 1}]}
        b = {"agents": [{"task": "t", "started": 10, "elapsed_s": 40}]}
        self.assertEqual(revision(a), revision(b))
        c = {"agents": [{"task": "t", "started": 10, "elapsed_s": 40, "stalled": True}]}
        self.assertNotEqual(revision(a), revision(c))

    def test_nested_event_timestamps_stay_in_the_hash(self):
        a = {"events": [{"type": "task.merged", "ts": 10}]}
        b = {"events": [{"type": "task.merged", "ts": 11}]}
        self.assertNotEqual(revision(a), revision(b))


class _Resp(dashboard.Handler):
    def __init__(self, inm=None):
        self.headers = {"If-None-Match": inm} if inm else {}
        self.status = None
        self.sent = []
        self.chunks = []
        self.wfile = self

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, name, value):
        self.sent.append((name, value))

    def end_headers(self):
        pass

    def write(self, chunk):
        self.chunks.append(chunk)


class ConditionalJsonTests(unittest.TestCase):
    def test_matching_etag_is_304_with_no_body(self):
        payload = {"ready": True, "now": 1, "prs": [{"n": 1}]}
        tag = etag_for(payload)
        hit = _Resp(tag)
        hit._json(payload, conditional=True)
        self.assertEqual(hit.status, 304)
        self.assertEqual(hit.chunks, [])

    def test_no_validator_is_the_full_body(self):
        payload = {"ready": True, "now": 1}
        miss = _Resp()
        miss._json(payload, conditional=True)
        self.assertEqual(miss.status, 200)
        self.assertIn(b'"ready": true', b"".join(miss.chunks))
        self.assertIn(("ETag", etag_for(payload)), miss.sent)


class _Get(dashboard.Handler):
    def __init__(self, path):
        self.path = path
        self.status = None
        self.chunks = []
        self.wfile = self

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass

    def write(self, chunk):
        self.chunks.append(chunk)


class PanelScriptRouteTests(unittest.TestCase):
    def test_a_panel_name_with_an_underscore_is_served(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        panel = root / "static" / "panels"
        panel.mkdir(parents=True)
        (panel / "work_status.js").write_text(
            "function pollWorkStatus(){}\n", encoding="utf-8")
        old = config.ROOT
        config.ROOT = root
        self.addCleanup(setattr, config, "ROOT", old)
        req = _Get("/panels/work_status.js")
        req.do_GET()
        self.assertEqual(req.status, 200)
        self.assertIn(b"pollWorkStatus", b"".join(req.chunks))

    def test_a_panel_path_cannot_leave_the_static_tree(self):
        req = _Get("/panels/../../etc/passwd")
        req.do_GET()
        self.assertEqual(req.status, 404)


if __name__ == "__main__":
    unittest.main()
