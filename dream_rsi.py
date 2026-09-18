"""Dream-RSI: improve *how* the fleet explores, by replaying recorded history.

Based on arXiv:2609.14858v1, "Dream-RSI: Recursive Self-Improvement through
Evolving Worlds". The paper's thesis, in this repo's terms:

  Doing the work is one loop; deciding HOW to explore (which branches to open,
  how many attempts to run in parallel, when to stop) is a second, META loop.
  Feedback on an exploration policy is delayed and expensive — you only learn
  whether a strategy was good after a whole run. But a COMPLETED run is itself
  a *replay simulator*: every attempt's outcome is already recorded, so an
  alternative policy can be scored by RE-READING recorded outcomes, with no
  model calls. One expensive online run yields many cheap off-policy
  evaluations ("dreaming"). The best policy is redeployed and the next run
  grows the history — a recursive self-improvement loop at the exploration
  layer.

This module implements the three pieces the paper needs, against the records
the orchestrator ALREADY writes (store.code_tasks + store.harness_runs):

  1. ``build_tree``      — a taskfile run's recorded history as a discovery
                           tree (root + per-attempt nodes, each with one parent,
                           a score ``s_v`` and a cost).
  2. ``replay`` / ``replay_score`` — evaluate one policy on one tree the way
                           the paper's Eq. 1 does:
                               V = max(revealed score) − β1·N + β2·N/max(1,k)
                           where N = revealed non-root attempts, k = decision
                           rounds. Deterministic; reads only recorded fields.
  3. ``improve`` / ``select`` — evaluate a candidate set of policies across
                           every recorded tree and pick the argmax. Because the
                           incumbent is always in the candidate set, the
                           selection is monotonically non-worsening on the
                           fixed history (paper §3, "Policy improvement and
                           selection").

The LLM "policy-development agent" (§3) is a pluggable hook (``propose_source``):
given the replay trajectories it may author a NEW candidate policy. Its output
is only ever *evaluated on replay* and selected by the same argmax — it is never
executed online except by being the top-scoring candidate, exactly as the paper
restricts the changing surface to the policy code.

Design notes grounded in the repo:
  * The "policy" here does not choose model calls at run time — the DAG engine
    has no runtime task-spawning (graph_shapes declares this). It chooses the
    ORDER and BATCHING of a fixed set of recorded branches, which is precisely
    what replay can evaluate without executing anything.
  * Scores are synthesised from outcomes the repo records per attempt (gate
    pass, review verdict, escalation, merge). The paper learns a scalar
    ``s_v``; here it is derived, and the derivation is one function
    (``attempt_score``) so it can be replaced when a native score lands.
  * History is read from SQLite (store), NOT logs/events.jsonl: the event log
    rotates at 100 MiB (events._ROTATE_BYTES) and only ~2 files survive, while
    code_tasks/harness_runs are durable.
"""
from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import config

log = __import__("logging").getLogger("dream-rsi")


# --------------------------------------------------------------------------
# Objective coefficients (paper Eq. 1: V = quality − β1·N + β2·N/max(1,k)).
# Read from config at call time so an operator can retune without an import
# dance; defaults mirror the paper's shape (reward quality, penalise attempts,
# reward batching).
# --------------------------------------------------------------------------
def beta1() -> float:
    return config.DREAM_BETA1


def beta2() -> float:
    return config.DREAM_BETA2


def workers() -> int:
    """W — the batch width a policy may open per decision (paper's workers).

    Defaults to the fleet's graph admission cap so a replayed batch is one a
    real run could actually hold; ARC_DREAM_WORKERS overrides it.
    """
    w = config.DREAM_WORKERS
    if w > 0:
        return w
    try:
        return max(1, config.max_tasks_in_flight())
    except Exception:                            # noqa: BLE001 - config drift
        return 4


# --------------------------------------------------------------------------
# The discovery tree.
# --------------------------------------------------------------------------
@dataclass
class Node:
    """One attempt in the recorded history — the paper's discovery-tree node.

    ``id`` is stable and unique within a tree (``<task_id>-x<attempt>`` for a
    non-root attempt; ``root`` for the root). ``parent`` is the single primary
    parent: for the first attempt of a task it is the root (the base workspace)
    or the node of its last-merged dependency; for a rework attempt it is the
    previous attempt of the SAME task (a rework continues the previous
    attempt's worktree/session — code_tasks._resume_session / _rework_feedback).
    """
    id: str
    parent: Optional[str]
    task_id: str
    model: str
    score: float
    cost_s: float
    tokens: int = 0
    attempt: int = 1
    role: str = "implementer"
    outcome: str = ""          # gate_pass | gate_fail | review_reject | merged | ...
    # The paper's replay may only rest on PREFIX-observable information; these
    # are the fields a policy is allowed to read while deciding.
    revealed: bool = False


