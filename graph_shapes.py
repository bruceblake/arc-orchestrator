"""Graph shapes: the pattern library as data, and the shape a taskfile has.

Every project runs TWO graphs (AGENTS.md § "Two graphs"):

* The per-task pipeline — `alloc → implement → gate → review → publish →
  PR review → merge`, with its fix loop and tier escalation. It is fixed, in
  code, in `code_tasks.build_code_graph`. Nobody chooses it per project.
* The graph BETWEEN tasks — which tasks exist, which run in parallel, which
  wait on which — is designed per project by the planner and written as the
  taskfile's `deps` (and `after`, between taskfiles). That is where a
  "pattern" lives: fan-out, diamond, chain, and so on are recipes for `deps`.

This module is the planner's catalogue of shapes for that second graph
(PATTERNS — the prose is a condensed docs/graph-patterns.md; where the two
differ the doc is stale), the classifier that says which shape a taskfile's
`deps` ACTUALLY form (`classify`), the prose handed to the planner
(`planner_prose`), and the dashboard's view of all of it (`describe` →
GET /api/graph-shapes).

The prose is hand-maintained. Everything numeric — fan-out widths, tier
names, caps — is read from `config` at call time: a width typed here would
have told the planner to fan out to a cap that changed the next week.
"""
import json
import re
from pathlib import Path

import config


# --- the catalogue ---------------------------------------------------------
# `sketch` is a small topology in the shape /api/graphs uses ({name, gather}
# nodes, {src, dst, conditional} edges, starts), so the dashboard draws a
# pattern with the same renderer as the real workload graphs.

def _sketch(starts, edges, gather=()):
    names = []
    for s, d in edges:
        for n in (s, d):
            if n not in names:
                names.append(n)
    for s in starts:
        if s not in names:
            names.insert(0, s)
    return {"starts": list(starts),
            "nodes": [{"name": n, "gather": n in gather} for n in names],
            "edges": [{"src": s, "dst": d, "conditional": False} for s, d in edges]}


