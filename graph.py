import asyncio
import logging
import time

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


class Graph:
    def __init__(self, name, max_steps=500, drain_timeout=1200):
        self.name = name
        self.max_steps = max_steps
        # After a node fails, sibling nodes already running are allowed to
        # finish (see _Execution) rather than being cancelled mid-flight. This
        # bounds that wait so one wedged node cannot hang the whole run.
        self.drain_timeout = drain_timeout
        self.nodes = {}
        self.edges = []
        self.starts = []

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
        self.done = asyncio.Event()
        self.error = None
        self.firings = 0
        self.drain_deadline = None

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
                events.emit("node_start", graph=self.g.name, node=node.name)
                result = await node.fn(ctx)
            except Exception as exc:
                events.emit("node_error", graph=self.g.name, node=node.name,
                            seconds=round(time.monotonic() - t0, 3), error=str(exc)[:300])
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