@dataclass
class DiscoveryTree:
    """A recorded run as a replay world (paper §3, "discovery trees")."""
    name: str
    nodes: dict[str, Node] = field(default_factory=dict)

    def add(self, node: Node) -> None:
        self.nodes[node.id] = node

    def root_children(self) -> list[str]:
        return [n.id for n in self.nodes.values() if n.parent == "root"]

    def children_of(self, node_id: str) -> list[str]:
        return [n.id for n in self.nodes.values() if n.parent == node_id]

    def has_unrevealed_root_child(self) -> bool:
        return any(not self.nodes[c].revealed for c in self.root_children())

    def revealed(self) -> set[str]:
        return {nid for nid, n in self.nodes.items() if n.revealed}

    def eligible(self) -> list[str]:
        """Actionable continuation points — paper A(T), restricted to real moves.

        The paper defines A(T) = {r} ∪ {leaves of the observed tree}, and a
        selected node reveals its recorded child. Over a FINITE recording a
        revealed leaf with no still-unrevealed child has nothing to reveal —
        selecting it is a no-op (``Child(...) = ∅``, §3), not an action. We
        therefore offer only nodes that can actually advance the tree: the root
        while it has an unopened branch, plus any revealed node with an
        unrevealed recorded child. Without this a policy spins on dead ends
        until the round limit and every candidate scores identically.
        """
        rev = self.revealed()
        out = []
        if "root" in rev and self.has_unrevealed_root_child():
            out.append("root")
        for nid in sorted(rev):
            if nid == "root":
                continue
            if any(c not in rev for c in self.children_of(nid)):
                out.append(nid)
        return out

    def best_score(self, ids: Optional[Iterable[str]] = None) -> float:
        ids = list(ids) if ids is not None else [n for n in self.revealed()
                                                 if n != "root"]
        if not ids:
            return 0.0
        return max(self.nodes[i].score for i in ids if i in self.nodes)

    def non_root_revealed(self) -> int:
        return sum(1 for nid in self.revealed() if nid != "root")

    def reset(self) -> None:
        """Paper: replay resets per-rollout state; only the root is revealed."""
        for n in self.nodes.values():
            n.revealed = (n.id == "root")


# --------------------------------------------------------------------------
# Small coercers so malformed recorded rows never take the replay down.
# --------------------------------------------------------------------------
def _as_int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(v) -> Optional[datetime]:
    """Parse a recorded ISO-8601 timestamp; None if absent or unparseable.

    Naive timestamps (one legacy row is space-separated and tz-less) are read
    as UTC so comparing them against aware rows never raises.
    """
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def _run_in_taskfile_window(run_ts: Optional[datetime],
                            windows: list[tuple]) -> bool:
    """Whether a run's timestamp falls in one taskfile's [created, finished].

    Two taskfiles can reuse a task id (verified in the real store: 7 ids do),
    and ``harness_runs`` carries no taskfile column, so the id alone cannot say
    which run belongs to which. But the task's own ``code_tasks`` row brackets
    its harness runs — every one of the 916 recorded runs falls inside exactly
    its row's window — and two rows for the same id are sequential (the store
    rejects a duplicate (id, taskfile)). That window is the disambiguator. An
    unparseable/absent timestamp is accepted rather than dropped.
    """
    if run_ts is None:
        return True
    for start, finish in windows:
        if (start is None or run_ts >= start) and \
                (finish is None or run_ts <= finish):
            return True
    return False


def _closed_windows(task_rows: list[dict]) -> dict[str, list[tuple]]:
    """Same-id taskfile windows with an open end bounded by the NEXT start.

    A task whose row has ``finished_at IS NULL`` (still running/abandoned; 2
    such rows exist in the real store) would otherwise have an OPEN-ENDED
    window that absorbs a later taskfile's runs for a reused id. Two rows for
    one id are sequential, so an open window ends where the next one begins.
    """
    raw: dict[str, list[tuple]] = {}
    for t in task_rows:
        raw.setdefault(t["id"], []).append(
            (_ts(t.get("created_at")), _ts(t.get("finished_at"))))
    out: dict[str, list[tuple]] = {}
    for tid, wins in raw.items():
        wins = sorted(wins, key=lambda w: (w[0] is None, w[0]))
        fixed = []
        for i, (start, finish) in enumerate(wins):
            if finish is None and i + 1 < len(wins):
                finish = wins[i + 1][0]        # next taskfile's start bounds it
            fixed.append((start, finish))
        out[tid] = fixed
    return out


def _as_verdict(verdict):
    """A recorded verdict as a dict, or None.

    A verdict column is either a dict (from a live row) or a JSON string, and
    reviewer verdicts are truncated to 500 chars on write (code_tasks.py), so a
    long issues list can cut the JSON off mid-object — a parse failure here is
    expected, not exceptional, and simply scores as 'no verdict'.
    """
    if isinstance(verdict, dict):
        return verdict
    if isinstance(verdict, str):
        try:
            v = json.loads(verdict)
        except (ValueError, TypeError):
            return None
        return v if isinstance(v, dict) else None
    return None


