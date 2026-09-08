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
    def __init__(self, src, dst, when=None):
        self.src = src
        self.dst = dst
        self.when = when


class Graph:
    def __init__(self, name, max_steps=500):
        self.name = name
        self.max_steps = max_steps
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

    def edge(self, src, dst, when=None):
        self.edges.append(Edge(src, dst, when))

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

    def _sources_of(self, name):
        return {e.src for e in self.g.edges if e.dst == name}

    def _put(self, name, src, ctx):
        ctx = dict(ctx)
        ctx["results"] = dict(ctx.get("results", {}))
        self.queues[name].put_nowait((src, ctx))
        self.in_flight += 1

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
            await self.done.wait()
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
            src, ctx = await q.get()
            if self.error is not None:
                self.in_flight -= 1
                if self.in_flight <= 0:
                    self.done.set()
                continue
            t0 = time.monotonic()
            try:
                if node.gather:
                    self.gathered[node.name][src] = ctx
                    self.gather_pending[node.name].discard(src)
                    if self.gather_pending[node.name]:
                        self.log.debug("gather '%s' waiting for %s", node.name, sorted(self.gather_pending[node.name]))
                        self.in_flight -= 1
                        continue
                    ctx = self._merge(list(self.gathered[node.name].values()))
                    self.gathered[node.name] = {}
                    self.gather_pending[node.name] = self._sources_of(node.name)
                events.emit("node_start", graph=self.g.name, node=node.name)
                result = await node.fn(ctx)
            except Exception as exc:
                self.in_flight -= 1
                if self.error is None:
                    self.error = exc
                    self.log.error("node '%s' failed: %s", node.name, exc)
                events.emit("node_error", graph=self.g.name, node=node.name,
                            seconds=round(time.monotonic() - t0, 3), error=str(exc)[:300])
                self.done.set()
                continue
            self.firings += 1
            if self.firings > self.g.max_steps:
                self.in_flight -= 1
                if self.error is None:
                    self.error = GraphError(f"graph '{self.g.name}' exceeded max_steps={self.g.max_steps}")
                self.done.set()
                continue
            runs = ctx.setdefault("runs", {})
            runs[node.name] = runs.get(node.name, 0) + 1
            ctx.setdefault("results", {})[node.name] = result
            self.last_ctx = ctx
            events.emit("node_end", graph=self.g.name, node=node.name, run=runs[node.name],
                        seconds=round(time.monotonic() - t0, 3))
            self.log.debug("node '%s' fired (run %d, firings=%d, in_flight=%d)", node.name, runs[node.name], self.firings, self.in_flight)
            for e in self.g.edges:
                if e.src != node.name:
                    continue
                if e.when is None or e.when(result, ctx):
                    self._put(e.dst, node.name, ctx)
            self.in_flight -= 1
            if self.in_flight <= 0 and self.error is None:
                self.done.set()