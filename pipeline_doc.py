"""What each pipeline node does, in prose, joined to what it actually did.

Two halves, deliberately kept apart:

* The PROSE below is written for a reader who wants to understand the system.
  It is the only hand-maintained part, and it says what a node is for and what
  goes wrong with it — things no amount of introspection can recover.
* Everything else is DERIVED at call time: the node's real outgoing edges and
  their conditions from the graph the fleet builds, its retry policy and
  timeout from the node object, and how it has actually behaved from the event
  log. A diagram that says a node retries three times while the code says ten
  is worse than no diagram, so the numbers are never typed here.
"""
import json
import re
import time
from pathlib import Path

import config

# Plain-English readings of the `when=` predicates. Keyed by (src, dst) on the
# suffix-stripped node names, matching what _build_graph_topologies produces.
EDGE_MEANING = {
    ("alloc", "implement"): "always — the worktree is ready",
    ("implement", "gate"): "always — something was written, now prove it",
    ("gate", "review"): "the verify command passed",
    ("gate", "implement"): "the gate failed and the fix budget is not spent",
    ("gate", "escalate"): "the gate failed and the budget IS spent, but a stronger model exists",
    ("gate", "fail"): "the gate failed, no budget and no stronger model left",
    ("review", "publish"): "the reviewer approved",
    ("review", "review"): "the reviewer CRASHED — retry the review, not the code",
    ("review", "implement"): "the reviewer found real problems",
    ("review", "escalate"): "still rejected and the budget is spent",
    ("review", "fail"): "still rejected, nothing left to try",
    ("escalate", "implement"): "always — start again on the stronger model",
    ("publish", "pr_fanout"): "the branch pushed and a PR is open",
    ("publish", "implement"): "the branch conflicts with the base and needs a human-shaped fix",
    ("publish", "alloc"): "no worktree to resume — start the task over",
    ("pr_fanout", "pr_reviewer"): "one child per chosen reviewer (dynamic fan-out)",
    ("pr_fanout", "pr_review"): "there was no PR to review",
    ("pr_reviewer", "pr_review"): "every reviewer has finished (the join)",
    ("pr_review", "pr_merge"): "every reviewer approved",
    ("pr_review", "pr_fanout"): "nobody objected but a reviewer crashed — re-review",
    ("pr_review", "implement"): "a reviewer asked for changes",
    ("pr_review", "fail"): "still rejected after the last round",
    ("pr_merge", "pr_fanout"): "the base moved, the branch was resynced — review the new diff",
}