PATTERNS = [
    {"id": "single", "name": "Single task", "aliases": ["one", "solo"],
     "gist": "One task; the per-task pipeline is the whole graph.",
     "when": "A small feature, an endpoint, a known-location bugfix, a doc "
             "page — anything one agent finishes in under 30 minutes.",
     "how": "One task, no deps. The evaluator-optimizer loop (gate → fix → "
            "review → fix) comes free with the pipeline; do not add a second "
            "task just to 'review' the first — the PR reviewers already do.",
     "pitfalls": ["Splitting one small change into two tasks doubles the "
                  "gate/review/PR cost for nothing."],
     "sketch": _sketch(["task"], [])},
    {"id": "chain", "name": "Chain / pipeline", "aliases": ["pipeline", "sequential"],
     "gist": "Each task consumes the previous task's merged output.",
     "when": "Staged migrations (schema → callers → cleanup), refactors that "
             "must land in order (introduce API → migrate call sites → delete "
             "old API), docs that must follow the code they describe.",
     "how": "deps chain: B deps [A], C deps [B]. Route each link by its own "
            "tier — a chain often mixes tiers. A dependent allocates only after "
            "its dep's PR has MERGED, so each link pays the full gauntlet.",
     "pitfalls": ["Never chain for stylistic order; if two links do not read "
                  "each other's code, unchain them and get parallelism back.",
                  "Keep chains ≤ 4 links — longer means the plan is hiding a "
                  "fan-out."],
     "sketch": _sketch(["A"], [("A", "B"), ("B", "C")])},
    {"id": "fanout", "name": "Fan-out / fan-in",
     "aliases": ["fan-out", "fan-out-fan-in", "fan-out/fan-in", "scatter-gather",
                 "parallel", "map-reduce", "orchestrator-workers", "supervisor"],
     "gist": "Independent tasks in parallel, optionally one integration task "
             "that waits for all of them.",
     "when": "Repo-wide mechanical change sliced by directory; greenfield "
             "builds whose modules are separable; anything where the subtasks "
             "do not read each other's code. The system's default shape.",
     "how": "Heads get deps []; they all start at t=0. An optional fan-in "
            "task lists EVERY head in its deps — a gather node waits for all "
            "of them to merge (a real join). Keep files_hint disjoint across "
            "the heads: two implementers in one file is a merge conflict.",
     "pitfalls": ["Width beyond a model's driver cap queues, it does not "
                  "fail — six hard tasks on one hard model is really two "
                  "batches; spread tiers or accept the latency.",
                  "The fan-in task must not redo the heads' work; give it an "
                  "integration verify_cmd and the seam to check."],
     "sketch": _sketch(["B", "C", "D"], [("B", "E"), ("C", "E"), ("D", "E")], gather=("E",))},
    {"id": "diamond", "name": "Diamond (split → verify → merge)",
     "aliases": ["split-verify-merge", "split-merge"],
     "gist": "A contract task, two or three parallel sides built on it, then "
             "one hard task that verifies the combination.",
     "when": "Risky core changes; pairs of interacting modules "
             "(producer/consumer) whose seam needs its own check; "
             "review-heavy work such as protocol or security code.",
     "how": "contract deps [] → side-a, side-b deps [contract] with disjoint "
            "files_hint → verify deps [side-a, side-b] routed hard, with an "
            "integration verify_cmd (the full suite on the merged base).",
     "pitfalls": ["Skip the contract link and both sides invent their own "
                  "interface; the verify task becomes a rewrite.",
                  "Two sides writing the same files merge-conflict — rivals "
                  "must live in separate paths (or use debate/vote)."],
     "sketch": _sketch(["contract"], [("contract", "side-a"), ("contract", "side-b"),
                                     ("side-a", "verify"), ("side-b", "verify")],
                       gather=("verify",))},
    {"id": "router", "name": "Router (probe → conditional branches)",
     "aliases": ["route", "classify", "probe", "two-phase", "conditional"],
     "gist": "A cheap probe writes a verdict; each branch task runs only if "
             "its `when` holds against it.",
     "when": "A bug whose location is unknown; a migration that needs an "
             "inventory first; any goal where the right tier or split cannot "
             "be read off the goal text.",
     "how": "ONE taskfile: a probe task with a `probe_cmd` that prints a JSON "
            "verdict after its gate passes (e.g. {\"area\": \"frontend\"}), then "
            "one task per branch with deps [probe] and `when`: {\"dep\": \"probe\", "
            "\"key\": \"area\", \"equals\": \"frontend\"}. Branches whose condition "
            "does not hold are recorded skipped, with everything downstream of "
            "them. When the probe needs a human or a whole plan of its own, use "
            "two taskfiles with the second `after` the first instead.",
     "pitfalls": ["A probe that lands code beyond the reproduction test "
                  "contaminates the branches' base — keep probes read-only-plus-test.",
                  "Every branch's `when` must be satisfiable by the probe's "
                  "verdict keys; a typo in `key` skips every branch."],
     "sketch": {"starts": ["probe"],
                "nodes": [{"name": "probe", "gather": False}, {"name": "fix-frontend", "gather": False},
                          {"name": "fix-backend", "gather": False}],
                "edges": [{"src": "probe", "dst": "fix-frontend", "conditional": True},
                          {"src": "probe", "dst": "fix-backend", "conditional": True}]}},
    {"id": "evaluator", "name": "Evaluator-optimizer (built in)",
     "aliases": ["evaluator-optimizer", "reflection", "critic", "fix-loop"],
     "gist": "Generate → evaluate → fix, bounded. Every task already runs it.",
     "when": "Automatic. The verify gate and the cross-family review each "
             "bounce a task back to its implementer with the failure as "
             "feedback, up to the fix budget, then escalate a tier.",
     "how": "Nothing to design — make it bite: an honest verify_cmd that "
            "prints the diagnosis early (gate output is truncated) and a "
            "prompt a reviewer can judge from the diff alone.",
     "pitfalls": ["A vague task ('make it better') loops to the budget, "
                  "escalates to the scarcest tier, and fails there."],
     "sketch": _sketch(["implement"], [("implement", "gate"), ("gate", "review"),
                                      ("review", "implement")])},
    {"id": "debate", "name": "Debate / vote (N candidates + judge)",
     "aliases": ["vote", "debate-vote", "voting", "candidates"],
     "gist": "The same spec implemented N ways in separate paths, then a hard "
             "judge task picks and lands one.",
     "when": "Design decisions with real disagreement — API shape, schema, "
             "algorithm choice — where one wrong pick is expensive.",
     "how": "N candidate tasks (different models, deps [], DISJOINT paths such "
            "as candidates/a/, candidates/b/), then judge deps [all "
            "candidates] routed hard: compare, choose, move the winner into "
            "place, delete the rest. The PR reviewers already vote on every "
            "task; this adds voting at generation time.",
     "pitfalls": ["Candidates writing the same files conflict — the judge "
                  "must be the only task that touches the real path.",
                  "Two candidates is the norm; three at most — more costs "
                  "budget, not information."],
     "sketch": _sketch(["cand-a", "cand-b"], [("cand-a", "judge"), ("cand-b", "judge")],
                       gather=("judge",))},
    {"id": "hierarchical", "name": "Hierarchical (rounds of plans)",
     "aliases": ["hierarchy", "nested", "planner-of-planners", "rounds"],
     "gist": "Contracts first, then a taskfile per module chained `after` it — "
             "a DAG of DAGs.",
     "when": "Large greenfield systems (> 8 natural tasks) and multi-subsystem "
             "work where the per-module plans only make sense once shared "
             "interfaces exist.",
     "how": "Round 1: one taskfile landing the shared contracts. Round 2: one "
            "taskfile per module, each `after: [round-1]`, each 2-8 tasks. "
            "Within a file, a head that fans out under a head and then joins "
            "is the same shape one level down.",
     "pitfalls": ["A planner writing all rounds at once guesses the module "
                  "plans before the contracts exist — plan round 2 after "
                  "round 1 merged."],
     "sketch": _sketch(["contracts"], [("contracts", "mod-a"), ("contracts", "mod-b"),
                                      ("mod-a", "a-1"), ("mod-a", "a-2"),
                                      ("mod-b", "b-1"), ("a-1", "integrate"),
                                      ("a-2", "integrate"), ("b-1", "integrate")],
                       gather=("integrate",))},
    {"id": "escalate", "name": "Retry + escalate (built in)",
     "aliases": ["retry", "escalation", "circuit-breaker", "saga"],
     "gist": "Bounded local retry, then the same task on the next tier up "
             "with the failure as feedback.",
     "when": "Automatic for every task. Know it when estimating latency: "
             "worst case per task is fix rounds × tiers.",
     "how": "Nothing to design. Reallocation resets the task branch to base, "
            "so a rejected attempt never leaks into the retry; the reviewer "
            "flips family with the implementer.",
     "pitfalls": ["Escalation assumes the failure was the model's fault; an "
                  "unknowable prompt climbs every tier and fails at the top."],
     "sketch": _sketch(["implement"], [("implement", "gate"), ("gate", "implement"),
                                      ("gate", "escalate"), ("escalate", "implement")])},
]

