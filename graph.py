import asyncio
import json
import logging
import time

import errors
import events


class GraphError(RuntimeError):
    pass


class Retry:
    """Per-node retry policy — LangGraph's RetryPolicy, Temporal's RetryOptions.

    Retries belonged to the driver layer before this, so a node that failed for
    a transient reason (a flaky subprocess, a lock held for a moment) drained
    the whole graph. ``on`` restricts which exceptions are retried; anything
    else propagates at once, because retrying a programming error is how a bug
    gets to run three times.
    """

    def __init__(self, attempts=3, backoff=1.0, max_backoff=30.0, on=(Exception,)):
        self.attempts = max(1, int(attempts))
        self.backoff = float(backoff)
        self.max_backoff = float(max_backoff)
        self.on = tuple(on)

    def delay(self, attempt):
        return min(self.max_backoff, self.backoff * (2 ** (attempt - 1)))


class Spawn:
    """Dynamic fan-out — LangGraph's Send(), Prefect's .map().

    A node returns Spawn(target, items, join) and the engine schedules
    ``target`` once PER ITEM, in parallel, each with ``ctx["spawn"]`` set to
    that item. When every child has finished, ``join`` fires once with
    ``ctx["results"][target]`` set to the LIST of child results in item order.

    Width is decided at runtime from data, not drawn in advance. The PR
    reviewers used to fan out inside a single node via asyncio.gather, which
    made them invisible to the graph: not in the diagram, not checkpointed,
    not individually retryable, and a crashed reviewer surfaced only as a
    field on its parent's result.
    """

    def __init__(self, target, items, join, result=None):
        self.target = target
        self.items = list(items)
        self.join = join
        # What the SPAWNING node itself records as its result.
        self.result = result if result is not None else {"spawned": len(self.items)}