NODES = {
    "alloc": {
        "title": "alloc — cut a worktree and a branch",
        "what": "Creates ~/worktrees/<repo>/<task-id> and a branch task/<task-id>, "
                "both named after the task id. That is why the id is the KEY: two "
                "taskfiles sharing one would share a directory and a branch.",
        "why": "Every task edits a private checkout. Agents never touch your working "
               "copy, and two tasks editing the same file cannot see each other's "
               "half-written state.",
        "watch": "alloc RESETS the branch to base. It emits task.branch_reset when "
                 "that discards commits, because doing it silently once cost four "
                 "reviewed branches.",
    },
    "implement": {
        "title": "implement — an agent writes the code",
        "what": "Runs the task's model in the worktree with the task prompt, plus "
                "whatever feedback sent it back here: a failed gate, a rejecting "
                "reviewer, PR comments, or conflict markers to resolve.",
        "why": "This is the only node that writes code. Everything before it "
               "prepares, everything after it judges.",
        "watch": "It is reached by SEVEN different edges. If the feedback for one of "
                 "them is missing the agent re-reads finished work, concludes there "
                 "is nothing to do, and the task dies as 'no changes to publish' — "
                 "which is exactly what happened when PR review issues were not "
                 "passed through.",
    },
    "gate": {
        "title": "gate — run the task's own verify command",
        "what": "Executes the taskfile's verify_cmd in the worktree. Deterministic, "
                "no model involved. Its full output is kept in logs/gates/.",
        "why": "A cheap, honest check before a reviewer is spent. It catches the "
               "mechanical failures — nothing compiles, tests fail — that a model "
               "should never be asked to notice.",
        "watch": "A gate built only from `grep -q '<string>' <file>` where the string "
                 "is ALREADY in the file cannot fail, so the implementer correctly "
                 "writes nothing. The daily audit reports these.",
    },
    "review": {
        "title": "review — a second model reads the whole diff",
        "what": "A cross-family model reads the complete diff against the spec and "
                "answers with JSON: pass, or a list of issues.",
        "why": "The gate proves the code runs; this asks whether it does what was "
               "asked. It rejects 39% of implementations that passed their gate, "
               "which is why it is not redundant with the PR review that follows.",
        "watch": "A reviewer that CRASHED did not review. Counting that as a rejection "
                 "sent implementers to fix nothing and burned a fix round each time; "
                 "it now retries the review instead.",
    },
    "escalate": {
        "title": "escalate — move up a tier",
        "what": "Switches the task to the next stronger model on the escalation path "
                "and refreshes its fix budget.",
        "why": "A task can fail because the work is hard, not because the agent is "
               "broken. Rather than failing at the entry tier, it is retried by a "
               "model with more capability.",
        "watch": "Only a CAPABILITY failure escalates. A killed run or a merge "
                 "conflict resumes at the same tier — escalating on infrastructure "
                 "noise wastes the scarcest models.",
    },
    "publish": {
        "title": "publish — commit, sync with base, push, open the PR",
        "what": "Commits the agent's work with trailers naming the model and "
                "reviewer, merges the CURRENT base into the branch, pushes, and "
                "opens a pull request. Merges nothing locally.",
        "why": "The pull request is the gate. Reviewers read that diff and their "
               "approval is what merges it.",
        "watch": "The sync is why conflicts are rare: a task branches from base and "
                 "opens its PR a median of 101 minutes later, and without syncing "
                 "here a task that never overlapped anyone still conflicted.",
    },
    "pr_fanout": {
        "title": "pr_fanout — choose the reviewers and fan out",
        "what": "Picks the least-contended cross-family models that may review, then "
                "spawns one graph node per reviewer. Width is decided here, at "
                "runtime, from who is eligible and free.",
        "why": "Reviewers used to run inside a single node. That made them invisible "
               "to the graph: not in this diagram, not checkpointed, not "
               "individually retryable.",
        "watch": "Contention is whichever ceiling binds first — the model's own cap "
                 "or the harness pool it shares with the other models.",
    },
    "pr_reviewer": {
        "title": "pr_reviewer — ONE reviewer reads the real PR diff",
        "what": "Fetches the diff with `gh pr diff` and answers approve or "
                "changes-requested with reasons. One of these runs per reviewer, in "
                "parallel, each a real graph node with its own retry and timeout.",
        "why": "Two independent readings of the same diff, by models from different "
               "families, so an approval means two genuinely separate judgements.",
        "watch": "A crash here is retried at this node. The join only ever sees "
                 "crashes that survived the retries, so a flaky harness cannot look "
                 "like a rejection.",
    },
    "pr_review": {
        "title": "pr_review — the join: tally the verdicts and decide",
        "what": "Waits for EVERY reviewer, posts each verdict to GitHub as a real "
                "review, and decides: merge, send back, or re-review.",
        "why": "Unanimity is required. One dissent sends the task back to the "
               "implementer with the issues attached.",
        "watch": "Three outcomes, not two. If nobody objected but a reviewer never "
                 "ran, the round is INCONCLUSIVE — nothing is posted as "
                 "changes-requested and the review is retried, because a crashed "
                 "reviewer has not judged anything.",
    },
    "pr_merge": {
        "title": "pr_merge — merge the pull request",
        "what": "Squash-merges via `gh pr merge`, fast-forwards the local base, and "
                "removes the worktree. Reached only after unanimous approval.",
        "why": "The only node that lands code. Everything upstream is a gate in "
               "front of it.",
        "watch": "If GitHub reports a conflict it resyncs the branch and sends it "
                 "back for review rather than giving up — the diff changed, so the "
                 "approval it already has no longer covers it.",
    },
    "fail": {
        "title": "fail — stop, and say why",
        "what": "Marks the task failed with the actual cause, and emits the attempt "
                "and escalation counts alongside it.",
        "why": "A terminal state that is honest is the difference between a "
               "diagnosis and a guess.",
        "watch": "It used to report 'exhausted escalation' no matter how it was "
                 "reached, so a task that ran out of PR rounds with ZERO escalations "
                 "was filed as an escalation failure.",
    },
    "join": {
        "title": "join — wait for every dependency",
        "what": "A gather node: fires only once every task this one depends on has "
                "merged.",
        "why": "A dependent must branch from a base that already contains what it "
               "depends on.",
        "watch": "Multi-dependency tasks used to wait on the LAST dependency only — "
                 "a race dressed as a dependency.",
    },
}

