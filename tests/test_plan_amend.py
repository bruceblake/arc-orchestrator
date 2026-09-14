"""Plan amendments: the taskfile is a living document (plan_amend.py).

Agents propose changes to the plan from inside their worktrees via
.arc/plan_proposals.jsonl; the graph harvests the file after every agent run,
validates each proposal through the real taskfile loader, and applies the
survivors to the taskfile on disk. These tests cover the parse/delete channel
semantics, every proposal kind, the freeze boundary (merged/running tasks are
immutable), per-entry rollback on loader rejection, and the recording trail.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeStore, capture_events
from helpers import ENTRY, STRONGEST, ENTRY_REVIEWER, STRONGEST_REVIEWER  # noqa: E402,F401

import code_tasks
import config
import plan_amend
import store as store_mod


def _task(tid, model=None, reviewer=None, **kw):
    t = {"id": tid, "title": f"title {tid}", "prompt": f"do {tid}",
         "model": model or ENTRY, "reviewer": reviewer or ENTRY_REVIEWER,
         "verify_cmd": "true"}
    t.update(kw)
    return t


def _write_taskfile(tasks):
    doc = {"project": {"repo": "/tmp", "title": "T", "tasks": tasks}}
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, fh)
    fh.close()
    return Path(fh.name)


def _read_tasks(tf):
    return json.loads(Path(tf).read_text())["project"]["tasks"]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plan-amend-test-"))
        self.addCleanup(self._cleanup)
        self.store = store_mod.Store(":memory:")
        self.tf = _write_taskfile([_task("t1"), _task("t2")])
        self.validate = code_tasks._amendment_validator(str(self.tf), None)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self, tid, status, finished=True):
        self.store.upsert_code_task(str(self.tf), tid, f"title {tid}", ENTRY,
                                    ENTRY_REVIEWER, status, finished=finished)

    def apply(self, entries, **kw):
        kw.setdefault("proposer", "t9")
        kw.setdefault("role", "implementer")
        kw.setdefault("model", ENTRY)
        kw.setdefault("validate", self.validate)
        return plan_amend.apply(self.store, str(self.tf), entries, **kw)

    def rows(self):
        return self.store.list_plan_proposals(str(self.tf), 100)


class TestChannel(_Base):
    def test_read_proposals_reads_and_deletes(self):
        d = self.tmp / ".arc"
        d.mkdir()
        p = d / "plan_proposals.jsonl"
        p.write_text('{"kind":"note","task":"t1","note":"hi"}\n'
                     '{"kind":"change_verify","task":"t2","verify_cmd":"make -C x t"}\n')
        entries = plan_amend.read_proposals(self.tmp)
        self.assertEqual(len(entries), 2)
        # Deletion is the anti-`git add -A` guarantee: the file must not
        # survive to publish. A second read must find nothing.
        self.assertFalse(p.exists())
        self.assertEqual(plan_amend.read_proposals(self.tmp), [])

    def test_read_proposals_missing_file(self):
        self.assertEqual(plan_amend.read_proposals(self.tmp), [])

    def test_malformed_line_is_recorded_rejected(self):
        counts = self.apply([{"kind": "note", "task": "t1", "note": "ok"},
                             {"_malformed": '{"kind": broken'}])
        self.assertEqual((counts["noted"], counts["rejected"]), (1, 1))
        bad = [r for r in self.rows() if r["action"] == "rejected"]
        self.assertIn("not valid JSON", bad[0]["reason"])
        # The malformed entry never touched the file.
        self.assertEqual(len(_read_tasks(self.tf)), 2)

    def test_overlong_batch_surfaces_a_truncation_rejection(self):
        # A runaway agent appending forever is a bug to be surfaced, not a
        # backlog: beyond the cap the tail is dropped and the sentinel is
        # itself recorded (rejected) so the cut is part of the trail.
        d = self.tmp / ".arc"
        d.mkdir()
        with (d / "plan_proposals.jsonl").open("w") as f:
            for i in range(plan_amend.MAX_PER_BATCH + 5):
                f.write(json.dumps({"kind": "note", "task": "t1",
                                    "note": f"n{i}"}) + "\n")
        entries = plan_amend.read_proposals(self.tmp)
        self.assertEqual(len(entries), plan_amend.MAX_PER_BATCH + 1)
        self.assertEqual(entries[-1], {"_truncated": True})
        counts = self.apply(entries)
        self.assertEqual(counts["noted"], plan_amend.MAX_PER_BATCH)
        self.assertEqual(counts["rejected"], 1)
        trunc = [r for r in self.rows() if r["action"] == "rejected"]
        self.assertIn("batch truncated", trunc[0]["reason"])


class TestNote(_Base):
    def test_note_recorded_and_file_unchanged(self):
        before = Path(self.tf).read_text()
        counts = self.apply([{"kind": "note", "task": "t1",
                              "note": "t1's gate is weaker than its spec"}])
        self.assertEqual(counts, {"applied": 0, "rejected": 0, "noted": 1})
        self.assertEqual(Path(self.tf).read_text(), before)
        row = self.rows()[0]
        self.assertEqual((row["action"], row["kind"], row["target"]),
                         ("noted", "note", "t1"))
        self.assertIn("weaker", row["reason"])

    def test_note_on_frozen_task_still_lands(self):
        self.status("t1", "merged")
        counts = self.apply([{"kind": "note", "task": "t1", "note": "too late but noted"}])
        self.assertEqual(counts["noted"], 1)

    def test_empty_note_rejected(self):
        counts = self.apply([{"kind": "note", "task": "t1", "note": "  "}])
        self.assertEqual(counts["rejected"], 1)


class TestEditScope(_Base):
    def test_edit_scope_applies(self):
        counts = self.apply([{"kind": "edit_scope", "task": "t1",
                              "title": "better title",
                              "prompt": "a sharper spec",
                              "files_hint": ["a.py", "b.py"]}])
        self.assertEqual(counts["applied"], 1)
        t1 = _read_tasks(self.tf)[0]
        self.assertEqual(t1["title"], "better title")
        self.assertEqual(t1["prompt"], "a sharper spec")
        self.assertEqual(t1["files_hint"], ["a.py", "b.py"])

    def test_edit_scope_rejects_frozen_statuses(self):
        for st in ("merged", "running", "in_review", "conflict"):
            self.status("t1", st)
            counts = self.apply([{"kind": "edit_scope", "task": "t1",
                                  "title": "nope"}])
            self.assertEqual(counts["applied"], 0, st)
            self.assertEqual(counts["rejected"], 1, st)
            self.assertEqual(_read_tasks(self.tf)[0]["title"], "title t1", st)

    def test_pending_status_is_frozen_too(self):
        # The freeze boundary is an ALLOWLIST (only failed/skipped mutate),
        # not a denylist: a status nobody named — e.g. `pending` — must
        # freeze as well, because the burden of proof is on mutation (an
        # unrecognized state may hide a live agent).
        self.status("t1", "pending")
        counts = self.apply([{"kind": "edit_scope", "task": "t1", "title": "x"}])
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("pending", self.rows()[0]["reason"])

    def test_failed_and_skipped_may_be_amended(self):
        # A failed/skipped task's resume re-reads the file, so amending it is
        # honest — unlike merged/immutable or in-flight/live.
        for st in ("failed", "skipped"):
            self.status("t1", st)
            counts = self.apply([{"kind": "edit_scope", "task": "t1",
                                  "title": f"fixed after {st}"}])
            self.assertEqual(counts["applied"], 1, st)

    def test_unknown_task_rejected(self):
        counts = self.apply([{"kind": "edit_scope", "task": "ghost", "title": "x"}])
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("unknown task", self.rows()[0]["reason"])


class TestChangeVerify(_Base):
    def test_change_verify_applies(self):
        counts = self.apply([{"kind": "change_verify", "task": "t2",
                              "verify_cmd": "./check.sh"}])
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(_read_tasks(self.tf)[1]["verify_cmd"], "./check.sh")

    def test_change_verify_never_empties_the_gate(self):
        # Rule 4 holds for agents exactly as for planners: a gate may be
        # REPLACED, never removed.
        for v in ("", "   ", None, 3):
            counts = self.apply([{"kind": "change_verify", "task": "t2",
                                  "verify_cmd": v}])
            self.assertEqual(counts["applied"], 0, repr(v))
        self.assertEqual(_read_tasks(self.tf)[1]["verify_cmd"], "true")


class TestChangeModel(_Base):
    def test_change_model_applies_with_reviewer_flip(self):
        # t1 starts on ENTRY with ENTRY's cross-family reviewer. Switching it
        # to STRONGEST makes the pairing same-family (with a two-family roster:
        # ENTRY_REVIEWER *is* STRONGEST's family), so the reviewer flips to the
        # other family rather than let loader validation reject the amendment.
        counts = self.apply([{"kind": "change_model", "task": "t1",
                              "model": STRONGEST}])
        self.assertEqual(counts["applied"], 1)
        t1 = _read_tasks(self.tf)[0]
        self.assertEqual(t1["model"], STRONGEST)
        if config.MODEL_FAMILY[STRONGEST] == ENTRY_REVIEWER:
            self.assertEqual(t1["reviewer"], STRONGEST_REVIEWER)

    def test_change_model_rejects_non_implementer(self):
        counts = self.apply([{"kind": "change_model", "task": "t1",
                              "model": "GLM-4.6-air"}])
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(_read_tasks(self.tf)[0]["model"], ENTRY)


class TestAddTask(_Base):
    def test_add_task_applies_and_gets_cross_family_reviewer(self):
        counts = self.apply([{"kind": "add_task", "taskspec": {
            "id": "t3", "title": "new work", "prompt": "found along the way",
            "model": ENTRY, "verify_cmd": "true", "deps": ["t1"]}}])
        self.assertEqual(counts["applied"], 1)
        tasks = _read_tasks(self.tf)
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[2]["reviewer"], ENTRY_REVIEWER)

    def test_add_task_rejects_reused_or_spent_id(self):
        counts = self.apply([{"kind": "add_task", "taskspec": {
            "id": "t1", "title": "x", "prompt": "x",
            "model": ENTRY, "verify_cmd": "true"}}])
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("already used", self.rows()[0]["reason"])
        # An id with a ROW is spent even if the task left the file: resume
        # keys on the row, and a merged row would skip the new work as done.
        self.status("ghost", "merged")
        counts = self.apply([{"kind": "add_task", "taskspec": {
            "id": "ghost", "title": "x", "prompt": "x",
            "model": ENTRY, "verify_cmd": "true"}}])
        self.assertEqual(counts["rejected"], 1)

    def test_add_task_requires_gate_and_model(self):
        counts = self.apply([{"kind": "add_task", "taskspec": {
            "id": "t3", "title": "x", "prompt": "x", "model": ENTRY}}])
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("verify_cmd", self.rows()[0]["reason"])
        counts = self.apply([{"kind": "add_task", "taskspec": {
            "id": "t3", "title": "x", "prompt": "x", "verify_cmd": "true"}}])
        self.assertEqual(counts["rejected"], 1)

    def test_self_dep_cycle_rejected_with_rollback(self):
        counts = self.apply([{"kind": "add_task", "taskspec": {
            "id": "cy", "title": "x", "prompt": "x", "model": ENTRY,
            "verify_cmd": "true", "deps": ["cy"]}}])
        self.assertEqual(counts["applied"], 0)
        self.assertEqual(counts["rejected"], 1)
        # Rolled back: the cycle never lands in the file.
        self.assertEqual([t["id"] for t in _read_tasks(self.tf)], ["t1", "t2"])
        self.assertIn("invalid plan", self.rows()[0]["reason"])

    def test_unknown_dep_rejected(self):
        counts = self.apply([{"kind": "add_task", "taskspec": {
            "id": "t3", "title": "x", "prompt": "x", "model": ENTRY,
            "verify_cmd": "true", "deps": ["ghost"]}}])
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual([t["id"] for t in _read_tasks(self.tf)], ["t1", "t2"])


class TestSplitTask(_Base):
    def setUp(self):
        super().setUp()
        self.tf = _write_taskfile([_task("t1"), _task("t2", deps=["t1"])])
        self.validate = code_tasks._amendment_validator(str(self.tf), None)

    def test_split_applies_and_dependents_repoint_to_all_pieces(self):
        counts = self.apply([{"kind": "split_task", "task": "t1", "into": [
            {"id": "t1a", "title": "half a", "prompt": "a", "verify_cmd": "true"},
            {"id": "t1b", "title": "half b", "prompt": "b", "verify_cmd": "true",
             "deps": ["t1a"]}]}])
        self.assertEqual(counts["applied"], 1)
        tasks = _read_tasks(self.tf)
        self.assertEqual([t["id"] for t in tasks], ["t1a", "t1b", "t2"])
        # t1a inherited t1's (empty) deps and t1's model; reviewers were
        # auto-paired cross-family.
        self.assertEqual(tasks[0]["model"], ENTRY)
        self.assertEqual(tasks[0]["reviewer"], ENTRY_REVIEWER)
        # The dependent re-points to BOTH pieces — any subset either starts it
        # too early or ties it to work it never needed.
        self.assertEqual(sorted(tasks[2]["deps"]), ["t1a", "t1b"])

    def test_split_first_piece_unions_target_deps_with_declared(self):
        # t1 flows from t0; a first piece that declares its own deps must not
        # LOSE t0 — the split's combined upstream is exactly the target's
        # upstream, and deps a piece declares are unioned in, never a
        # replacement (a replacement would start the piece before t0 exists).
        self.tf = _write_taskfile(
            [_task("t0"), _task("t1", deps=["t0"]), _task("t2")])
        self.validate = code_tasks._amendment_validator(str(self.tf), None)
        counts = self.apply([{"kind": "split_task", "task": "t1", "into": [
            {"id": "t1a", "title": "a", "prompt": "a", "verify_cmd": "true",
             "deps": ["t2"]},
            {"id": "t1b", "title": "b", "prompt": "b", "verify_cmd": "true",
             "deps": ["t1a"]}]}])
        self.assertEqual(counts["applied"], 1)
        tasks = _read_tasks(self.tf)
        t1a = [t for t in tasks if t["id"] == "t1a"][0]
        self.assertEqual(t1a["deps"], ["t0", "t2"])

    def test_split_rejects_merged_target(self):
        self.status("t1", "merged")
        counts = self.apply([{"kind": "split_task", "task": "t1", "into": [
            {"id": "t1a", "title": "a", "prompt": "a", "verify_cmd": "true"},
            {"id": "t1b", "title": "b", "prompt": "b", "verify_cmd": "true"}]}])
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("merged", self.rows()[0]["reason"])

    def test_split_rejects_when_dependent_points_at_target(self):
        # A `when` condition reads the target's verdict; distributing that
        # semantics over pieces cannot be guessed, so the split refuses.
        self.tf = _write_taskfile([
            _task("probe", probe_cmd="true"),
            _task("t1"),
            _task("t2", when={"dep": "t1", "key": "k", "equals": "x"})])
        self.validate = code_tasks._amendment_validator(str(self.tf), None)
        counts = self.apply([{"kind": "split_task", "task": "t1", "into": [
            {"id": "t1a", "title": "a", "prompt": "a", "verify_cmd": "true"},
            {"id": "t1b", "title": "b", "prompt": "b", "verify_cmd": "true"}]}])
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("`when`", self.rows()[0]["reason"])

    def test_split_piece_without_gate_rejected(self):
        self.tf = _write_taskfile([_task("t1", verify_cmd="")])
        self.validate = code_tasks._amendment_validator(str(self.tf), None)
        counts = self.apply([{"kind": "split_task", "task": "t1", "into": [
            {"id": "t1a", "title": "a", "prompt": "a"},
            {"id": "t1b", "title": "b", "prompt": "b"}]}])
        # A piece would inherit the target's empty gate — Rule 4 refuses.
        self.assertEqual(counts["rejected"], 1)
        self.assertIn("empty gate", self.rows()[0]["reason"])


class TestRobustness(_Base):
    def test_unreadable_taskfile_rejects_all_without_raising(self):
        counts = plan_amend.apply(self.store, "/nonexistent/plan.json",
                                  [{"kind": "note", "task": "t", "note": "x"},
                                   {"kind": "edit_scope", "task": "t", "title": "y"}],
                                  proposer="p", role="r", model=ENTRY,
                                  validate=self.validate)
        self.assertEqual(counts["rejected"], 2)
        rows = self.store.list_plan_proposals("/nonexistent/plan.json", 10)
        self.assertEqual(len(rows), 2)

    def test_ordering_good_bad_good(self):
        counts = self.apply([
            {"kind": "edit_scope", "task": "t1", "title": "one"},
            {"kind": "change_verify", "task": "t2", "verify_cmd": ""},
            {"kind": "edit_scope", "task": "t2", "title": "two"}])
        self.assertEqual((counts["applied"], counts["rejected"]), (2, 1))
        tasks = _read_tasks(self.tf)
        self.assertEqual(tasks[0]["title"], "one")
        self.assertEqual(tasks[1]["title"], "two")

    def test_store_unreadable_rejects_mutations_but_notes_land(self):
        class ExplodingStore(FakeStore):
            def code_tasks_for(self, taskfile):
                raise RuntimeError("db gone")
        st = ExplodingStore()
        counts = plan_amend.apply(st, str(self.tf), [
            {"kind": "change_verify", "task": "t1", "verify_cmd": "./x"},
            {"kind": "add_task", "taskspec": {
                "id": "t3", "title": "x", "prompt": "x",
                "model": ENTRY, "verify_cmd": "true"}},
            {"kind": "note", "task": "t1", "note": "cannot prove t1 unstarted"}],
            proposer="p", role="r", model=ENTRY, validate=self.validate)
        # Without statuses there is no freeze proof, so EVERY mutation refuses
        # closed: target edits, and new ids alike (an id with an unread row
        # would key new work against old state on resume).
        self.assertEqual(counts["rejected"], 2)
        self.assertEqual(counts["noted"], 1)
        rejected = [a[7] for a, _ in st.plan_proposals if a[6] == "rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all("store unreadable" in r for r in rejected))

    def test_events_and_rows_cover_every_entry(self):
        with capture_events() as cap:
            self.apply([{"kind": "edit_scope", "task": "t1", "title": "one"},
                        {"kind": "change_verify", "task": "t2", "verify_cmd": ""}])
        evs = cap.of("plan.amend")
        self.assertEqual(len(evs), 2)
        self.assertEqual({e["action"] for e in evs}, {"applied", "rejected"})
        applied = [e for e in evs if e["action"] == "applied"][0]
        self.assertEqual((applied["task"], applied["proposer"], applied["kind"]),
                         ("t1", "t9", "edit_scope"))


class TestHarvest(_Base):
    def test_harvest_end_to_end(self):
        d = self.tmp / "wt" / ".arc"
        d.mkdir(parents=True)
        (d / "plan_proposals.jsonl").write_text(
            '{"kind":"edit_scope","task":"t2","title":"amended by run"}\n'
            '{"kind":"note","task":"t1","note":"risky dep"}\n')
        plan_amend.harvest(self.store, str(self.tf), self.tmp / "wt",
                           proposer="t1", role="implementer", model=ENTRY,
                           validate=self.validate)
        self.assertEqual(_read_tasks(self.tf)[1]["title"], "amended by run")
        self.assertFalse((d / "plan_proposals.jsonl").exists())
        rows = self.rows()
        self.assertEqual({r["action"] for r in rows}, {"applied", "noted"})
        self.assertEqual({r["proposer"] for r in rows}, {"t1"})

    def test_failed_delete_is_retried_by_harvest(self):
        # The anti-`git add -A` guarantee hangs on the delete succeeding. If
        # read-time deletion fails (lock, transient EBUSY), harvest retries
        # once after applying — and wins here.
        d = self.tmp / ".arc"
        d.mkdir()
        f = d / "plan_proposals.jsonl"
        f.write_text('{"kind":"note","task":"t1","note":"x"}\n')
        real_unlink = Path.unlink
        calls = {"n": 0}

        def flaky(self, *a, **k):
            if self == f:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("locked")
            return real_unlink(self, *a, **k)

        with capture_events() as cap, mock.patch.object(Path, "unlink", flaky):
            plan_amend.harvest(self.store, str(self.tf), self.tmp,
                               proposer="t1", role="implementer", model=ENTRY,
                               validate=self.validate)
        self.assertFalse(f.exists())
        self.assertEqual(cap.of("plan.amend.channel_survives"), [])

    def test_unkillable_channel_is_a_loud_event(self):
        # A file that survives BOTH deletes would ride into the PR on
        # publish's `git add -A` if the pathspec belt (gitstore) ever missed
        # it — so survival is an event a human will see, not a log line.
        d = self.tmp / ".arc"
        d.mkdir()
        f = d / "plan_proposals.jsonl"
        f.write_text('{"kind":"note","task":"t1","note":"x"}\n')

        def always_fail(self, *a, **k):
            if self == f:
                raise OSError("rdlock")

        with capture_events() as cap, mock.patch.object(Path, "unlink",
                                                        always_fail):
            plan_amend.harvest(self.store, str(self.tf), self.tmp,
                               proposer="t1", role="implementer", model=ENTRY,
                               validate=self.validate)
        ev = cap.first("plan.amend.channel_survives")
        self.assertEqual(ev["path"], str(f))
        self.assertTrue(f.exists())


if __name__ == "__main__":
    unittest.main()