def _issue_count(v: dict) -> int:
    """len(issues) when issues is a list; anything else counts as 0.

    A truthy non-list (an int, a dict, a bare string) has no length semantics
    here, and `len()` on it would raise inside a scoring pass.
    """
    issues = v.get("issues")
    return len(issues) if isinstance(issues, list) else 0


def _verdict_passed(v: dict) -> bool:
    """Did this verdict pass? Accept BOTH shapes the repo records.

    The pre-merge reviewer writes ``{"pass": bool, "issues": [...]}``
    (code_tasks._parse_verdict) and the PR reviewer writes
    ``{"approve": bool, "issues": [...]}`` (code_tasks._parse_approval) — same
    idea, different key. Reading only `pass` would score every approving PR
    review as a rejection.
    """
    return v.get("pass") is True or v.get("approve") is True


def _combine_verdicts(verdicts) -> Optional[dict]:
    """Fold several review verdicts of ONE attempt into one, or None.

    Passes only if EVERY parseable verdict passed; issues are the union of the
    real lists. Used to merge a task's pre-merge reviewer and its PR reviewers
    into the single ``s_v`` the paper's node carries.
    """
    parsed = [v for v in (_as_verdict(x) for x in verdicts) if v is not None]
    if not parsed:
        return None
    issues = []
    for v in parsed:
        if isinstance(v.get("issues"), list):
            issues.extend(v["issues"])
    return {"pass": all(_verdict_passed(v) for v in parsed), "issues": issues}


def attempt_score(exit_code, role: str, verdict, task_status: str,
                  escalations: int = 0) -> float:
    """A scalar quality for one recorded attempt, from fields the repo records.

    The paper combines signals into a discovery score; this is the explicit
    derivation so a native per-attempt score can replace it later. Bounded to
    roughly [0, 3] so β1 (per-attempt cost) stays interpretable.

    ``exit_code`` is None for an attempt that never ran (an unrun task): it must
    NOT earn the "harness ran" bonus, so the completion credit is gated on
    ``exit_code == 0`` exactly.
    """
    s = 0.0
    if exit_code == 0:
        s += 0.5                                # the harness ran to completion
    # A reviewer/pr-reviewer verdict is a dict (or its JSON text): {"pass"|"approve":
    # bool, "issues": [...]}.
    v = _as_verdict(verdict)
    if isinstance(v, dict):
        if _verdict_passed(v):
            s += 1.0
        else:
            s -= min(0.1 * _issue_count(v), 1.0)   # each real objection costs
    # Terminal task outcome dominates: a merged task is the goal.
    if task_status == "merged":
        s += 1.5
    elif task_status == "failed":
        s -= 0.5
    s -= 0.5 * max(0, escalations)              # escalation means the tier was wrong
    return round(s, 4)


# --------------------------------------------------------------------------
# Building a tree from recorded rows (no models, no git).
# --------------------------------------------------------------------------
def _verdict_of(run: dict):
    return run.get("verdict")


