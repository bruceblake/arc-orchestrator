"""Human playtesting (studio/playtest.py) and its dashboard routes.

What these tests pin, in order of how much damage a regression would do:

  ISOLATION. A build snapshot is a detached clone under the studio run dir.
  The blessed repo must come out untouched — still ONE worktree (a second one
  holding task/<id> would break gitstore.alloc) and a clean status — and the
  overlay must land in the snapshot's project.godot only.

  RULE 6b. Every dashboard route acts only on values the server listed. Each
  allowlist gets its own rejection test, including path traversal on the
  screenshot route. Godot never actually starts: launch is exercised with
  Popen redirected to a harmless sleeper.

  ONE WAY TO THE FLEET. Only accepted and reopened findings reach the planner.
"""
import json
import time
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import capture_events  # noqa: F401  (sets sys.path)

import config  # noqa: E402
import dashboard  # noqa: E402
from studio import playtest  # noqa: E402
from studio.engine import stage_manager  # noqa: E402
from studio.schemas.task import PHASES  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
       "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run(GIT + ["-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class PlaytestCase(unittest.TestCase):
    """A studio project "game" whose repo has main and task/foo."""

    def setUp(self):
        self._dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        self.studio, self.tasks, self.repo = (Path(d.name) for d in self._dirs)
        self._old = (config.STUDIO_DIR, config.TASKS_DIR, config.STUDIO_DISPLAY)
        config.STUDIO_DIR, config.TASKS_DIR = self.studio, str(self.tasks)
        config.STUDIO_DISPLAY = ":99"
        playtest._BUILD_CACHE.clear()
        subprocess.run(GIT + ["init", "-q", str(self.repo)], check=True)
        (self.repo / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="game"\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "checkout", "-qb", "task/foo")
        (self.repo / "foo.gd").write_text("extends Node\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "task(foo): add foo")
        git(self.repo, "checkout", "-q", "main")
        self.main_sha = git(self.repo, "rev-parse", "main")
        self.foo_sha = git(self.repo, "rev-parse", "task/foo")
        stage_manager.promote("game", str(self.repo), force=True)
        (self.tasks / "game-phase_1.json").write_text(json.dumps(
            {"project": {"name": "game", "repo": str(self.repo), "tasks": []}}))

    def tearDown(self):
        for proc in list(playtest._PROCS.values()):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        playtest._PROCS.clear()
        config.STUDIO_DIR, config.TASKS_DIR, config.STUDIO_DISPLAY = self._old
        playtest._BUILD_CACHE.clear()
        for d in self._dirs:
            d.cleanup()

    def session(self, sid="20260923-120000-abcd", build="main", sha=None,
                status="ended", lines=()):
        d = playtest._sessions_dir("game") / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "session.json").write_text(json.dumps(
            {"id": sid, "build": build, "sha": sha or self.main_sha,
             "started": 1.0, "ended": 2.0, "pid": None, "status": status,
             "survey": None}))
        if lines:
            (d / "findings.jsonl").write_text("".join(l + "\n" for l in lines))
        return sid


class TestBuilds(PlaytestCase):
    def test_base_first_then_task_branches(self):
        rows = playtest.builds("game")
        self.assertEqual([b["id"] for b in rows], ["main", "task/foo"])
        self.assertEqual(rows[0]["sha"], self.main_sha)
        self.assertEqual(rows[1]["sha"], self.foo_sha)
        self.assertEqual(rows[1]["subject"], "task(foo): add foo")
        self.assertFalse(any(b["snapshot"] for b in rows))

    def test_unknown_project_has_no_builds(self):
        self.assertEqual(playtest.builds("nope"), [])