OVERVIEW = {
    "title": "How a task moves through the pipeline",
    "paragraphs": [
        "A project is a taskfile: a list of tasks, each with a prompt, a model, a "
        "reviewer and a verify command. Tasks with no dependencies start at once; "
        "a task with dependencies waits for all of them to merge.",
        "Each task gets a private git worktree and a branch named after it, so "
        "agents never touch your working copy and two tasks cannot see each "
        "other's half-written edits.",
        "The happy path is short: write the code, run the verify command, have a "
        "second model read the diff, open a pull request, have two more models "
        "from other families read THAT diff, merge on unanimous approval.",
        "The loops are where the time actually goes. A failed gate, a rejecting "
        "reviewer or PR comments send the task back to the implementer with the "
        "reasons attached. Repeated failure escalates it to a stronger model. A "
        "crashed reviewer retries the review rather than blaming the code. A base "
        "that moved under a long task resyncs and re-reviews.",
        "Nothing merges locally. The pull request is the gate, every verdict is "
        "posted to GitHub, and the only node that lands code is the last one.",
    ],
    "reading": [
        "Solid arrow — always taken.",
        "Dashed arrow — conditional; click a node to read the condition.",
        "Amber arrow — a loop back to an earlier stage.",
        "Blue node — where a task starts. Purple — a join that waits for several.",
    ],
}


def _stats(window_s=7 * 86400):
    """How each node has actually behaved, from the event log."""
    path = Path(config.EVENTS_LOG)
    cut = time.time() - window_s
    ok, err, starts, durs = {}, {}, {}, {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for ln in lines:
        if '"node' not in ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("ts", 0) < cut:
            continue
        n = e.get("node") or ""
        base = re.sub(r"_[^_]+$", "", n) if "_" in n else n
        if not base:
            continue
        t = e.get("type")
        if t == "node_start":
            starts[n] = e["ts"]
        elif t == "node_end":
            ok[base] = ok.get(base, 0) + 1
            if n in starts:
                durs.setdefault(base, []).append(e["ts"] - starts.pop(n))
        elif t == "node_error":
            err[base] = err.get(base, 0) + 1
    out = {}
    for base in set(ok) | set(err):
        d = sorted(durs.get(base, []))
        n_ok, n_err = ok.get(base, 0), err.get(base, 0)
        out[base] = {
            "runs": n_ok, "errors": n_err,
            "reliability": round(100 * n_ok / max(1, n_ok + n_err)),
            "median_s": round(d[len(d) // 2], 1) if d else None,
        }
    return out


def describe(topo):
    """Merge the prose, the real edges and the live stats into one payload."""
    stats = _stats()
    starts = set(topo.get("starts") or [])
    out = []
    for n in topo.get("nodes") or []:
        name = n["name"]
        doc = NODES.get(name, {})
        outgoing = [
            {"to": e["dst"],
             "when": EDGE_MEANING.get((name, e["dst"]),
                                      "conditional" if e["conditional"] else "always"),
             "conditional": e["conditional"],
             "loop": e["dst"] in _EARLIER.get(name, ())}
            for e in topo.get("edges") or [] if e["src"] == name]
        incoming = sorted({e["src"] for e in topo.get("edges") or [] if e["dst"] == name})
        out.append({
            "id": name,
            "title": doc.get("title", name),
            "what": doc.get("what", ""),
            "why": doc.get("why", ""),
            "watch": doc.get("watch", ""),
            "start": name in starts,
            "gather": bool(n.get("gather")),
            "incoming": incoming,
            "outgoing": outgoing,
            "stats": stats.get(name),
        })
    return {"overview": OVERVIEW, "nodes": out}


_ORDER = ["alloc", "implement", "gate", "review", "escalate", "publish",
          "pr_fanout", "pr_reviewer", "pr_review", "pr_merge", "fail"]
_EARLIER = {n: set(_ORDER[:i]) | {n} for i, n in enumerate(_ORDER)}