def build_tree(name: str, task_rows: list[dict], run_rows: list[dict],
               deps_by_task: Optional[dict[str, list[str]]] = None) -> DiscoveryTree:
    """Assemble a DiscoveryTree from ``code_tasks`` + ``harness_runs`` rows.

    One branch per task; within a task, attempts form a chain rooted at the
    task's first attempt. A task's first attempt hangs off the root, or off
    the last-merged dependency's best node when the task declares deps (its
    worktree is allocated only after that dep merged — Rule 3).

    Rows are plain dicts (store readers return dicts). Missing/zero-run tasks
    still become a single attempt node so a plan with an unrun branch is
    replayable.
    """
    tree = DiscoveryTree(name=name)
    tree.add(Node(id="root", parent=None, task_id="", model="", score=0.0,
                  cost_s=0.0, outcome="base", revealed=True))

    # Attempts grouped by owning task id, RESTRICTED to this tree's tasks. A
    # harness_runs.task_id is the bare task id (verified against orchestrator.db:
    # code_tasks writes save_harness_run(tid, ...) — no "-xN" suffix); a legacy
    # or prompt-path row may carry one, so strip it only when the stripped head
    # is a task this tree knows. Keeping the restriction is what stops another
    # taskfile's runs from leaking into THIS tree: run_rows is the whole store.
    by_task: dict[str, list[dict]] = {}
    known_tasks = {t["id"] for t in task_rows}
    # Two taskfiles can reuse a task id; their rows are sequential, so a run is
    # assigned to the id-window whose [created_at, finished_at] brackets it. An
    # open (finished_at NULL) window is bounded by the next same-id start.
    windows = _closed_windows(task_rows)
    for r in run_rows:
        raw = r.get("task_id", "")
        owner = _task_of(raw)
        if owner not in known_tasks and raw in known_tasks:
            owner = raw
        if owner in known_tasks and \
                _run_in_taskfile_window(_ts(r.get("created_at")),
                                        windows[owner]):
            by_task.setdefault(owner, []).append(r)

    status = {t["id"]: t.get("status") for t in task_rows}
    model = {t["id"]: t.get("model") for t in task_rows}

    # Dependency-aware parent: a task that declares deps hangs off its last
    # dependency's best recorded attempt, else the root. `deps` is not carried
    # on code_tasks rows, so the caller passes `deps_by_task` (from the
    # taskfile) and branch placement follows it; without it every task is a
    # root child (a flat fan-out).
    deps_by_task = deps_by_task or {}
    # Topologically order the tasks so a dependency's node exists before the
    # task that needs to hang off it. The store returns rows newest-first
    # (code_tasks_all: ORDER BY created_at DESC) and a dependency is always
    # created BEFORE its dependent, so without this every dependent is seen
    # first, `parent_for` finds no dep node, and the whole tree flattens to a
    # root fan-out — exactly the structure replay is supposed to recover.
    task_order = _topo_order([t["id"] for t in task_rows], deps_by_task)

    def parent_for(tid: str) -> str:
        deps = deps_by_task.get(tid) or []
        for dep in reversed(deps):                 # last dep, like wire_deps
            dep_nodes = [n.id for n in tree.nodes.values() if n.task_id == dep]
            if dep_nodes:
                return max(dep_nodes, key=lambda nid: (tree.nodes[nid].score,
                                                       tree.nodes[nid].attempt))
        return "root"

    for tid in task_order:
        runs = sorted(by_task.get(tid, []),
                      key=lambda r: (_as_int(r.get("attempt"), 1),
                                     _role_rank(r.get("role"))))
        if not runs:
            # An unrun task never invoked a harness, so it must NOT earn the
            # "harness ran to completion" credit: exit_code=None, not 0.
            tree.add(Node(id=f"{tid}-x1", parent=parent_for(tid),
                          task_id=tid, model=model.get(tid, ""),
                          score=attempt_score(None, "implementer", None,
                                              status.get(tid, "")),
                          cost_s=0.0, attempt=1, outcome="unrun"))
            continue
        prev = parent_for(tid)
        # The paper's node is one generation–evaluation ATTEMPT: the artifact
        # plus its evaluation. Here that is every harness run sharing one
        # (task, attempt) — the implementer run, its pre-merge reviewer run,
        # and any PR-reviewer runs — so they merge into ONE node rather than
        # colliding on one node id.
        by_attempt: dict[int, list[dict]] = {}
        for r in runs:
            by_attempt.setdefault(_as_int(r.get("attempt"), 1), []).append(r)
        for attempt in sorted(by_attempt):
            group = by_attempt[attempt]
            nid = f"{tid}-x{attempt}"
            tree.add(Node(
                id=nid, parent=prev, task_id=tid,
                model=group[0].get("model") or model.get(tid, ""),
                score=_attempt_group_score(group, status.get(tid, "")),
                cost_s=round(sum(_as_float(r.get("seconds")) for r in group), 2),
                attempt=attempt, role="attempt",
                outcome=_group_outcome(group, status.get(tid, "")),
            ))
            prev = nid                        # rework continues the previous attempt
    return tree


def _topo_order(tids: list[str], deps_by_task: dict[str, list[str]]) -> list[str]:
    """Order tasks so every dependency precedes its dependents (Kahn).

    Declaration order is the tiebreak, so a taskfile with no deps keeps its
    written order. Tasks are deduped (a duplicate id would otherwise be placed
    twice); a cycle (which the loader rejects, but a stale on-disk file could
    contain) does not spin: any node still blocked is appended in declaration
    order at the end.
    """
    tids = list(dict.fromkeys(tids))
    remaining = list(tids)
    id_set = set(tids)
    placed: set[str] = set()
    out: list[str] = []
    while remaining:
        progressed = False
        still = []
        for tid in remaining:
            deps = [d for d in (deps_by_task.get(tid) or []) if d in id_set]
            if all(d in placed for d in deps):
                out.append(tid)
                placed.add(tid)
                progressed = True
            else:
                still.append(tid)
        remaining = still
        if not progressed:                     # cycle: emit the rest as written
            out.extend(remaining)
            break
    return out


def _role_rank(role) -> int:
    """Order harness runs of one attempt: implementer, then its reviewers."""
    return 0 if (role or "") == "implementer" else 1


def _is_review_role(role) -> bool:
    return (role or "") in ("reviewer", "pr-reviewer")


def _attempt_group_score(group: list[dict], status: str) -> float:
    """The combined s_v for one attempt = its implementer run scored with the
    verdicts of its reviewer AND PR reviewers folded in (the paper's node has
    one score)."""
    impl = next((r for r in group if (r.get("role") or "") == "implementer"),
                group[0])
    review_verdicts = [r.get("verdict") for r in group if _is_review_role(r.get("role"))]
    merged = _combine_verdicts(review_verdicts) if review_verdicts else None
    return attempt_score(_as_int(impl.get("exit_code"), None), "implementer",
                         merged, status)


