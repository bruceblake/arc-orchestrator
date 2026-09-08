import asyncio
import logging
import signal
import time

import config
import events
from work import Roles, build_round_graph

log = logging.getLogger("supervisor")


class Supervisor:
    def __init__(self, pool, store, *, pipeline=None, questions=None, max_rounds=None):
        self.pool = pool
        self.store = store
        self.pipeline = pipeline or config.PIPELINE_ROUNDS
        self.questions = questions
        self.max_rounds = max_rounds
        self.stagger = config.ROUND_COOLDOWN
        self.stats_interval = config.STATS_INTERVAL
        self.roles = Roles()
        self.completed = 0
        self.failed = 0
        self.consecutive_failures = 0

    async def run(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        stale = self.store.fail_stale_rounds()
        if stale:
            log.warning("marked %d orphaned round(s) from a previous run as failed", stale)
        stats_task = asyncio.create_task(self._stats_loop(stop))
        active = set()
        launched = 0
        log.info(
            "supervisor start — pipeline=%d questions/round=%s max_rounds=%s dry_run=%s",
            self.pipeline, self.questions or config.QUESTIONS_PER_ROUND, self.max_rounds, self.pool.dry_run,
        )
        try:
            while True:
                if stop.is_set():
                    break
                can_launch = self.max_rounds is None or launched < self.max_rounds
                if can_launch and len(active) < self.pipeline:
                    if self.consecutive_failures:
                        delay = min((2 ** self.consecutive_failures) * 5, 300)
                        log.warning(
                            "backing off %.0fs after %d consecutive failed round(s)",
                            delay, self.consecutive_failures,
                        )
                        await self._sleep_or_stop(delay, stop)
                        if stop.is_set():
                            break
                    launched += 1
                    meta = {"round_index": launched}
                    active.add(asyncio.create_task(self._run_round(launched, meta)))
                    await self._sleep_or_stop(self.stagger, stop)
                elif active:
                    _done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                elif not can_launch:
                    break
                else:
                    await asyncio.sleep(0.05)
        finally:
            if active:
                log.info("stopping: cancelling %d active round(s)", len(active))
                for t in active:
                    t.cancel()
                await asyncio.gather(*active, return_exceptions=True)
            stats_task.cancel()
            await asyncio.gather(stats_task, return_exceptions=True)
            log.info("supervisor stopped — completed=%d failed=%d", self.completed, self.failed)

    async def _run_round(self, idx, meta):
        t0 = time.monotonic()
        events.set_context(workload="research", round=idx)
        graph = build_round_graph(self.pool, self.store, self.roles, meta, questions=self.questions)
        try:
            final = await graph.run({"round_index": idx})
            sr = final.get("results", {}).get("store_results", {})
            rid = meta.get("round_id")
            if rid is not None:
                self.store.finish_round(
                    rid, "ok",
                    questions=sr.get("stored"),
                    passed=1 if sr.get("passed_all") else 0,
                    verify_rounds=sr.get("verify_rounds"),
                )
            self.completed += 1
            self.consecutive_failures = 0
            events.emit("round_end", round=idx, status="ok",
                        seconds=round(time.monotonic() - t0, 1), stored=sr.get("stored"),
                        passed=sr.get("passed_all"), verify_rounds=sr.get("verify_rounds"))
            log.info(
                "round %d ok in %.1fs — topic=%r items=%s seeds=%s verify_rounds=%s passed=%s",
                idx, time.monotonic() - t0, meta.get("topic"), sr.get("stored"),
                sr.get("seeds"), sr.get("verify_rounds"), sr.get("passed_all"),
            )
        except asyncio.CancelledError:
            rid = meta.get("round_id")
            if rid is not None:
                self.store.finish_round(rid, "interrupted", error="cancelled during shutdown")
            events.emit("round_end", round=idx, status="interrupted",
                        seconds=round(time.monotonic() - t0, 1))
            raise
        except Exception as exc:
            self.failed += 1
            self.consecutive_failures += 1
            rid = meta.get("round_id")
            if rid is not None:
                self.store.finish_round(rid, "failed", error=str(exc)[:500])
            events.emit("round_end", round=idx, status="failed",
                        seconds=round(time.monotonic() - t0, 1), error=str(exc)[:300])
            log.error("round %d failed after %.1fs: %s", idx, time.monotonic() - t0, exc)

    async def _stats_loop(self, stop):
        while not stop.is_set():
            await self._sleep_or_stop(self.stats_interval, stop)
            if stop.is_set():
                break
            snap = self.pool.snapshot()
            st = self.store.stats()
            total_req = sum(snap["requests"].values())
            total_tok = sum(snap["tokens"].values())
            avg = f"{st['avg_score']:.1f}" if st["avg_score"] is not None else "n/a"
            rate = f"{st['pass_rate'] * 100:.0f}%" if st["pass_rate"] is not None else "n/a"
            log.info(
                "stats — rounds ok=%d failed=%d | items done=%d avg_score=%s pass_rate=%s | "
                "seeds unused=%d | requests=%d tokens=%d inflight=%s capacity=%s",
                self.completed, self.failed, st["items_done"], avg, rate,
                st["seeds_unused"], total_req, total_tok, snap["inflight"], snap["capacity"],
            )

    @staticmethod
    async def _sleep_or_stop(delay, stop):
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass