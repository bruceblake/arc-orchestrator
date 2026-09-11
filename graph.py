import asyncio
import json
import logging
import time

import errors
import events


class GraphError(RuntimeError):
    pass


class Node:
    def __init__(self, name, fn, gather=False):
        self.name = name
        self.fn = fn
        self.gather = gather


class Edge:
    """A directed edge, optionally conditional on the source node's result.

    ``on_drain`` marks an edge that keeps firing after the graph has started
    draining. Use it for the tail of a chain whose expensive work is already
    DONE and only needs landing — in the code workload, a task that has pushed
    a branch and opened a pull request still has to get that PR reviewed and
    merged. Without it a sibling task's failure orphans a perfectly good PR:
    ``publish`` completes, nothing schedules ``pr_review``, and the branch sits
    on GitHub with no one coming back for it.

    It is deliberately NOT the default. Draining exists to stop committing new
    model time to a run that has already failed, so edges that kick off fresh
    implement/rework work must stay closed.
    """

    def __init__(self, src, dst, when=None, on_drain=False):
        self.src = src
        self.dst = dst
        self.when = when
        self.on_drain = on_drain


# How long a persisted node result is trusted on resume. A row older than
# this is ignored (and left for the next clear): a day-old verdict may no
# longer describe the code it was recorded against.
STATE_TTL = 24 * 3600


class Persist:
    """Opt-in handle naming the nodes whose results survive a killed run.

    Give one to ``Graph(..., persist=Persist(store, nodes=[...]))`` and every
    named node's result is written to the store's ``graph_state`` table as it
    completes; a later ``_Execution`` of a graph with the SAME name seeds its
    ctx from those rows and fires their downstream edges, so a run killed
    mid-graph resumes downstream of finished work instead of repeating it.

    Name only nodes whose result is a pure verdict the graph routes on (a
    review verdict, a gate pass) — never a node whose fn has external side
    effects (alloc, publish, merge): seeding trusts the recorded result
    WITHOUT re-running the fn, so an effect that never happened would be
    believed on resume.

    Double-fire hazard: if a seeded node runs again anyway (its upstream
    re-fired), its downstream edges fire a second time. Shape a resume graph
    to route AROUND persisted nodes — close their incoming edges with
    ``when=lambda r, ctx: False`` — the way code_tasks stubs merged tasks.
    """

    def __init__(self, store, nodes, ttl=None):
        self.store = store
        self.nodes = frozenset(nodes)
        self.ttl = STATE_TTL if ttl is None else ttl


class Graph:
    def __init__(self, name, max_steps=500, drain_timeout=1200, max_in_flight=None,
                 persist=None):
        self.name = name
        self.max_steps = max_steps
        # Bounded admission: at most this many node fns execute at once; the
        # rest wait on their per-node queues. None (default) is the old
        # unbounded behaviour, where every ready node starts immediately and
        # contention is pushed down into the drivers.
        self.max_in_flight = max_in_flight
        # After a node fails, sibling nodes already running are allowed to
        # finish (see _Execution) rather than being cancelled mid-flight. This
        # bounds that wait so one wedged node cannot hang the whole run.
        self.drain_timeout = drain_timeout
        self.nodes = {}
        self.edges = []
        self.starts = []
        # Opt-in resumable state (see Persist). None (the default, and every
        # existing caller) keeps runs exactly as ephemeral as before.
        self.persist = persist

    def node(self, name, fn=None, *, gather=False):
        def register(f):
            if name in self.nodes:
                raise GraphError(f"duplicate node: {name}")
            self.nodes[name] = Node(name, f, gather)
            return f

        return register(fn) if fn is not None else register

    def edge(self, src, dst, when=None, *, on_drain=False):
        self.edges.append(Edge(src, dst, when, on_drain))

    def start(self, name):
        self.starts.append(name)

    def validate(self):
        if not self.starts:
            raise GraphError("graph has no start node")
        for s in self.starts:
            if s not in self.nodes:
                raise GraphError(f"unknown start node: {s}")
        for e in self.edges:
            if e.src not in self.nodes:
                raise GraphError(f"edge from unknown node: {e.src}")
            if e.dst not in self.nodes:
                raise GraphError(f"edge to unknown node: {e.dst}")
        reachable = set()
        frontier = list(self.starts)
        while frontier:
            n = frontier.pop()
            if n in reachable:
                continue
            reachable.add(n)
            frontier.extend(e.dst for e in self.edges if e.src == n)
        unreachable = set(self.nodes) - reachable
        if unreachable:
            raise GraphError(f"unreachable nodes: {sorted(unreachable)}")

    def run(self, ctx=None):
        return _Execution(self, ctx or {}).run()