def _group_outcome(group: list[dict], status: str) -> str:
    """The attempt's outcome, from its merged review verdict when it has one."""
    review_verdicts = [r.get("verdict") for r in group if _is_review_role(r.get("role"))]
    merged = _combine_verdicts(review_verdicts) if review_verdicts else None
    if merged is not None:
        return "review_pass" if _verdict_passed(merged) else "review_reject"
    return _outcome(group[0], status)


def _task_of(task_id: str) -> str:
    """Strip the attempt suffix from a harness_runs task_id.

    code_tasks names a harness run's task_id ``f"{tid}-x{attempt}"`` (Rule 7),
    so the owning task is everything before the LAST ``-x<digits>``. A bare
    ``<tid>`` is already the owning task.
    """
    if "-x" in task_id:
        head, _, tail = task_id.rpartition("-x")
        if tail.isdigit():
            return head
    return task_id


def _outcome(run: dict, status: str) -> str:
    role = run.get("role") or ""
    if _is_review_role(role):
        v = _as_verdict(run.get("verdict"))
        if v is not None:
            return "review_pass" if _verdict_passed(v) else "review_reject"
    if status == "merged":
        return "merged"
    if _as_int(run.get("exit_code"), 0) != 0:
        return "gate_fail"
    return role or "run"


# --------------------------------------------------------------------------
# The exploration policy interface (paper §3, "shared decision interface").
# --------------------------------------------------------------------------
class ExplorationPolicy:
    """Chooses a batch of eligible nodes to advance, and when to stop.

    ``choose`` sees ONLY the revealed prefix (paper: "prefix-observable"): the
    list of currently-eligible node ids and the tree as revealed so far. It
    returns a batch ``C`` with ``|C| <= W``; an empty batch stops the rollout.
    """
    name = "policy"

    def reset(self) -> None:            # per-rollout state (paper resets it)
        pass

    def choose(self, tree: DiscoveryTree, eligible: list[str], W: int) -> list[str]:
        raise NotImplementedError


class RecursiveFixed(ExplorationPolicy):
    """The paper's controlled baseline: open branches, depth-first, always W.

    Deterministic and policy-free — it never reads scores — so it is the
    incumbent that every other candidate must beat (selection argmax then
    guarantees V* >= V^0 on the fixed history).
    """
    name = "fixed"

    def choose(self, tree, eligible, W):
        # Prefer an unopened root child (open a new branch), then the frontier.
        roots = [n for n in eligible if n == "root" and tree.has_unrevealed_root_child()]
        frontier = [n for n in eligible if n != "root"]
        frontier.sort(key=lambda nid: (tree.nodes[nid].attempt, nid))
        batch = (roots + frontier)[:W]
        return batch


class GreedyBest(ExplorationPolicy):
    """Follow the highest-scoring revealed branch, batching its frontier."""
    name = "greedy-best"

    def choose(self, tree, eligible, W):
        frontier = [n for n in eligible if n != "root"]
        if not frontier:
            return ["root"] if "root" in eligible else []
        frontier.sort(key=lambda nid: (-tree.nodes[nid].score,
                                       tree.nodes[nid].attempt, nid))
        return frontier[:W]


class BreadthBatch(ExplorationPolicy):
    """Open many branches in parallel; spend effort broadly, not deeply."""
    name = "breadth-batch"

    def choose(self, tree, eligible, W):
        frontier = [n for n in eligible if n != "root"]
        if not frontier:
            return ["root"] if "root" in eligible else []
        # Shallowest first (spread across branches), stable by id.
        frontier.sort(key=lambda nid: (tree.nodes[nid].attempt, nid))
        return frontier[:W]