class TestSnapshot(PlaytestCase):
    def test_snapshot_is_detached_isolated_and_carries_the_overlay(self):
        before = (self.repo / "project.godot").read_text()
        snap = playtest.ensure_snapshot("game", "task/foo", do_import=False)
        self.assertEqual(snap, self.studio / "game" / "playtest" / "builds" / self.foo_sha[:12])
        self.assertTrue((snap / playtest.READY_MARKER).exists())
        self.assertTrue((snap / "foo.gd").exists())
        self.assertEqual(git(snap, "rev-parse", "HEAD"), self.foo_sha)
        self.assertNotEqual(subprocess.run(["git", "-C", str(snap), "symbolic-ref", "-q", "HEAD"],
                                           capture_output=True).returncode, 0,
                            "the snapshot must be on a detached HEAD")
        self.assertEqual(git(snap, "remote"), "", "a snapshot must not push back")
        pg = (snap / "project.godot").read_text()
        self.assertIn('[autoload]\n\nArcPlaytest="*res://arc_playtest/overlay.gd"', pg)
        self.assertTrue((snap / "arc_playtest" / "overlay.gd").is_file())
        # The blessed repo: untouched.
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual((self.repo / "project.godot").read_text(), before)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue(next(b for b in playtest.builds("game")
                             if b["id"] == "task/foo")["snapshot"])

    def test_snapshot_is_reused_and_a_half_made_one_is_rebuilt(self):
        snap = playtest.ensure_snapshot("game", "main", do_import=False)
        (snap / "keep.txt").write_text("x")
        self.assertEqual(playtest.ensure_snapshot("game", "main", do_import=False), snap)
        self.assertTrue((snap / "keep.txt").exists(), "a ready snapshot is reused")
        (snap / playtest.READY_MARKER).unlink()
        playtest.ensure_snapshot("game", "main", do_import=False)
        self.assertFalse((snap / "keep.txt").exists(), "no marker = rebuilt")
        self.assertTrue((snap / playtest.READY_MARKER).exists())

    def test_unknown_build_is_refused(self):
        with self.assertRaises(KeyError):
            playtest.ensure_snapshot("game", "task/nope", do_import=False)

    def test_overlay_joins_an_existing_autoload_section(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "project.godot").write_text(
                'config_version=5\n\n[autoload]\n\nNet="*res://net.gd"\n\n[display]\n')
            playtest.inject_overlay(d)
            playtest.inject_overlay(d)               # idempotent
            text = (Path(d) / "project.godot").read_text()
        self.assertEqual(text.count("ArcPlaytest="), 1)
        self.assertEqual(text.count("[autoload]"), 1)
        self.assertIn('Net="*res://net.gd"', text)