PATTERN_IDS = [p["id"] for p in PATTERNS]
_ALIAS = {}
for _p in PATTERNS:
    _ALIAS[_p["id"]] = _p["id"]
    for _a in _p["aliases"]:
        _ALIAS[_a] = _p["id"]


def normalize_pattern(name):
    """The canonical id for a planner-written pattern name, or None.

    Planners write 'fan-out-fan-in', 'Fan out', 'orchestrator-workers' —
    all the same shape. Unknown names stay unknown (the loader keeps them as
    written and logs it); nothing is rejected over a label."""
    if not isinstance(name, str) or not name.strip():
        return None
    key = re.sub(r"[\s_]+", "-", name.strip().lower())
    return _ALIAS.get(key) or _ALIAS.get(key.replace("-", ""))


# --- decision table (goal kind → shape) --------------------------------------
# Mirrors the table at the end of docs/graph-patterns.md; the planner reads
# this form. `tasks` is the typical implement-task count.
DECISIONS = [
    ("small feature / endpoint / known-location bugfix", "single", "1"),
    ("greenfield build with separable modules", "fanout", "3-8"),
    ("repo-wide mechanical change (rename, lint, migration by directory)", "fanout", "3-6"),
    ("delicate refactor / architecture change", "chain", "2-4"),
    ("staged migration (introduce → migrate → delete)", "chain", "3-5"),
    ("docs that must follow code", "chain", "1-3"),
    ("interacting modules with a risky seam; protocol or security code", "diamond", "3-5"),
    ("bugfix, unknown location / spike", "router", "1 + 1-3 after"),
    ("design decision with real alternatives", "debate", "3-4"),
    ("very large build (> 8 natural tasks)", "hierarchical", "rounds of 2-8"),
]


