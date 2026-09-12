"""Driver-lease accounting and code-task lifecycle bookkeeping."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from helpers import capture_events
import config  # noqa: E402  # noqa: F401  (sys.path)

from store import Store

TTL = 1800
CAP = 2


class TempStore(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self._dir.name) / "t.db"))

    def tearDown(self):
        self._dir.cleanup()


class DriverLeases(TempStore):
    def test_grants_up_to_the_cap_then_reports_the_live_count(self):
        me = os.getpid()
        self.assertIsNone(self.store.acquire_driver_lease("m", me, "a", CAP, TTL))
        self.assertIsNone(self.store.acquire_driver_lease("m", me, "b", CAP, TTL))
        self.assertEqual(self.store.acquire_driver_lease("m", me, "c", CAP, TTL), CAP)

    def test_release_frees_a_slot(self):
        me = os.getpid()
        self.store.acquire_driver_lease("m", me, "a", CAP, TTL)
        self.store.acquire_driver_lease("m", me, "b", CAP, TTL)
        self.store.release_driver_lease("m", me, "a")
        self.assertIsNone(self.store.acquire_driver_lease("m", me, "c", CAP, TTL))

    def test_caps_are_per_model(self):
        me = os.getpid()
        for i in range(CAP):
            self.store.acquire_driver_lease("m1", me, str(i), CAP, TTL)
        self.assertIsNone(self.store.acquire_driver_lease("m2", me, "x", CAP, TTL))

    def test_a_dead_owners_lease_is_reaped_on_the_next_acquire(self):
        """A killed run must not pin the fleet at cap until its TTL."""
        dead = 2 ** 22  # pid far above /proc/sys/kernel/pid_max
        for i in range(CAP):
            self.store.acquire_driver_lease("m", dead, str(i), CAP, TTL)
        self.assertIsNone(
            self.store.acquire_driver_lease("m", os.getpid(), "mine", CAP, TTL))

    def test_expired_leases_are_reaped(self):
        me = os.getpid()
        self.store.acquire_driver_lease("m", me, "old", CAP, TTL)
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE driver_leases SET acquired_at=?", (time.time() - TTL - 10,))
            self.store.conn.commit()
        self.assertEqual(self.store.reap_driver_leases(TTL), 1)
        self.assertEqual(self.store.driver_lease_rows(), [])

    def test_release_leases_for_pid_drops_only_that_pid(self):
        me, other = os.getpid(), os.getpid() + 1
        self.store.acquire_driver_lease("m", me, "mine", 9, TTL)
        self.store.acquire_driver_lease("m", other, "theirs", 9, TTL)
        self.assertEqual(self.store.release_leases_for_pid(me), 1)
        rows = self.store.driver_lease_rows()
        self.assertEqual([r["pid"] for r in rows], [other])


class CodeTaskLifecycle(TempStore):
    def add(self, tid, taskfile, status="running", model=config.ESCALATION_PATH[0]):
        self.store.upsert_code_task(taskfile, tid, tid, model, "kimi", status)

    def test_running_rows_are_listed_and_scoped(self):
        self.add("a", "f1.json")
        self.add("b", "f2.json")
        self.assertEqual(len(self.store.running_code_tasks()), 2)
        self.assertEqual(
            [r["id"] for r in self.store.running_code_tasks(taskfile="f1.json")], ["a"])

    def test_stale_reset_is_scoped_to_one_taskfile(self):
        """A blanket reset would clobber a concurrent run of another file."""
        self.add("a", "f1.json")
        self.add("b", "f2.json")
        self.assertEqual(self.store.reset_stale_code_tasks(taskfile="f1.json"), 1)
        self.assertEqual([r["id"] for r in self.store.running_code_tasks()], ["b"])

    def test_reset_records_the_reason_verbatim(self):
        """The reason drives escalation: an infrastructure reason must not
        read as a capability failure to the resume planner."""
        self.add("a", "f1.json")
        self.store.reset_stale_code_tasks(taskfile="f1.json", reason="interrupted: x")
        row = self.store.code_tasks_for("f1.json")[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "interrupted: x")

    def test_upsert_preserves_identity_across_status_changes(self):
        self.add("a", "f1.json", status="running")
        self.store.upsert_code_task("f1.json", "a", "a", "GLM-5.3", "kimi",
                                    "merged", finished=True)
        rows = self.store.code_tasks_for("f1.json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "merged")
        self.assertEqual(rows[0]["model"], "GLM-5.3")

    def test_escalation_updates_the_recorded_model(self):
        """The escalate node re-upserts at a stronger tier; if that is dropped,
        the next resume restarts the task at the tier it already outgrew."""
        self.add("a", "f1.json", model=config.ESCALATION_PATH[0])
        self.store.upsert_code_task("f1.json", "a", "a", "GLM-5.3", "kimi", "running")
        row = self.store.code_tasks_for("f1.json")[0]
        self.assertEqual(row["model"], "GLM-5.3")
        self.assertEqual(row["reviewer"], "kimi")

    def test_reupsert_without_a_branch_keeps_the_allocation(self):
        self.store.upsert_code_task("f1.json", "a", "a", "gpt-oss-120b", "kimi",
                                    "running", branch="task/a", worktree="/wt/a")
        self.store.upsert_code_task("f1.json", "a", "a", "GLM-5.3", "kimi", "running")
        rows = [r for r in self.store.code_tasks_all() if r["id"] == "a"]
        self.assertEqual(rows[0]["branch"], "task/a")
        self.assertEqual(rows[0]["worktree"], "/wt/a")

    def test_same_task_id_in_two_taskfiles_stays_separate(self):
        self.add("shared", "f1.json")
        self.add("shared", "f2.json", status="merged")
        self.assertEqual(self.store.code_tasks_for("f1.json")[0]["status"], "running")
        self.assertEqual(self.store.code_tasks_for("f2.json")[0]["status"], "merged")


if __name__ == "__main__":
    unittest.main()
