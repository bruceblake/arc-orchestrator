"""Human checkpoints (manual_review.py, review_routes.py) and the playtest
fixes that make "play this build" work from the dashboard.

What is pinned here, each with a test that fails without the change:

  * taskfile `human_review` (project and per task) is validated by the loader
    and resolves over ARC_PR_MANUAL_REVIEW;
  * a decision made in the dashboard reaches code_tasks._await_manual_review
    (approve -> approved; reject -> the comment is the implementer's issue);
  * the decide route acts only on a waiting hold, goes through _refuse_post,
    and demands a comment to request changes;
  * GET /api/reviews carries evidence URLs and the play target;
  * a launch may pick a scene, but only one of that build's own scenes;
  * with no DISPLAY (systemd), config falls back to the local X server :0.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import capture_events, ENTRY, ENTRY_REVIEWER  # noqa: F401  (sys.path)

import config  # noqa: E402
import code_tasks  # noqa: E402
import dashboard  # noqa: E402
import gitstore  # noqa: E402
import manual_review  # noqa: E402
import review_routes  # noqa: E402
from studio import playtest  # noqa: E402
from test_http_write import _FakeRequest  # noqa: E402
from test_studio_playtest import PlaytestCase, _Sleeper, git  # noqa: E402


class _DB(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self._old_db = config.DB_PATH
        config.DB_PATH = str(Path(self._d.name) / "t.db")

    def tearDown(self):
        config.DB_PATH = self._old_db
        self._d.cleanup()


class TestTaskfileHumanReview(unittest.TestCase):
    def _load(self, project_extra=None, task_extra=None):
        with tempfile.TemporaryDirectory() as d:
            task = {"id": "a", "prompt": "p", "model": ENTRY, "reviewer": ENTRY_REVIEWER}
            task.update(task_extra or {})
            proj = {"name": "g", "repo": d, "tasks": [task, dict(task, id="b")]}
            proj.update(project_extra or {})
            f = Path(d) / "t.json"
            f.write_text(json.dumps({"project": proj}))
            return code_tasks.load_taskfile(f)

    def test_absent_means_the_global_default_decides(self):
        ts = self._load()
        self.assertIsNone(ts["tasks"]["a"]["human_review"])
        with mock.patch.object(config, "PR_MANUAL_REVIEW", True):
            self.assertTrue(manual_review.wanted(ts["tasks"]["a"]))
        with mock.patch.object(config, "PR_MANUAL_REVIEW", False):
            self.assertFalse(manual_review.wanted(ts["tasks"]["a"]))

    def test_project_flag_applies_to_every_task_and_a_task_overrides_it(self):
        ts = self._load({"human_review": True}, None)
        self.assertTrue(ts["human_review"])
        with mock.patch.object(config, "PR_MANUAL_REVIEW", False):
            self.assertTrue(manual_review.wanted(ts["tasks"]["b"]))
        ts = self._load({"human_review": True}, {"human_review": False})
        with mock.patch.object(config, "PR_MANUAL_REVIEW", True):
            self.assertFalse(manual_review.wanted(ts["tasks"]["a"]))
        self.assertEqual(ts["name"], "g")

    def test_non_boolean_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "project.human_review"):
            self._load({"human_review": "yes"})
        with self.assertRaisesRegex(ValueError, "task a: human_review"):
            self._load(None, {"human_review": 1})


class TestStore(_DB):
    def test_request_decide_settle(self):
        manual_review.request("t", 7, 1, project="g", reviewers=[{"model": "m", "approve": True}])
        self.assertEqual([r["task"] for r in manual_review.waiting()], ["t"])
        self.assertTrue(manual_review.waiting()[0]["live"])     # this pid
        manual_review.decide("t", 7, 1, "reject", comment="clamp the door")
        self.assertEqual(manual_review.waiting(), [])
        with self.assertRaises(KeyError):                      # only a waiting hold
            manual_review.decide("t", 7, 1, "approve")
        # Decided while no run was listening: a resumed run gets the answer.
        again = manual_review.request("t", 7, 1)
        self.assertEqual((again["status"], again["comment"]), ("rejected", "clamp the door"))
        manual_review.settle("t", 7, 1, "rejected")
        self.assertEqual(manual_review.request("t", 7, 1)["status"], "waiting")
        with self.assertRaises(ValueError):
            manual_review.decide("t", 7, 1, "merge")


class TestAwaitSeesDashboardDecision(_DB):
    def _run(self, decision, comment=""):
        calls = []

        async def fake_gh(args, cwd, timeout=180):
            calls.append(args)
            if args[:2] == ["pr", "view"]:
                # The human clicks while the fleet is waiting.
                if manual_review.get("t", 7, 1)["status"] == "waiting":
                    manual_review.decide("t", 7, 1, decision, comment=comment)
                return 0, json.dumps({"labels": [], "comments": []}), ""
            return 0, "", ""
        with mock.patch.object(gitstore, "_gh", fake_gh), \
                mock.patch.object(config, "PR_MANUAL_POLL", 0), capture_events() as evs:
            out = asyncio.run(code_tasks._await_manual_review(
                "/repo", "t", 7, 1, project="g", url="https://github.com/o/r/pull/7"))
        return out, calls, evs

    def test_dashboard_approve_merges(self):
        out, calls, evs = self._run("approve")
        self.assertEqual(out["decision"], "approved")
        self.assertEqual(evs.of("task.pr_manual")[0]["via"], "dashboard")
        self.assertTrue(manual_review.get("t", 7, 1)["consumed"])
        self.assertTrue(any("approved by a human" in " ".join(c) for c in calls))

    def test_dashboard_reject_hands_the_comment_to_the_implementer(self):
        out, calls, _ = self._run("reject", "the guard walks through walls")
        self.assertEqual(out, {"decision": "rejected",
                               "issues": ["the guard walks through walls"]})
        # The PR comment recording it must not be read back as new feedback.
        body = [c for c in calls if c[:2] == ["pr", "comment"]][-1][-1]
        self.assertTrue(code_tasks._is_fleet_comment(body))


class TestRoutes(_DB):
    def setUp(self):
        super().setUp()
        manual_review.request("t", 7, 1, project="nogame", title="Doors",
                              url="https://github.com/o/r/pull/7")

    def _post(self, obj, headers=None):
        req = _FakeRequest("/api/reviews/decide", json.dumps(obj).encode(), headers=headers)
        req.do_POST()
        return req.status, json.loads(req.body)

    def test_get_lists_waiting_with_play_and_evidence(self):
        req = _FakeRequest("/api/reviews")
        req.do_GET()
        body = json.loads(req.body)
        self.assertEqual(req.status, 200)
        (item,) = body["waiting"]
        self.assertEqual((item["task"], item["pr"], item["title"]), ("t", 7, "Doors"))
        self.assertFalse(item["play"]["available"])
        self.assertIn("studio", item["play"]["reason"])

    def test_decide_validation(self):
        self.assertEqual(self._post({"task": "t", "pr": 7, "round": 1,
                                     "decision": "reject", "comment": " "})[0], 400)
        self.assertEqual(self._post({"task": "t", "pr": "7", "round": 1,
                                     "decision": "approve"})[0], 400)
        self.assertEqual(self._post({"task": "../x", "pr": 7, "round": 1,
                                     "decision": "approve"})[0], 404)
        code, body = self._post({"task": "t", "pr": 7, "round": 1,
                                 "decision": "reject", "comment": "fix it"})
        self.assertEqual(code, 200, body)
        self.assertEqual(manual_review.get("t", 7, 1)["comment"], "fix it")
        self.assertEqual(self._post({"task": "t", "pr": 7, "round": 1,
                                     "decision": "approve"})[0], 404)

    def test_decide_rejects_json_booleans_for_pr_and_round(self):
        """True == 1 in Python, so a boolean would match a round-1 hold."""
        for pr, rnd in ((True, 1), (7, True), (7, 1.0), (7, None)):
            code, _ = self._post({"task": "t", "pr": pr, "round": rnd,
                                  "decision": "approve"})
            self.assertEqual(code, 400, (pr, rnd))
        self.assertEqual(manual_review.get("t", 7, 1)["status"], "waiting")

    def test_decide_goes_through_refuse_post(self):
        code, _ = self._post({"task": "t", "pr": 7, "round": 1, "decision": "approve"},
                             headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        with mock.patch.object(config, "DASHBOARD_TOKEN", "s3cret"):
            code, _ = self._post({"task": "t", "pr": 7, "round": 1, "decision": "approve"})
        self.assertEqual(code, 401)
        self.assertEqual(manual_review.get("t", 7, 1)["status"], "waiting")

    def test_evidence_is_served_as_urls(self):
        with tempfile.TemporaryDirectory() as d:
            att = Path(d) / "nogame" / "t" / "x2"
            att.mkdir(parents=True)
            for f in ("flythrough.gif", "flythrough.mp4", "contact_sheet.png", "cmp.png"):
                (att / f).write_bytes(b"x")
            (att / "manifest.json").write_text(json.dumps({
                "shots": [], "videos": {"flythrough": {"gif": str(att / "flythrough.gif"),
                                                       "mp4": str(att / "flythrough.mp4")}},
                "compare": [{"name": "cam", "changed": 0.2, "side_by_side": str(att / "cmp.png")},
                            {"name": "evil", "side_by_side": "/etc/passwd"}],
                "warnings": ["w"]}))
            with mock.patch.object(config, "EVIDENCE_DIR", Path(d)):
                ev = review_routes.queue()["waiting"][0]["evidence"]
        self.assertEqual(ev["attempt"], "x2")
        self.assertTrue(ev["videos"]["flythrough"]["gif"].startswith("/api/evidence-file?path="))
        self.assertEqual([c["name"] for c in ev["compare"]], ["cam"])   # outside root dropped
        self.assertIsNotNone(ev["contact_sheet"])

    def test_evidence_url_quotes_the_path(self):
        """A shot name with '#', a space or '"' must survive the query string
        and never close the HTML attribute it is rendered into."""
        from urllib.parse import parse_qs, urlparse
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            f = root / "game" / "t" / "x1" / 'cam #2 "x".png'
            url = dashboard._evidence_url(root, f)
        for ch in ('#', ' ', '"'):
            self.assertNotIn(ch, url)
        self.assertEqual(parse_qs(urlparse(url).query)["path"],
                         ['game/t/x1/cam #2 "x".png'])


class TestScenesAndDisplay(PlaytestCase):
    def setUp(self):
        super().setUp()
        git(self.repo, "checkout", "-q", "task/foo")
        (self.repo / "scenes").mkdir()
        (self.repo / "scenes" / "prison.tscn").write_text("[gd_scene format=3]\n")
        pg = (self.repo / "project.godot").read_text()
        (self.repo / "project.godot").write_text(
            pg + '\n[application]\nrun/main_scene="res://scenes/world.tscn"\n')
        (self.repo / "scenes" / "world.tscn").write_text("[gd_scene format=3]\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "scenes")
        git(self.repo, "checkout", "-q", "main")
        self.foo_sha = git(self.repo, "rev-parse", "task/foo")
        playtest._BUILD_CACHE.clear()

    def test_builds_list_scenes_main_first(self):
        foo = [b for b in playtest.builds("game") if b["id"] == "task/foo"][0]
        self.assertEqual(foo["main_scene"], "res://scenes/world.tscn")
        self.assertEqual(foo["scenes"], ["res://scenes/world.tscn", "res://scenes/prison.tscn"])

    def test_launch_scene_is_allowlisted_and_reaches_argv(self):
        fake = str(self.repo / "godot")
        sleeper = _Sleeper(fake)
        with mock.patch("studio.engine.godot.godot_bin", return_value=fake), \
                mock.patch.object(playtest, "_import", lambda snap: None), \
                mock.patch.object(playtest.subprocess, "Popen", sleeper), capture_events():
            obj, code = dashboard._playtest_launch(
                {"project": "game", "build": "task/foo", "scene": "res://../../etc/x.tscn"})
            self.assertEqual(code, 404)
            obj, code = dashboard._playtest_launch(
                {"project": "game", "build": "task/foo", "scene": "res://scenes/prison.tscn"})
            self.assertEqual(code, 200, obj)
            # The dashboard launches with wait=False: the snapshot is made on a
            # thread that must finish before teardown removes its directory.
            for t in list(playtest._PREPARING.values()):
                t.join(30)
            s = playtest.launch("game", "task/foo", scene="res://scenes/prison.tscn")
        self.assertEqual(s["scene"], "res://scenes/prison.tscn")
        argv = sleeper.calls[-1][0]
        self.assertIn("res://scenes/prison.tscn", argv)
        self.assertLess(argv.index("res://scenes/prison.tscn"), argv.index("--"))
        # The reused snapshot carries the lit overlay (#139).
        snap = playtest._snapshot_dir("game", self.foo_sha)
        self.assertIn("PlaytestInspectionSun",
                      (snap / playtest.OVERLAY_DIR / "overlay.gd").read_text())
        for p in list(playtest._PROCS.values()):
            p.kill()


class TestDisplayFallback(unittest.TestCase):
    def test_systemd_without_display_uses_local_x0(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "ARC_STUDIO_DISPLAY")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("os.path.exists", lambda p: p == config.X0_SOCKET):
            self.assertEqual(config._studio_display(), ":0")
        with mock.patch.dict(os.environ, dict(env, ARC_STUDIO_DISPLAY="none"), clear=True), \
                mock.patch("os.path.exists", lambda p: True):
            self.assertEqual(config._studio_display(), "")
        with mock.patch.dict(os.environ, dict(env, DISPLAY=":5"), clear=True):
            self.assertEqual(config._studio_display(), ":5")

    def test_a_played_build_does_not_inherit_the_dashboard_token(self):
        """The game is agent-written code started by the dashboard."""
        from studio import playtest
        with mock.patch.dict(os.environ, {"ARC_DASHBOARD_TOKEN": "s3cret"}):
            env = playtest.play_env(":0", "/tmp/ud")
        self.assertNotIn("ARC_DASHBOARD_TOKEN", env)
        self.assertEqual((env["DISPLAY"], env["XDG_DATA_HOME"]), (":0", "/tmp/ud"))


if __name__ == "__main__":
    unittest.main()