# --- classification: what shape do these deps actually form? -----------------

def classify(taskset):
    """The shape a taskfile's `deps` form, derived from the deps alone.

    Returns {shape, width, depth, heads, joins, n_tasks, reason, declared,
    mismatch}. `declared` is the planner's own label (project.pattern,
    normalized); `mismatch` says the deps do not form that shape — the
    dashboard shows it, and `main.py code plan` prints it, because a plan
    that says 'diamond' and wires a chain has a bug the planner should see.

    Rules (deps between the file's own ids only; unknown ids are ignored):
      single       one task
      chain        every task has ≤ 1 dep and ≤ 1 dependent, one path
      fanout       no task has ≥ 2 deps: parallel heads, possibly with
                   single-dep tails hanging off them
      diamond      exactly one head, and one join whose branches all
                   descend from it (split → … → merge)
      hierarchical fan-out under a fan-out with a join, or several joins
      fanin        several heads and one join, no shared root
      mixed        anything else
    """
    tasks = taskset.get("tasks") if isinstance(taskset, dict) else taskset
    if isinstance(tasks, dict):
        tasks = list(tasks.values())
    tasks = [t for t in (tasks or []) if isinstance(t, dict) and t.get("id")]
    ids = [t["id"] for t in tasks]
    idset = set(ids)
    deps = {t["id"]: [d for d in (t.get("deps") or t.get("depends") or []) if d in idset]
            for t in tasks}
    conditional = {t["id"] for t in tasks if isinstance(t.get("when"), dict) and t["when"]}
    children = {i: [] for i in ids}
    for tid, ds in deps.items():
        for d in ds:
            children[d].append(tid)
    heads = [i for i in ids if not deps[i]]
    joins = [i for i in ids if len(deps[i]) >= 2]
    # level = longest path from a head (deps form a DAG; the loader rejects
    # cycles, but guard anyway so a bad file cannot spin this).
    level = {}

    def lvl(i, seen=()):
        if i in level:
            return level[i]
        if i in seen or not deps[i]:
            level[i] = 0
            return 0
        level[i] = 1 + max(lvl(d, seen + (i,)) for d in deps[i])
        return level[i]

    for i in ids:
        lvl(i)
    depth = (max(level.values()) + 1) if ids else 0
    by_level = {}
    for i, l in level.items():
        by_level[l] = by_level.get(l, 0) + 1
    width = max(by_level.values()) if by_level else 0
    n = len(ids)

    declared_raw = (taskset.get("pattern") if isinstance(taskset, dict) else None) or ""
    declared = normalize_pattern(declared_raw)

    if n == 0:
        shape, reason = "empty", "no tasks"
    elif n == 1:
        shape, reason = "single", "one task — the per-task pipeline is the whole graph"
    elif conditional:
        # Conditional deps (task.when): the branches are decided by a probe's
        # verdict at run time, so this is a router whatever else the deps do.
        probes = sorted({t.get("when", {}).get("dep") for t in tasks if t["id"] in conditional})
        shape = "router"
        reason = (f"{len(conditional)} task(s) run only if a verdict holds — "
                  f"routed by {', '.join(p for p in probes if p)}")
    elif not joins and all(len(children[i]) <= 1 for i in ids) and len(heads) == 1:
        shape, reason = "chain", f"{n} tasks in one path — each waits for the previous PR to merge"
    elif not joins:
        tails = n - len(heads)
        reason = ((f"{len(heads)} independent heads start at t=0" if len(heads) > 1
                   else f"one head ({heads[0]}) starts at t=0")
                  + (f", {tails} single-dep task(s) fan out under {'them' if len(heads) > 1 else 'it'}"
                     if tails else ", no deps"))
        shape = "fanout"
    else:
        # ancestors of each join
        def ancestors(i):
            out, stack = set(), list(deps[i])
            while stack:
                d = stack.pop()
                if d in out:
                    continue
                out.add(d)
                stack.extend(deps[d])
            return out
        # A join may redundantly list the root itself (scaffold, a, b, c →
        # integrate); the sides are its other deps.
        sides = [d for d in deps[joins[0]] if d != heads[0]] if len(joins) == 1 else []
        if len(heads) == 1 and len(joins) == 1 and heads[0] in ancestors(joins[0]) \
                and len(sides) >= 2 and depth <= 3:
            shape = "diamond"
            reason = (f"one head ({heads[0]}) splits into {len(sides)} sides that join at "
                      f"{joins[0]}")
        elif len(heads) == 1 and (len(joins) > 1 or depth > 3):
            shape = "hierarchical"
            reason = (f"one root, {len(joins)} join(s), depth {depth} — fan-out under fan-out")
        elif len(heads) >= 2 and len(joins) == 1 and level[joins[0]] == depth - 1:
            shape = "fanin"
            reason = f"{len(heads)} heads start at t=0 and join at {joins[0]}"
        elif len(joins) >= 1 and len(heads) >= 2:
            shape = "hierarchical"
            reason = f"{len(heads)} heads, {len(joins)} join(s), depth {depth}"
        else:
            shape = "mixed"
            reason = f"{len(heads)} head(s), {len(joins)} join(s), depth {depth}"
    # A declared 'fanout' with a fan-in is still a fan-out; 'fanin' is the
    # detected name for what the catalogue calls fan-out/fan-in.
    same = {"fanin": "fanout"}.get(shape, shape)
    mismatch = bool(declared) and declared not in ("evaluator", "escalate") \
        and declared != same and not (declared == "fanout" and shape == "single")
    return {"shape": shape, "width": width, "depth": depth, "heads": heads,
            "joins": joins, "n_tasks": n, "reason": reason,
            "declared": declared, "declared_raw": declared_raw or None,
            "mismatch": mismatch}