class TestFindings(PlaytestCase):
    LINE = json.dumps({"ts": 5.0, "note": "fell through floor", "category": "bug",
                       "severity": 1, "scene": "res://main.tscn",
                       "screenshot": "shot-0.png", "game_time": 3.2, "fps": 60})

    def test_ingest_is_idempotent_and_waits_for_a_whole_line(self):
        sid = self.session(lines=[self.LINE, json.dumps(
            {"note": "x", "category": "weird", "severity": 9,
             "screenshot": "../../etc/passwd.png"})])
        f = playtest._sessions_dir("game") / sid / "findings.jsonl"
        with f.open("a") as fh:
            fh.write('{"note": "partial"')
        self.assertEqual(playtest.ingest("game", sid), 2)
        self.assertEqual(playtest.ingest("game", sid), 0)
        store = playtest.load_findings("game")
        self.assertEqual(len(store), 2)
        by_note = {x["note"]: x for x in store.values()}
        first = by_note["fell through floor"]
        self.assertEqual((first["source"], first["severity"], first["category"],
                          first["screenshot"], first["sha"], first["state"]),
                         ("game", 1, "bug", "shot-0.png", self.main_sha, "new"))
        odd = by_note["x"]
        self.assertEqual((odd["category"], odd["severity"], odd["screenshot"]),
                         ("other", 3, None))
        with f.open("a") as fh:
            fh.write(', "category": "ux", "severity": 2}\n')
        self.assertEqual(playtest.ingest("game", sid), 1)
        self.assertEqual(len(playtest.load_findings("game")), 3)

    def test_triage_transitions(self):
        f = playtest.add_finding("game", category="feel", severity=2, note="floaty jump")
        fid = f["id"]
        self.assertRegex(fid, r"^f-[0-9a-f]{8}$")
        self.assertEqual((f["build"], f["sha"], f["session"]), ("main", self.main_sha, None))
        for bad in ("verified", "reopened", "new", "bogus"):
            with self.assertRaises(ValueError, msg=bad):
                playtest.triage("game", fid, bad)
        with capture_events() as evs:
            playtest.triage("game", fid, "accepted", note="yes")
        self.assertEqual(evs.of("playtest.triage")[0]["to"], "accepted")
        with self.assertRaises(ValueError):
            playtest.triage("game", fid, "accepted")         # no-op move
        f = playtest.triage("game", fid, "fixed", link="task/foo")
        self.assertEqual((f["fixed_in"], f["link"]), (self.main_sha, "task/foo"))
        f = playtest.triage("game", fid, "verified")
        f = playtest.triage("game", fid, "reopened", note="back again")
        self.assertIsNone(f["fixed_in"])
        self.assertEqual([h["state"] for h in f["history"]],
                         ["new", "accepted", "fixed", "verified", "reopened"])
        with self.assertRaises(KeyError):
            playtest.triage("game", "f-00000000", "accepted")

    def test_add_finding_validates(self):
        for kw in ({"category": "nope", "severity": 1, "note": "n"},
                   {"category": "bug", "severity": 5, "note": "n"},
                   {"category": "bug", "severity": True, "note": "n"},
                   {"category": "bug", "severity": 1, "note": "  "}):
            with self.assertRaises(ValueError, msg=kw):
                playtest.add_finding("game", **kw)
        sid = self.session(build="task/foo", sha=self.foo_sha)
        f = playtest.add_finding("game", category="ux", severity=3, note="n", session=sid)
        self.assertEqual((f["build"], f["sha"], f["session"]), ("task/foo", self.foo_sha, sid))

    def test_survey_validation(self):
        sid = self.session()
        for bad in (0, 6, "3", True, None, 2.5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                playtest.survey("game", sid, fun=bad, clarity=3, difficulty=3)
        s = playtest.survey("game", sid, fun=5, clarity=4, difficulty=2, note="fun")
        self.assertEqual((s["survey"]["fun"], s["survey"]["note"]), (5, "fun"))
        with self.assertRaises(KeyError):
            playtest.survey("game", "20260101-000000-0000", fun=1, clarity=1, difficulty=1)

    def test_only_accepted_and_reopened_reach_the_planner(self):
        ids = {}
        for sev, note, state in ((3, "minor thing", "accepted"), (1, "blocker", "accepted"),
                                 (2, "untriaged", None), (2, "nah", "wontfix"),
                                 (4, "came back", "reopened")):
            fid = playtest.add_finding("game", category="bug", severity=sev, note=note)["id"]
            ids[note] = fid
            if state == "reopened":
                playtest.triage("game", fid, "wontfix")
            if state:
                playtest.triage("game", fid, state)
        rows = playtest.open_for_planner("game")
        self.assertEqual([r["note"] for r in rows], ["blocker", "minor thing", "came back"])
        block = playtest.planner_block("game")
        self.assertIn("--- OPEN HUMAN PLAYTEST FINDINGS", block)
        self.assertIn(f"- [sev 1 bug] blocker ({self.main_sha[:7]})", block)
        self.assertNotIn("untriaged", block)
        self.assertNotIn("nah", block)
        self.assertEqual(playtest.planner_block("empty-project"), "")


class TestPlannerHook(PlaytestCase):
    def _plan_goal(self):
        from studio import planner
        seen = {}

        def chat(model, messages, **kw):
            seen["goal"] = messages[-1]["content"]
            raise RuntimeError("stop here")
        with mock.patch.object(config, "STUDIO_API", True), \
                mock.patch.object(planner.openrouter, "chat", chat):
            with self.assertRaisesRegex(RuntimeError, "stop here"):
                planner.plan("build the yard", str(self.repo), phase=PHASES[1],
                             project="game", model=config.PLANNER_MODEL)
        return seen["goal"]

    def test_accepted_findings_are_appended_to_the_goal(self):
        fid = playtest.add_finding("game", category="feel", severity=2, note="floaty")["id"]
        self.assertEqual(self._plan_goal(), "build the yard", "new findings stay out")
        playtest.triage("game", fid, "accepted")
        goal = self._plan_goal()
        self.assertTrue(goal.startswith("build the yard\n\n--- OPEN HUMAN PLAYTEST"))
        self.assertIn("[sev 2 feel] floaty", goal)

    def test_a_broken_findings_store_never_breaks_planning(self):
        with mock.patch.object(playtest, "planner_block", side_effect=OSError("disk")):
            self.assertEqual(self._plan_goal(), "build the yard")


_REAL_POPEN = subprocess.Popen


class _Sleeper:
    """Popen stand-in: records the Godot argv/env it was given and runs a
    harmless sleeper instead, in its own session like the real launch. Every
    other command (git) passes straight through."""

    def __init__(self, godot):
        self.godot, self.calls = godot, []

    def __call__(self, argv, *a, **kw):
        if not argv or argv[0] != self.godot:
            return _REAL_POPEN(argv, *a, **kw)
        self.calls.append((argv, kw))
        kw = dict(kw)
        kw.pop("cwd", None)
        return _REAL_POPEN([sys.executable, "-c", "import time; time.sleep(60)"], **kw)


class TestLaunchAndRoutes(PlaytestCase):
    def setUp(self):
        super().setUp()
        self.fake_godot = str(self.repo / "godot")
        self._patches = [mock.patch("studio.engine.godot.godot_bin",
                                    return_value=self.fake_godot),
                         mock.patch.object(playtest, "_import", lambda snap: None)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    def test_launch_refuses_without_godot_or_display(self):
        with mock.patch("studio.engine.godot.godot_bin", return_value=None):
            obj, code = dashboard._playtest_launch({"project": "game", "build": "main"})
        self.assertEqual(code, 409)
        self.assertIn("Godot", obj["error"])
        config.STUDIO_DISPLAY = ""
        obj, code = dashboard._playtest_launch({"project": "game", "build": "main"})
        self.assertEqual(code, 409)
        self.assertIn("display", obj["error"])
        self.assertEqual(playtest.session_ids("game"), [])

    def test_launch_argv_env_session_and_stop(self):
        sleeper = _Sleeper(self.fake_godot)
        with mock.patch.object(playtest.subprocess, "Popen", sleeper), \
                capture_events() as evs:
            obj, code = dashboard._playtest_launch({"project": "game", "build": "task/foo"})
            self.assertEqual(code, 200, obj)
            s = obj["session"]
            # First play of this sha: the route answers at once and the clone
            # runs on a thread, so no request waits on an import.
            self.assertEqual(s["status"], "preparing")
            t = playtest._PREPARING.get(s["id"])
            if t is not None:
                t.join(60)
        self.assertRegex(s["id"], r"^\d{8}-\d{6}-[0-9a-f]{4}$")
        sess = playtest._sessions_dir("game") / s["id"]
        s = json.loads((sess / "session.json").read_text())
        self.assertEqual((s["build"], s["sha"], s["status"]), ("task/foo", self.foo_sha, "running"))
        snap = playtest._snapshot_dir("game", self.foo_sha)
        (argv, kw), = sleeper.calls
        self.assertEqual(argv, [self.fake_godot, "--path", str(snap), "--log-file",
                                str(sess / "godot.log"), "--",
                                f"--arc-playtest-dir={sess}", f"--arc-build={self.foo_sha}"])
        self.assertEqual(kw["env"]["DISPLAY"], ":99")
        self.assertEqual(kw["env"]["XDG_DATA_HOME"], str(sess / "userdata"))
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(evs.of("playtest.launch")[0]["sha"], self.foo_sha)
        snap_view = playtest.snapshot("game")
        self.assertEqual(snap_view["sessions"][0]["status"], "running")
        with capture_events() as evs:
            self.assertEqual(dashboard._playtest_stop({"project": "game", "session": s["id"]}),
                             ({"ok": True}, 200))
        self.assertEqual(len(evs.of("playtest.stop")), 1)
        done = json.loads((sess / "session.json").read_text())
        self.assertEqual(done["status"], "ended")
        self.assertTrue(done["ended"])

    def test_second_launch_of_a_made_snapshot_starts_inline(self):
        sleeper = _Sleeper(self.fake_godot)
        with mock.patch.object(playtest.subprocess, "Popen", sleeper):
            playtest.ensure_snapshot("game", "main")
            obj, code = dashboard._playtest_launch({"project": "game", "build": "main"})
            self.assertEqual((code, obj["session"]["status"]), (200, "running"))
            playtest.stop("game", obj["session"]["id"])

    def test_failed_preparation_marks_the_session_failed(self):
        with mock.patch.object(playtest, "ensure_snapshot",
                               side_effect=playtest.Unavailable("no project.godot")):
            obj, code = dashboard._playtest_launch({"project": "game", "build": "main"})
            self.assertEqual(code, 200)
            t = playtest._PREPARING.get(obj["session"]["id"])
            if t is not None:
                t.join(30)
        s, = playtest.sessions("game")
        self.assertEqual(s["status"], "failed")
        self.assertIn("no project.godot", s["error"])
        # The CLI path (wait=True) raises instead.
        with mock.patch.object(playtest, "ensure_snapshot",
                               side_effect=playtest.Unavailable("boom")):
            with self.assertRaises(playtest.Unavailable):
                playtest.launch("game", "main")

    def test_orphaned_preparing_session_goes_stale(self):
        with mock.patch.object(playtest, "ensure_snapshot",
                               side_effect=playtest.Unavailable("x")):
            obj, _ = dashboard._playtest_launch({"project": "game", "build": "main"})
            t = playtest._PREPARING.get(obj["session"]["id"])
            if t is not None:
                t.join(30)
        path = playtest._session_path("game", obj["session"]["id"])
        doc = json.loads(path.read_text())
        doc.update(status="preparing", started=time.time() - playtest.PREPARE_STALE - 5)
        doc.pop("error", None)
        path.write_text(json.dumps(doc))
        s, = playtest.sessions("game")
        self.assertEqual(s["status"], "failed")
        self.assertIn("interrupted", s["error"])

    def test_launch_rejects_unlisted_values(self):
        for body in ({"project": "nope", "build": "main"},
                     {"project": "../game", "build": "main"},
                     {"project": ["game"], "build": "main"},
                     {"build": "main"}):
            self.assertEqual(dashboard._playtest_launch(body)[1], 404, body)
        for build in ("main ", "task/nope", "../main", "refs/heads/main", "", None,
                      ["main"], self.main_sha):
            self.assertEqual(dashboard._playtest_launch(
                {"project": "game", "build": build})[1], 404, build)
        self.assertEqual(playtest.session_ids("game"), [])

    def test_session_routes_reject_unlisted_sessions(self):
        sid = self.session()
        for bad in ("20260101-000000-0000", "../" + sid, sid + "/", "", None, 1):
            self.assertEqual(dashboard._playtest_stop(
                {"project": "game", "session": bad})[1], 404, bad)
            self.assertEqual(dashboard._playtest_survey(
                {"project": "game", "session": bad, "fun": 3, "clarity": 3,
                 "difficulty": 3})[1], 404, bad)
        self.assertEqual(dashboard._playtest_stop({"project": "nope", "session": sid})[1], 404)
        self.assertEqual(dashboard._playtest_survey(
            {"project": "game", "session": sid, "fun": 9, "clarity": 3,
             "difficulty": 3})[1], 400)
        self.assertEqual(dashboard._playtest_survey(
            {"project": "game", "session": sid, "fun": 4, "clarity": 3,
             "difficulty": 3, "note": 5})[1], 400)
        self.assertEqual(dashboard._playtest_survey(
            {"project": "game", "session": sid, "fun": 4, "clarity": 3,
             "difficulty": 3, "note": "ok"}), ({"ok": True}, 200))

    def test_finding_route(self):
        obj, code = dashboard._playtest_finding(
            {"project": "game", "category": "bug", "severity": 2, "note": "stuck"})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["finding"]["source"], "dashboard")
        for body, want in (({"project": "nope", "category": "bug", "severity": 2, "note": "n"}, 404),
                           ({"project": "game", "session": "x", "category": "bug",
                             "severity": 2, "note": "n"}, 404),
                           ({"project": "game", "category": "rce", "severity": 2, "note": "n"}, 400),
                           ({"project": "game", "category": "bug", "severity": 0, "note": "n"}, 400),
                           ({"project": "game", "category": "bug", "severity": 2, "note": ""}, 400),
                           ({"project": "game", "category": "bug", "severity": 2, "note": 7}, 400)):
            self.assertEqual(dashboard._playtest_finding(body)[1], want, body)

    def test_triage_route(self):
        fid = playtest.add_finding("game", category="bug", severity=2, note="n")["id"]
        for body, want in (({"project": "nope", "finding": fid, "state": "accepted"}, 404),
                           ({"project": "game", "finding": "f-00000000", "state": "accepted"}, 404),
                           ({"project": "game", "finding": fid + " ", "state": "accepted"}, 404),
                           ({"project": "game", "finding": fid, "state": "merged"}, 400),
                           ({"project": "game", "finding": fid, "state": "verified"}, 400),
                           ({"project": "game", "finding": fid, "state": "accepted",
                             "link": ["x"]}, 400)):
            self.assertEqual(dashboard._playtest_triage(body)[1], want, body)
        obj, code = dashboard._playtest_triage(
            {"project": "game", "finding": fid, "state": "accepted", "link": "#12"})
        self.assertEqual((code, obj["finding"]["state"], obj["finding"]["link"]),
                         (200, "accepted", "#12"))

    def test_post_routes_are_dispatched(self):
        self.assertEqual(sorted(dashboard._PLAYTEST_POSTS), sorted(
            f"/api/studio/playtest/{n}" for n in ("launch", "stop", "finding",
                                                   "triage", "survey")))

    def test_shot_path_is_contained(self):
        sid = self.session()
        d = playtest._sessions_dir("game") / sid
        (d / "shot-0.png").write_bytes(b"\x89PNG")
        (d / "notes.txt").write_text("x")
        outside = self.studio / "secret.png"
        outside.write_bytes(b"\x89PNG")
        os.symlink(outside, d / "shot-9.png")
        self.assertEqual(playtest.shot_path("game", sid, "shot-0.png"), (d / "shot-0.png").resolve())
        for bad in ("../session.json", "../../secret.png", "notes.txt", "a/b.png",
                    "shot-9.png", "missing.png", "", ".png/../x.png", None):
            self.assertIsNone(playtest.shot_path("game", sid, bad), bad)
        self.assertIsNone(playtest.shot_path("game", "../" + sid, "shot-0.png"))
        self.assertIsNone(playtest.shot_path("game", "20260101-000000-0000", "shot-0.png"))
        self.assertIsNone(playtest.shot_path("nope", sid, "shot-0.png"))
        self.assertIsNone(playtest.shot_path("../game", sid, "shot-0.png"))


class TestDashboardSnapshot(PlaytestCase):
    def test_snapshot_shape(self):
        sid = self.session(lines=[TestFindings.LINE])
        playtest.add_finding("game", category="ux", severity=4, note="typed")
        snap = playtest.snapshot("game")
        self.assertEqual(set(snap), {"godot", "display", "builds", "sessions",
                                     "findings", "counts"})
        self.assertEqual(snap["display"], ":99")
        self.assertEqual(snap["sessions"][0]["id"], sid)
        self.assertEqual(snap["sessions"][0]["findings"], 1,
                         "an ended session's in-game findings are ingested")
        game = next(f for f in snap["findings"] if f["source"] == "game")
        self.assertEqual(game["screenshot_url"],
                         f"/api/studio/playtest/shot?project=game&session={sid}&file=shot-0.png")
        self.assertIn("accepted", game["next"])
        typed = next(f for f in snap["findings"] if f["source"] == "dashboard")
        self.assertIsNone(typed["screenshot_url"])
        self.assertEqual((snap["counts"]["new"], snap["counts"]["open"]), (2, 2))

    def test_snapshot_never_raises(self):
        with mock.patch.object(playtest, "builds", side_effect=RuntimeError("boom")):
            self.assertIn("error", playtest.snapshot("game"))

    def test_a_dead_running_session_is_settled_once(self):
        sid = self.session(status="running", lines=[TestFindings.LINE])
        path = playtest._session_path("game", sid)
        doc = json.loads(path.read_text())
        doc["pid"] = 2 ** 22 + 12345          # beyond pid_max defaults: not alive
        path.write_text(json.dumps(doc))
        s = playtest.sessions("game")[0]
        self.assertEqual(s["status"], "ended")
        self.assertEqual(len(playtest.load_findings("game")), 1)

    def test_studio_status_carries_the_playtest_key(self):
        from studio import status
        p = status.project_snapshot("game")
        self.assertIn("playtest", p)
        self.assertEqual([b["id"] for b in p["playtest"]["builds"]], ["main", "task/foo"])


if __name__ == "__main__":
    unittest.main()
