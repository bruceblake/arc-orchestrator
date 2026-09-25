"""A paste-ready PR review for an agent the operator drives by hand.

The operator can put Gemini (in Antigravity) or Cursor on a pull request as an
extra reviewer, outside the fleet. Those agents start with nothing: no task
spec, no gate, no idea what the fleet's own reviewers already said. This
writes one markdown file that gives them all of it, plus the exact commands
that turn their verdict into the manual gate's decision
(config.PR_MANUAL_REVIEW): a `manual-approved` label merges, a
`manual-rejected` label plus a comment sends it back to the implementer.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import config

INSTRUCTIONS = """You are an independent reviewer on a pull request built by an \
autonomous game-development fleet (Godot 4, a 3D multiplayer prison-escape \
game). Other models already wrote and reviewed this change; you are the \
human-chosen last check. Judge the DIFF against the TASK SPEC below.

Check, in order:
1. Does the diff do what the task spec asks — all of it, and nothing it was \
told not to (e.g. no meshes in a graybox phase)?
2. Would the verify command actually fail if the code were wrong? A gate that \
cannot fail is not a gate.
3. Does it ship tests for what it adds, and do they test behaviour rather than \
merely that files exist?
4. Regressions: what calls the changed code, and does it still work?
5. Game feel and readability: dimensions from scripts/layout.gd, not magic \
numbers; clear node paths and signals.

End with exactly one line:
  VERDICT: APPROVE
or
  VERDICT: REJECT — <the specific changes required>
"""


def _gh(args, cwd):
    p = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True,
                       timeout=config.GH_TIMEOUT, env=config.child_env())
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def _task_for(task_id):
    """The taskfile entry a PR came from, found by its Task-Id trailer."""
    tdir = Path(config.TASKS_DIR)
    for f in sorted(tdir.glob("*.json")) if tdir.is_dir() else []:
        try:
            proj = json.loads(f.read_text(encoding="utf-8")).get("project") or {}
        except (OSError, ValueError):
            continue
        for t in proj.get("tasks") or []:
            if t.get("id") == task_id:
                return f.name, t
    return None, None


def ensure_labels(repo):
    """Create the two manual-gate labels if the repo lacks them."""
    for name, color, desc in (
            (config.PR_MANUAL_APPROVED_LABEL, "2da44e", "Manual review: merge it"),
            (config.PR_MANUAL_REJECTED_LABEL, "cf222e", "Manual review: send it back")):
        subprocess.run(["gh", "label", "create", name, "--color", color,
                        "--description", desc, "--force"], cwd=repo,
                       capture_output=True, text=True, timeout=config.GH_TIMEOUT,
                       env=config.child_env())


def build(repo, number):
    repo = str(Path(repo).expanduser())
    view = json.loads(_gh(["pr", "view", str(number), "--json",
                           "title,url,headRefName,baseRefName,additions,deletions,"
                           "files,comments,commits,state"], repo))
    diff = _gh(["pr", "diff", str(number)], repo)
    task_id = None
    for c in view.get("commits") or []:
        m = re.search(r"Task-Id:\s*(\S+)", (c.get("messageBody") or "") + "\n"
                      + (c.get("messageHeadline") or ""))
        if m:
            task_id = m.group(1)
            break
    if not task_id:
        m = re.match(r"task/(.+)$", view.get("headRefName") or "")
        task_id = m.group(1) if m else None
    tf, task = _task_for(task_id) if task_id else (None, None)
    fleet = [c.get("body", "") for c in view.get("comments") or []
             if (c.get("body") or "").lstrip().startswith("**")]
    lines = [f"# Review: {view.get('title')}", "",
             f"- PR: {view.get('url')} ({view.get('headRefName')} → {view.get('baseRefName')}, "
             f"+{view.get('additions')}/-{view.get('deletions')}, "
             f"{len(view.get('files') or [])} files)",
             f"- Task: `{task_id or 'unknown'}`" + (f" from `{tf}`" if tf else ""), "",
             "## Your instructions", "", INSTRUCTIONS, "",
             "## The task spec (what this change was asked to do)", "",
             (task or {}).get("prompt", "(task spec not found)"), "",
             "## The verify gate it had to pass", "", "```",
             (task or {}).get("verify_cmd", "(unknown)"), "```", "",
             "## What the fleet's reviewers said", ""]
    lines += [f"> {b.strip()[:1500]}".replace("\n", "\n> ") + "\n" for b in fleet] or ["(none yet)"]
    lines += ["", "## The diff", "", "```diff", diff.rstrip(), "```", "",
              "## Recording your decision", "",
              "Approve (the fleet merges it):",
              f"    gh pr edit {number} --add-label {config.PR_MANUAL_APPROVED_LABEL}", "",
              "Reject (your comment becomes the implementer's feedback):",
              f"    gh pr comment {number} --body \"<what must change>\"",
              f"    gh pr edit {number} --add-label {config.PR_MANUAL_REJECTED_LABEL}", "",
              f"(run in {repo}, or use the labels in the GitHub UI)"]
    return "\n".join(lines) + "\n"


def write(repo, number, out=None):
    text = build(repo, number)
    ensure_labels(str(Path(repo).expanduser()))
    out = Path(out) if out else (Path(config.ROOT) / "logs" / "review-packs"
                                 / f"{Path(repo).name}-pr{number}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out