# --- the planner's copy -------------------------------------------------------

def _caps():
    """Per-model fan-out width the fleet can actually run, from config."""
    out = []
    for m in config.ESCALATION_PATH:
        try:
            out.append((m, config.driver_limit(m)))
        except Exception:
            continue
    return out


def planner_prose():
    """The GRAPH DESIGN block of the planner prompt, written from PATTERNS and
    today's caps. One source for the catalogue: the doc, the dashboard and the
    planner cannot drift apart if they all read this."""
    lines = ["GRAPH DESIGN — you are choosing the graph BETWEEN tasks:",
             "- Every task already runs the fixed per-task pipeline (worktree → "
             "implement → verify gate → cross-family review → push → PR → "
             f"{config.PR_REVIEWERS} independent PR reviews → merge) with a bounded "
             "fix loop and tier escalation. You never design that. Your output "
             "is the shape ABOVE it: which tasks exist, which run in parallel "
             "(no deps), which wait (deps), and which whole projects wait on "
             "others (after).",
             "- Pick ONE shape from this catalogue by the kind of work, write it "
             "as \"pattern\": \"<id>\" in the project object, and make the deps "
             "actually form it (the loader checks and reports a mismatch):"]
    for p in PATTERNS:
        if p["id"] in ("evaluator", "escalate"):
            continue  # built in — mentioned once below, not a choice
        lines.append(f"    {p['id']}: {p['gist']} WHEN {p['when']} HOW {p['how']}")
    lines.append("  Built in, never chosen: evaluator (gate/review fix loop) and "
                 "escalate (tier up on repeated failure) run inside every task.")
    lines.append("- Decision table (kind of work → pattern, typical implement tasks):")
    for kind, pid, n in DECISIONS:
        lines.append(f"    {kind} → {pid} ({n})")
    caps = ", ".join(f"{m} {n}" for m, n in _caps())
    lines.append("- Fan-out arithmetic: a task waits for a free driver slot, it does "
                 f"not fail. Slots today: {caps}. A fan-out wider than a model's "
                 "slots is really batches — spread tiers or accept the latency.")
    lines.append("- Joins are real: a task with several deps waits for EVERY one of "
                 "them to merge (a gather node). List all of them; order does not matter.")
    lines.append("- Conditional branches: give a probe task a \"probe_cmd\" (a shell "
                 "command run in its worktree after its gate passes, printing one "
                 "JSON object) and give each branch task deps [probe] plus \"when\": "
                 "{\"dep\": \"probe\", \"key\": \"<field>\", \"equals\": <value>} "
                 "(or \"in\": [...], \"truthy\": true, \"exists\": true). A branch whose "
                 "condition does not hold is skipped, with everything downstream. "
                 "Use this instead of emitting every branch unconditionally.")
    lines.append("- deps only when a task truly reads code another task writes. "
                 "Never chain for stylistic order — chains serialize the fleet.")
    lines.append("- Keep files_hint disjoint across tasks that run in parallel; two "
                 "implementers in one file is a merge conflict and a failed task.")
    lines.append("- A whole project may wait on another: \"after\": [\"<taskfile "
                 "name>\"] in the project object holds this plan (no worktree at "
                 "all) until every task of that taskfile is merged. Use it when the "
                 "goal builds on a project listed below that has not merged yet, "
                 "and for router/hierarchical second phases.")
    return "\n".join(lines) + "\n\n"