class AdaptiveEffort(ExplorationPolicy):
    """Mirrors the paper §5.2 finding: conserve effort while improving,
    resume it when the frontier's best score plateaus.

    Stateful per rollout: it remembers the best score seen at the last
    decision and widens the batch when progress stalls, narrows it while the
    best keeps rising.
    """
    name = "adaptive-effort"

    def __init__(self):
        self._last_best = None
        self._coast = 0

    def reset(self):
        self._last_best = None
        self._coast = 0

    def choose(self, tree, eligible, W):
        frontier = [n for n in eligible if n != "root"]
        best = tree.best_score([n for n in tree.revealed() if n != "root"])
        improving = self._last_best is not None and best > self._last_best
        self._last_best = best
        if improving:
            self._coast = 0
        else:
            self._coast += 1
        if not frontier:
            return ["root"] if "root" in eligible else []
        frontier.sort(key=lambda nid: (-tree.nodes[nid].score,
                                       tree.nodes[nid].attempt, nid))
        # Plateau -> widen (explore); improving -> narrow (exploit the winner).
        width = W if self._coast >= 1 else max(1, W // 2)
        return frontier[:width]


BUILTIN_POLICIES: tuple[type[ExplorationPolicy], ...] = (
    RecursiveFixed, GreedyBest, BreadthBatch, AdaptiveEffort,
)


# --------------------------------------------------------------------------
# Replay (paper §3, "Offline evaluation").
# --------------------------------------------------------------------------
@dataclass
class ReplayResult:
    policy: str
    tree: str
    score: float
    quality: float
    n_attempts: int
    rounds: int
    revealed: list[str]


def replay(tree: DiscoveryTree, policy: ExplorationPolicy,
           W: Optional[int] = None, max_rounds: Optional[int] = None) -> ReplayResult:
    """Evaluate one policy on one recorded tree. Zero model calls.

    Revealing a selected node deterministically returns its recorded child(ren)
    (paper: replay "returns recorded children ... deterministically rather than
    generating new candidates"). The rollout ends on an empty batch, the round
    limit, or a full reveal. Costs nothing but CPU.
    """
    W = max(1, _as_int(W if W is not None else workers(), 1))
    max_rounds = max(0, _as_int(
        max_rounds if max_rounds is not None else config.DREAM_MAX_ROUNDS, 0))
    tree.reset()
    policy.reset()
    rounds = 0
    while rounds < max_rounds:
        eligible = tree.eligible()
        batch = _safe_choose(policy, tree, eligible, W, tree.name)
        if not batch:
            break
        rounds += 1
        for nid in batch[:W]:
            _reveal(tree, nid)
        if tree.non_root_revealed() == sum(1 for n in tree.nodes.values()
                                           if n.id != "root"):
            break                              # everything recorded is revealed
    revealed = [n for n in tree.revealed() if n != "root"]
    quality = tree.best_score(revealed)
    n = len(revealed)
    score = quality - beta1() * n + beta2() * (n / max(1, rounds))
    return ReplayResult(policy.name, tree.name, round(score, 4),
                        round(quality, 4), n, rounds, sorted(revealed))


def _safe_choose(policy: ExplorationPolicy, tree: DiscoveryTree,
                 eligible: list[str], W: int, tree_name: str) -> list[str]:
    """Call ``policy.choose`` defensively; a broken policy stops its rollout.

    The policy may be agent-authored (``compile_policy``) or read from a user
    file (``--policy``), so a raise or a non-list return must not take the
    whole replay — and therefore ``code dream`` — down. A failure is treated as
    an empty batch (the paper's stop signal) and logged once, not per tree.
    """
    try:
        chosen = policy.choose(tree, eligible, W)
    except Exception as exc:                    # noqa: BLE001 - agent-authored
        log.warning("policy %s raised in choose() on tree %s: %s "
                    "(treating as stop)", policy.name, tree_name, exc)
        return []
    if not isinstance(chosen, (list, tuple, set)):
        log.warning("policy %s returned %r (not a batch) on tree %s",
                    policy.name, type(chosen).__name__, tree_name)
        return []
    return [b for b in chosen if b in eligible]


def _reveal(tree: DiscoveryTree, node_id: str) -> None:
    """Paper: after a nonempty batch, reveal the selected nodes' next children."""
    if node_id == "root":
        # Opening the root reveals its EARLIEST still-unrevealed child
        # (one previously unseen branch), per §3.
        kids = sorted(c for c in tree.root_children() if not tree.nodes[c].revealed)
        if kids:
            tree.nodes[kids[0]].revealed = True
        return
    kids = [c for c in tree.children_of(node_id) if not tree.nodes[c].revealed]
    for c in kids:
        tree.nodes[c].revealed = True


# --------------------------------------------------------------------------
# Policy improvement + selection (paper §3, final paragraph).
# --------------------------------------------------------------------------
@dataclass
class ImprovementResult:
    selected: str
    mean_by_policy: dict[str, float]
    per_tree: list[dict]


def score_policies(trees: list[DiscoveryTree],
                   policies: list[ExplorationPolicy],
                   W: Optional[int] = None,
                   max_rounds: Optional[int] = None) -> ImprovementResult:
    """Evaluate every policy on every tree; rank by mean replay score.

    Selection is argmax of the MEAN over the fixed history (paper Eq. 2). The
    incumbent ``RecursiveFixed`` is in ``BUILTIN_POLICIES``, so the selected
    policy is never worse than it on the recorded history.
    """
    # Label each policy uniquely. Two policies can share a name (an LLM-authored
    # candidate reusing a built-in's name, or the same policy passed twice), and
    # a name-keyed totals dict would silently merge their scores and corrupt the
    # argmax. A unique name is left as-is (the common case, so ``mean_by_policy``
    # stays keyed by name); only a genuine collision gets a ``#i`` suffix.
    counts: dict[str, int] = {}
    for p in policies:
        counts[p.name] = counts.get(p.name, 0) + 1
    seen: dict[str, int] = {}
    labels: list[str] = []
    for p in policies:
        if counts[p.name] == 1:
            labels.append(p.name)
        else:
            k = seen.get(p.name, 0)
            seen[p.name] = k + 1
            labels.append(f"{p.name}#{k}")
    totals: dict[str, float] = {lab: 0.0 for lab in labels}
    n_trees = max(1, len(trees))
    per_tree = []
    for t in trees:
        row = {"tree": t.name}
        for lab, p in zip(labels, policies):
            r = replay(t, p, W=W, max_rounds=max_rounds)
            totals[lab] += r.score
            row[lab] = r.score
        per_tree.append(row)
    means = {k: round(v / n_trees, 4) for k, v in totals.items()}
    selected = max(means, key=lambda k: means[k]) if means else ""
    return ImprovementResult(selected, means, per_tree)


def load_trees(store, taskfile: Optional[str] = None,
               limit: int = 200) -> list[DiscoveryTree]:
    """Build replay worlds from the durable store. No models, no git.

    With ``taskfile`` set, only that file's run is replayed (one tree). Without
    it, each taskfile seen in recent history becomes a tree, giving the
    improvement loop a *pool* of worlds to average over (paper: "evaluated
    separately on every historical tree").
    """
    rows = store.code_tasks_all(limit=limit)
    run_rows = store.harness_runs_all(limit=max(limit * 4, 1500))
    by_file: dict[str, list[dict]] = {}
    for r in rows:
        by_file.setdefault(r.get("taskfile") or "(unknown)", []).append(r)

    trees = []
    for tf, trows in by_file.items():
        if taskfile and Path(tf).name != Path(taskfile).name:
            continue
        deps_by_task = _deps_from_taskfile(tf)
        trees.append(build_tree(Path(tf).name, trows, run_rows,
                                deps_by_task=deps_by_task))
    return trees


def _deps_from_taskfile(taskfile: str) -> dict[str, list[str]]:
    """Read a taskfile's `deps` so recorded branches place correctly.

    Best-effort: a taskfile that has been edited or deleted since the run
    simply yields a flat tree (every branch a root child), which is still
    replayable. The plan graph is part of the replay world (paper: the
    recorded decision), so we prefer it when it is still on disk.
    """
    try:
        data = json.loads(Path(taskfile).read_text(encoding="utf-8"))
        return {t["id"]: list(t.get("deps") or [])
                for t in data["project"]["tasks"] if isinstance(t, dict) and t.get("id")}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


# --------------------------------------------------------------------------
# The dreaming loop with a policy-development agent (paper §3, §4).
# --------------------------------------------------------------------------
def propose_source(agent: Callable[[dict], Optional[str]], context: dict) -> Optional[str]:
    """Ask a policy-development agent to author a NEW candidate policy's source.

    ``agent(context) -> source or None``. The paper's development agent reads
    replay trajectories + scores and rewrites the exploration-policy CODE; this
    is that hook. The result is NEVER executed directly: the caller compiles it
    into an ``ExplorationPolicy`` and it competes in ``score_policies`` like any
    other candidate, so an agent can only change behaviour by scoring best on
    the recorded history (paper: "ONLY the exploration-policy code changes").

    A failing/None agent is not an error — the built-in candidates stand.
    """
    try:
        return agent(context)
    except Exception as exc:                    # noqa: BLE001 - agent is external
        log.warning("policy-development agent failed: %s", exc)
        return None


# Only these builtins are exposed to agent-authored policy source. CPython
# injects the REAL `__builtins__` module by default, so `import os` (and thus
# subprocess, file I/O) worked despite the name — this dict is the actual
# sandbox. A policy needs no more than these to sort/iterate/compare nodes.
_SAFE_BUILTINS = {
    "__build_class__": __build_class__, "Exception": Exception,
    "TypeError": TypeError, "ValueError": ValueError,
    "isinstance": isinstance, "len": len, "min": min, "max": max,
    "sorted": sorted, "list": list, "set": set, "tuple": tuple, "sum": sum,
    "any": any, "all": all, "abs": abs, "round": round, "enumerate": enumerate,
    "range": range, "int": int, "float": float, "str": str, "bool": bool,
    "dict": dict, "zip": zip, "reversed": reversed,
}
# Names an agent-authored policy may not reference. The restricted builtins
# dict alone is NOT a sandbox: CPython objects leak the real interpreter
# through dunder attributes (``object.__subclasses__()[i].__init__.__globals__``
# reaches the real ``__builtins__``, hence ``open``/``__import__``). The AST
# check below blocks that, and these names are refused as a second layer.
_POLICY_FORBIDDEN_NAMES = frozenset({
    "__import__", "eval", "exec", "compile", "open", "input", "breakpoint",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "type", "object", "super", "memoryview", "help", "exit", "quit",
})


# Attribute names that walk into an object's internals without a dunder
# attribute node: ``'{0.choose.__globals__}'.format(obj)`` names the dunder
# inside a string CONSTANT, so the AST never sees an ``ast.Attribute`` — yet
# ``str.format`` resolves it at runtime and reads the real module globals
# (verified: it exfiltrated config.API_KEY). These accessors are refused.
_POLICY_FORBIDDEN_ATTRS = frozenset({"format", "format_map", "mro",
                                     "gi_frame", "f_back", "f_builtins",
                                     "f_globals", "f_locals"})


def _policy_source_is_safe(source: str) -> Optional[str]:
    """None if ``source`` is safe to compile, else the reason it is not.

    A denylist of builtins is insufficient on its own — every reachable object
    exposes dunder attributes that climb back to the real interpreter — so the
    gate is structural: no imports, no dunder attribute access anywhere (this
    is what closes ``object.__subclasses__`` and ``().__class__``), no
    ``_POLICY_FORBIDDEN_ATTRS`` accessor (whose FIELD NAMES name dunders the AST
    cannot see), no format-string field names containing an attribute walk, and
    none of ``_POLICY_FORBIDDEN_NAMES`` referenced by name.
    """
    try:
        mod = ast.parse(source, "<dream-rsi-policy>")
    except SyntaxError as exc:
        return f"syntax error: {exc}"
    for node in ast.walk(mod):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "imports are not allowed"
        if isinstance(node, ast.Attribute) and \
                node.attr.startswith("__") and node.attr.endswith("__"):
            return f"dunder attribute access is not allowed: .{node.attr}"
        if isinstance(node, ast.Attribute) and \
                node.attr in _POLICY_FORBIDDEN_ATTRS:
            return f"attribute access is not allowed: .{node.attr}"
        if isinstance(node, ast.Call) and \
                isinstance(node.func, ast.Attribute) and \
                node.func.attr in ("format", "format_map"):
            return f"format-string access is not allowed: .{node.func.attr}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and \
                _format_string_walks_attributes(node.value):
            return "format-string field names may not walk attributes"
        if isinstance(node, ast.Name) and node.id.startswith("__") and \
                node.id.endswith("__"):
            return f"dunder name is not allowed: {node.id}"
        if isinstance(node, ast.Name) and node.id in _POLICY_FORBIDDEN_NAMES:
            return f"name is not allowed: {node.id}"
    return None


def _format_string_walks_attributes(s: str) -> bool:
    """True if a format string has a ``{...}`` field that walks attributes.

    ``'{0.choose.x}'`` and ``'{a.__class__}'`` resolve attributes at runtime;
    plain ``'{}'``/``'{0}'`` do not. Conservative: any ``.`` inside a field
    (outside a ``:`` format spec) counts.
    """
    if "{" not in s:
        return False
    depth = 0
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "." and depth:
            return True
    return False


def compile_policy(source: str) -> Optional[ExplorationPolicy]:
    """Compile agent-authored policy source into an ExplorationPolicy.

    The source must define ``class Policy(ExplorationPolicy)`` with ``name`` and
    ``choose``. It is validated structurally (``_policy_source_is_safe``: no
    imports, no dunder attribute access) and executed under a restricted
    ``__builtins__``. Returns None on any failure so a bad proposal is dropped,
    never fatal.
    """
    reason = _policy_source_is_safe(source)
    if reason is not None:
        log.warning("policy source rejected: %s", reason)
        return None
    ns = {"ExplorationPolicy": ExplorationPolicy,
          "__builtins__": _SAFE_BUILTINS, "__name__": "dream_rsi_policy"}
    try:
        exec(compile(source, "<dream-rsi-policy>", "exec"), ns)  # noqa: S102
        cls = ns.get("Policy")
        if not isinstance(cls, type) or not issubclass(cls, ExplorationPolicy):
            return None
        return cls()
    except Exception as exc:                    # noqa: BLE001 - agent-authored
        log.warning("policy source did not compile: %s", exc)
        return None


def improve(trees: list[DiscoveryTree],
            extra_policies: Optional[list[ExplorationPolicy]] = None,
            W: Optional[int] = None,
            max_rounds: Optional[int] = None) -> ImprovementResult:
    """The full offline improvement step: evaluate the incumbent + candidates,
    return the argmax policy name. Monotone non-worsening on the fixed history.
    """
    policies = [p() for p in BUILTIN_POLICIES]
    policies.extend(extra_policies or [])
    return score_policies(trees, policies, W=W, max_rounds=max_rounds)


def context_for_agent(trees, result: ImprovementResult) -> dict:
    """The replay evidence a policy-development agent reasons over (paper §3)."""
    return {
        "history_size": len(trees),
        "policies": result.mean_by_policy,
        "selected": result.selected,
        "per_tree": result.per_tree,
        "objective": {"beta1": beta1(), "beta2": beta2()},
        "instructions": (
            "Author a NEW exploration policy as Python defining "
            "`class Policy(ExplorationPolicy)` with a `name` and "
            "`choose(self, tree, eligible, W) -> list[node_id]` returning at "
            "most W eligible node ids (empty list stops early). Read only the "
            "prefix-observable evidence: tree.nodes[nid].score/attempt. Do not "
            "import anything."),
    }


def save_run(result: ImprovementResult, path: Optional[str] = None) -> str:
    """Append one replay run to logs/replay/<stamp>.jsonl (Rule 7 evidence)."""
    import config as _c
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(path or (Path(_c.ROOT) / "logs" / "replay" / f"{stamp}.jsonl"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": round(time.time(), 3),
            "selected": result.selected,
            "means": result.mean_by_policy,
            "per_tree": result.per_tree,
        }) + "\n")
    return str(out)