class _Execution:
    def __init__(self, graph, ctx):
        self.g = graph
        self.log = logging.getLogger(f"graph.{graph.name}")
        self.queues = {name: asyncio.Queue() for name in graph.nodes}
        self.ctx = ctx
        self.last_ctx = ctx
        self.gather_pending = {}
        self.gathered = {}
        self.in_flight = 0
        # Admission gate. Only a node fn holds a slot, and only while it runs:
        # a queued item still counts in in_flight (so _settle's bookkeeping is
        # untouched), and a gather waiting on sources holds NO slot. The one
        # deadlock shape to avoid — a gather holding a slot while the sources
        # it waits on sit behind a full limit — is therefore impossible: slots
        # turn over as running fns finish, and no fn waits on another node
        # being scheduled.
        self.admission = (
            asyncio.Semaphore(self.g.max_in_flight) if self.g.max_in_flight else None)
        self.done = asyncio.Event()
        self.error = None
        self.firings = 0
        self.drain_deadline = None
        # Nodes whose results were seeded from the store; run() fires their
        # outgoing edges so a resume continues downstream of finished work.
        self.seeded = []
        self._seed_from_store()

    def _seed_from_store(self):
        """Fill ctx['results']/ctx['runs'] from persisted rows (fill-if-absent:
        a value the caller already put in ctx wins). A row is trusted only if
        its node is still named by the persist handle, still exists in this
        graph, and is inside the TTL; anything else is ignored so the node
        simply re-executes. Store failures fail open for the same reason —
        persistence must never make a run LESS runnable."""
        p = self.g.persist
        if p is None or not p.nodes:
            return
        try:
            rows = p.store.graph_state_rows(self.g.name)
        except Exception as exc:
            self.log.warning("graph '%s': cannot read persisted state: %s",
                             self.g.name, exc)
            return
        now = time.time()
        results = self.ctx.setdefault("results", {})
        runs = self.ctx.setdefault("runs", {})
        for row in rows:
            name = row["node"]
            if name not in p.nodes or name not in self.g.nodes:
                continue
            if row["updated_at"] < now - p.ttl:
                continue  # stale: re-run the node instead of trusting it
            try:
                value = json.loads(row["result"])
            except (TypeError, ValueError):
                continue
            if name not in results:
                results[name] = value
                self.seeded.append(name)
            if name not in runs:
                runs[name] = row["runs"]
        if self.seeded:
            self.log.info("graph '%s': resuming with persisted results for %s",
                          self.g.name, sorted(self.seeded))
            events.emit("graph.resume", graph=self.g.name,
                        nodes=sorted(self.seeded))

    def _fire_seeded(self):
        """Fire the outgoing edges of store-seeded nodes so a resumed graph
        continues DOWNSTREAM of finished work. ``when`` predicates see the
        seeded ctx exactly as they would after a real firing; one that raises
        fails the run rather than routing the resume on a guess."""
        if not self.seeded:
            return
        for name in self.seeded:
            result = self.ctx["results"][name]
            for e in self.g.edges:
                if e.src != name:
                    continue
                if e.when is None or e.when(result, self.ctx):
                    self._put(e.dst, name, self.ctx, e.on_drain)

    def _save_state(self, name, result, runs):
        """Persist one named node's result (best effort). A result that cannot
        be JSON-encoded (a Path, an exception instance, ...) skips the write
        entirely — there is no honest smaller value to record, and leaving an
        older row untouched is better than lying. Never raises into the run:
        on a store error the run continues, merely un-persisted."""
        p = self.g.persist
        if p is None or name not in p.nodes:
            return
        try:
            blob = json.dumps(result)
        except (TypeError, ValueError):
            self.log.warning("node '%s': result not JSON-serialisable; not persisted", name)
            return
        try:
            p.store.save_graph_state(self.g.name, name, blob, runs)
        except Exception as exc:
            self.log.warning("graph '%s': cannot persist node '%s': %s",
                             self.g.name, name, exc)

    def _sources_of(self, name):
        return {e.src for e in self.g.edges if e.dst == name}

    def _put(self, name, src, ctx, on_drain=False):
        ctx = dict(ctx)
        ctx["results"] = dict(ctx.get("results", {}))
        self.queues[name].put_nowait((src, ctx, on_drain))
        self.in_flight += 1

    def _settle(self):
        """One queued item finished or was dropped; wake run() once drained."""
        self.in_flight -= 1
        if self.in_flight <= 0:
            self.done.set()

    def _fail(self, exc, node_name):
        """Record the first error and start the drain clock.

        The graph stops SCHEDULING new work immediately, but nodes already
        running keep going: in the code workload a sibling task may be minutes
        into an implement/review it will successfully merge, and cancelling it
        threw that away while leaving its worktree, DB row and driver lease
        behind for someone to reap by hand.
        """
        if self.error is not None:
            return
        self.error = exc
        self.drain_deadline = time.monotonic() + self.g.drain_timeout
        remaining = max(0, self.in_flight - 1)
        self.log.error("node '%s' failed: %s%s", node_name, exc,
                       f" — draining {remaining} in-flight node(s)" if remaining else "")
        events.emit("graph.draining", graph=self.g.name, node=node_name,
                    in_flight=remaining, error=str(exc)[:300])

    def _merge(self, ctxs):
        base = dict(ctxs[0])
        results = {}
        for c in ctxs:
            results.update(c.get("results", {}))
        base["results"] = results
        return base

    async def run(self):
        self.g.validate()
        for name, node in self.g.nodes.items():
            if node.gather:
                srcs = self._sources_of(name)
                if not srcs:
                    raise GraphError(f"gather node '{name}' has no incoming edges")
                self.gather_pending[name] = set(srcs)
                self.gathered[name] = {}
        workers = [asyncio.create_task(self._worker(node)) for node in self.g.nodes.values()]
        try:
            for s in self.g.starts:
                self._put(s, "__start__", self.ctx)
            # Seed firings happen after the start puts but before the first
            # await: everything so far is synchronous, so a seeded edge and a
            # start put can never interleave with a worker.
            self._fire_seeded()
            while not self.done.is_set():
                try:
                    await asyncio.wait_for(self.done.wait(), timeout=5)
                except asyncio.TimeoutError:
                    if self.drain_deadline and time.monotonic() > self.drain_deadline:
                        self.log.error(
                            "graph '%s': drain timed out, cancelling %d in-flight node(s)",
                            self.g.name, self.in_flight)
                        events.emit("graph.drain_timeout", graph=self.g.name,
                                    in_flight=self.in_flight)
                        break
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        if self.error is not None:
            raise self.error
        if self.g.persist is not None:
            # Success: there is nothing left to resume. Best effort — a
            # failed clear only leaves rows the next run will re-clear.
            try:
                self.g.persist.store.clear_graph_state(self.g.name)
            except Exception as exc:
                self.log.warning("graph '%s': cannot clear persisted state: %s",
                                 self.g.name, exc)
        return self.last_ctx

    async def _worker(self, node):
        q = self.queues[node.name]
        while True:
            src, ctx, on_drain = await q.get()
            if self.error is not None and not on_drain:
                self._settle()  # graph is draining: drop queued work
                continue
            t0 = time.monotonic()
            try:
                if node.gather:
                    self.gathered[node.name][src] = ctx
                    self.gather_pending[node.name].discard(src)
                    if self.gather_pending[node.name]:
                        self.log.debug("gather '%s' waiting for %s", node.name, sorted(self.gather_pending[node.name]))
                        # A waiting gather is not "done", so it must not wake
                        # run() — but if nothing else is in flight either, the
                        # sources it waits on can never arrive. That used to
                        # hang the process forever; fail loudly instead.
                        self.in_flight -= 1
                        if self.in_flight <= 0:
                            self._fail(GraphError(
                                f"gather '{node.name}' still waiting for "
                                f"{sorted(self.gather_pending[node.name])} with nothing "
                                f"left in flight — unreachable sources"), node.name)
                            self.done.set()
                        continue
                    ctx = self._merge(list(self.gathered[node.name].values()))
                    self.gathered[node.name] = {}
                    self.gather_pending[node.name] = self._sources_of(node.name)
                if self.admission is not None:
                    await self.admission.acquire()
                    try:
                        # The drain check above ran BEFORE waiting for a slot;
                        # a failure may have started the drain in between.
                        # Landing work (on_drain) must still get in — its whole
                        # purpose is landing work that is already paid for —
                        # everything else drops the moment it is admitted.
                        if self.error is not None and not on_drain:
                            self._settle()  # dequeued before the drain, dropped on admission
                            continue
                        events.emit("node_start", graph=self.g.name, node=node.name)
                        result = await node.fn(ctx)
                    finally:
                        self.admission.release()
                else:
                    events.emit("node_start", graph=self.g.name, node=node.name)
                    result = await node.fn(ctx)
            except Exception as exc:
                # str(exc)[:300] was ALL that survived a node failure: no file,
                # no line, no frame. errors.capture keeps the traceback and
                # returns a fingerprint that groups this defect with its other
                # occurrences, so the event stays short and the evidence stops
                # being discarded.
                fp = errors.capture(exc, node=node.name, graph=self.g.name,
                                    where=f"graph:{node.name}")
                events.emit("node_error", graph=self.g.name, node=node.name,
                            seconds=round(time.monotonic() - t0, 3),
                            error=str(exc)[:300], fingerprint=fp)
                self._fail(exc, node.name)
                self._settle()
                continue
            self.firings += 1
            if self.firings > self.g.max_steps:
                self._fail(GraphError(
                    f"graph '{self.g.name}' exceeded max_steps={self.g.max_steps}"),
                    node.name)
                self._settle()
                continue
            runs = ctx.setdefault("runs", {})
            runs[node.name] = runs.get(node.name, 0) + 1
            ctx.setdefault("results", {})[node.name] = result
            self._save_state(node.name, result, runs[node.name])
            self.last_ctx = ctx
            events.emit("node_end", graph=self.g.name, node=node.name, run=runs[node.name],
                        seconds=round(time.monotonic() - t0, 3))
            self.log.debug("node '%s' fired (run %d, firings=%d, in_flight=%d)", node.name, runs[node.name], self.firings, self.in_flight)
            draining = self.error is not None
            for e in self.g.edges:
                if e.src != node.name:
                    continue
                if draining and not e.on_drain:
                    continue  # draining: only landing edges keep firing
                if e.when is None or e.when(result, ctx):
                    self._put(e.dst, node.name, ctx, e.on_drain)
            self._settle()