# --- the dashboard's copy ---------------------------------------------------

def _engine():
    """What the graph engine supports today, checked against graph.py."""
    import graph as g
    import code_tasks as ct
    have = [
        ("conditional edges", "graph.Edge when=", hasattr(g.Edge, "__init__") and
         "when" in g.Edge.__init__.__code__.co_varnames,
         "an edge that fires only when its predicate holds — the per-task pipeline's gate/review/escalate branches"),
        ("joins", "graph.Node gather=True", "gather" in g.Node.__init__.__code__.co_varnames,
         "a node that waits for every incoming branch — what a task with several deps gets"),
        ("dynamic fan-out", "graph.Spawn", hasattr(g, "Spawn"),
         "one node spawning N children decided at runtime — the pipeline's pr_fanout (one child per PR reviewer)"),
        ("retry and timeout per node", "graph.Retry / Node timeout=",
         hasattr(g, "Retry") and "timeout" in g.Node.__init__.__code__.co_varnames,
         "bounded retries with backoff; the fix loop and tier escalation are built on it"),
        ("subgraphs", "graph.Graph.subgraph", hasattr(g.Graph, "subgraph"),
         "a whole graph as one node of another"),
        ("taskfile chaining", "project.after → chain_wait", True,
         "a project that allocates nothing until every task of another taskfile is merged"),
        ("conditional deps between tasks", "task.when → Edge when= on the dep's probe verdict",
         hasattr(ct, "when_holds"),
         "a task runs only if a dependency's probe_cmd verdict satisfies its `when`; otherwise it and everything downstream are recorded skipped — a one-taskfile router"),
    ]
    missing = [
        ("tasks spawning tasks",
         "a task cannot add tasks to its own taskfile at runtime — the planner decides the set once; hierarchical work is rounds of taskfiles"),
        ("loops between tasks",
         "deps must be acyclic; the only loops are inside a task (gate → fix, review → fix) with a round budget"),
    ]
    return {"has": [{"name": n, "where": w, "note": note} for n, w, ok, note in have if ok],
            "missing": [{"name": n, "note": note} for n, note in missing]}


def describe(tasks_dir=None):
    """Patterns + every taskfile classified + what the engine can do."""
    d = Path(tasks_dir or config.TASKS_DIR)
    projects = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            proj = json.loads(f.read_text(encoding="utf-8", errors="replace")).get("project") or {}
        except Exception:
            continue
        cls = classify(proj)
        projects.append({"file": f.name, "title": proj.get("title") or f.stem,
                         "repo": proj.get("repo"), "n_tasks": cls["n_tasks"],
                         "declared": cls["declared"], "declared_raw": cls["declared_raw"],
                         "detected": cls,
                         "after": [Path(a).name for a in (proj.get("after") or [])
                                   if isinstance(a, str)]})
    caps = _caps()
    patterns = []
    for p in PATTERNS:
        q = dict(p)
        q["used_by"] = [x["file"] for x in projects
                        if {"fanin": "fanout"}.get(x["detected"]["shape"], x["detected"]["shape"]) == p["id"]
                        or x["declared"] == p["id"]]
        patterns.append(q)
    return {"patterns": patterns, "decisions": [
                {"kind": k, "pattern": p, "tasks": n} for k, p, n in DECISIONS],
            "projects": projects, "caps": [{"model": m, "slots": n} for m, n in caps],
            "engine": _engine()}