class Node:
    def __init__(self, name, fn, gather=False, retry=None, timeout=None,
                 on_error=None):
        self.name = name
        self.fn = fn
        self.gather = gather
        self.retry = retry
        # Seconds; None = unbounded. A node with no timeout can hold the graph
        # open forever — the fleet's drivers had one, the graph's nodes did not.
        self.timeout = timeout
        # Name of a node to run INSTEAD of draining when this one fails after
        # its retries. It receives ctx["error"] = {node, message, attempts}.
        self.on_error = on_error


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

    def node(self, name, fn=None, *, gather=False, retry=None, timeout=None,
             on_error=None):
        def register(f):
            if name in self.nodes:
                raise GraphError(f"duplicate node: {name}")
            self.nodes[name] = Node(name, f, gather, retry, timeout, on_error)
            return f

        return register(fn) if fn is not None else register

    def subgraph(self, name, inner, *, retry=None, timeout=None, on_error=None):
        """A node that is itself a graph.

        Runs ``inner`` with the current ctx and records its final results under
        this node's name. Errors inside propagate as this node's failure, so
        the parent's retry/on_error apply to the whole sub-run — which is the
        point: a task chain becomes one retryable, drainable unit.
        """
        async def run_inner(ctx):
            sub = dict(ctx)
            sub["results"] = dict(ctx.get("results", {}))
            out = await inner.run(sub)
            return {"subgraph": inner.name, "results": out.get("results", {})}
        return self.node(name, run_inner, retry=retry, timeout=timeout, on_error=on_error)

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
        for n in self.nodes.values():
            if n.on_error is not None and n.on_error not in self.nodes:
                raise GraphError(f"{n.name}.on_error names unknown node: {n.on_error}")
        reachable = set()
        frontier = list(self.starts) + [n.on_error for n in self.nodes.values() if n.on_error]
        while frontier:
            n = frontier.pop()
            if n in reachable:
                continue
            reachable.add(n)
            frontier.extend(e.dst for e in self.edges if e.src == n)
        # A Spawn target/join is reached at runtime; declare an edge from the
        # spawning node so validate() can see them.
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
        # Dynamic fan-out bookkeeping: group id -> {join, expected, target, got}
        self.spawns = {}
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

    async def _invoke(self, node, ctx):
        """Run one node with its retry policy and timeout.

        Only exceptions in ``retry.on`` are retried; a programming error
        propagates on the first attempt, because running a bug three times is
        not resilience. Each retry emits node_retry so the dashboard can show a
        node that is struggling rather than a node that is merely slow.
        """
        attempts = node.retry.attempts if node.retry else 1
        for attempt in range(1, attempts + 1):
            try:
                if node.timeout:
                    return await asyncio.wait_for(node.fn(ctx), node.timeout)
                return await node.fn(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Convert BEFORE deciding retryability: a policy written as
                # on=(GraphError,) must catch a timeout, and the raw
                # asyncio.TimeoutError is not a GraphError.
                if isinstance(exc, asyncio.TimeoutError):
                    exc = GraphError(f"node '{node.name}' timed out after {node.timeout}s")
                retryable = (node.retry is not None
                             and isinstance(exc, node.retry.on)
                             and attempt < attempts)
                if not retryable:
                    raise exc
                delay = node.retry.delay(attempt)
                events.emit("node_retry", graph=self.g.name, node=node.name,
                            attempt=attempt, of=attempts, error=str(exc)[:200],
                            retry_in_s=round(delay, 1))
                self.log.warning("node '%s' attempt %d/%d failed (%s); retrying in %.1fs",
                                 node.name, attempt, attempts, exc, delay)
                await asyncio.sleep(delay)

    def _spawn(self, node, sp, ctx):
        """Schedule one child per item and register the join."""
        import uuid
        if sp.target not in self.g.nodes or sp.join not in self.g.nodes:
            raise GraphError(f"Spawn from '{node.name}' names unknown node(s): "
                             f"{sp.target!r} -> {sp.join!r}")
        gid = uuid.uuid4().hex[:12]
        self.spawns[gid] = {"join": sp.join, "target": sp.target,
                            "expected": len(sp.items), "got": {}, "of": node.name}
        events.emit("node_spawn", graph=self.g.name, node=node.name,
                    target=sp.target, join=sp.join, n=len(sp.items))
        if not sp.items:
            # Nothing to fan out to: the join fires at once with an empty list.
            jctx = dict(ctx)
            jctx["results"] = dict(ctx.get("results", {}))
            jctx["results"][sp.target] = []
            self._put(sp.join, node.name, jctx)
            return
        for i, item in enumerate(sp.items):
            child = dict(ctx)
            child["results"] = dict(ctx.get("results", {}))
            child["spawn"] = item
            child["spawn_index"] = i
            child["spawn_group"] = gid
            child["spawn_of"] = node.name
            self._put(sp.target, node.name, child)

    def _collect_spawn(self, node, ctx, result):
        """A spawned child finished: record it; fire the join when all are in.

        Returns True if this result was consumed by a spawn group (so the
        normal edge-firing must NOT also run for it).
        """
        gid = ctx.get("spawn_group")
        grp = self.spawns.get(gid) if gid else None
        if grp is None or grp["target"] != node.name:
            return False
        grp["got"][ctx.get("spawn_index", len(grp["got"]))] = result
        if len(grp["got"]) < grp["expected"]:
            return True
        del self.spawns[gid]
        ordered = [grp["got"][i] for i in sorted(grp["got"])]
        jctx = dict(ctx)
        jctx["results"] = dict(ctx.get("results", {}))
        jctx["results"][node.name] = ordered  # the fan-in reducer: a LIST
        for k in ("spawn", "spawn_index", "spawn_group", "spawn_of"):
            jctx.pop(k, None)
        events.emit("node_join", graph=self.g.name, node=grp["join"],
                    target=node.name, n=len(ordered))
        self._put(grp["join"], node.name, jctx)
        return True

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
                        result = await self._invoke(node, ctx)
                    finally:
                        self.admission.release()
                else:
                    events.emit("node_start", graph=self.g.name, node=node.name)
                    result = await self._invoke(node, ctx)
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
                if node.on_error and self.error is None:
                    # Route the failure instead of draining the graph. The
                    # handler sees what failed and why, and decides what next.
                    hctx = dict(ctx)
                    hctx["results"] = dict(ctx.get("results", {}))
                    hctx["error"] = {"node": node.name, "message": str(exc)[:500],
                                     "kind": type(exc).__name__, "fingerprint": fp}
                    events.emit("node_handled", graph=self.g.name, node=node.name,
                                handler=node.on_error)
                    self._put(node.on_error, node.name, hctx, on_drain)
                    self._settle()
                    continue
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
            # A node may hand back a Spawn instead of a plain result.
            spawn = result if isinstance(result, Spawn) else None
            if spawn is not None:
                result = spawn.result
            runs = ctx.setdefault("runs", {})
            runs[node.name] = runs.get(node.name, 0) + 1
            ctx.setdefault("results", {})[node.name] = result
            self._save_state(node.name, result, runs[node.name])
            self.last_ctx = ctx
            events.emit("node_end", graph=self.g.name, node=node.name, run=runs[node.name],
                        seconds=round(time.monotonic() - t0, 3))
            self.log.debug("node '%s' fired (run %d, firings=%d, in_flight=%d)", node.name, runs[node.name], self.firings, self.in_flight)
            # Everything after the node fn — spawn bookkeeping, join collection,
            # edge predicates — used to run OUTSIDE the try/except. An exception
            # there (a `when=` lambda hitting a missing key, a Spawn naming an
            # unknown node) escaped the worker coroutine, killed it silently, and
            # left in_flight un-settled: the graph hung forever with no error.
            # That defect predates Spawn; Spawn made it reachable from a test.
            try:
                draining = self.error is not None
                if spawn is not None and not draining:
                    self._spawn(node, spawn, ctx)
                elif self._collect_spawn(node, ctx, result):
                    pass  # a spawned child: its join, not its edges, decides what is next
                else:
                    for e in self.g.edges:
                        if e.src != node.name:
                            continue
                        if draining and not e.on_drain:
                            continue  # draining: only landing edges keep firing
                        if e.when is None or e.when(result, ctx):
                            self._put(e.dst, node.name, ctx, e.on_drain)
            except Exception as exc:
                fp = errors.capture(exc, node=node.name, graph=self.g.name,
                                    where=f"graph:{node.name}:routing")
                events.emit("node_error", graph=self.g.name, node=node.name,
                            error=f"routing after {node.name}: {exc}"[:300],
                            fingerprint=fp)
                self._fail(exc, node.name)
            self._settle()