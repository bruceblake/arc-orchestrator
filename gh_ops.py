"""GitHub operations agents over the gh CLI: issue triage, issue drafting, PR review.

Three dedicated roles — 'issue-triager', 'issue-maker', 'pr-reviewer' — held
only by a model the roster trusts to PLAN (GLM-5.3 on the two-model fleet
pinned 2026-09-12; DeepSeek-V4.1-Flash-thinking-max implements and reviews but
never plans, so it is refused — drivers.py role validation enforces it). These
are standalone tools, NOT the governed code pipeline: no worktree, no gate, no
publish. Every command previews by default; --apply-labels / --create /
--post are the ONLY paths that write to GitHub. Every gh-touching command
checks `gh auth status` first and exits with 'run: gh auth login' when
unauthenticated; drafting and printing need no gh and always work.
"""

import asyncio
import json
import re
from pathlib import Path

import config
import drivers
from code_tasks import _balanced_span, _parse_verdict

GH_ROLES = ("issue-triager", "issue-maker", "pr-reviewer")
MAX_DIFF = 30000  # chars of PR diff sent to the reviewer model


async def _gh(argv, cwd=None):
    """One bounded gh subprocess call."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *argv, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError("gh CLI not found (install: https://cli.github.com)")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), config.GH_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"gh {' '.join(argv)} timed out after {config.GH_TIMEOUT:.0f}s")
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(argv)} exited {proc.returncode}: "
            + err.decode(errors="replace").strip()[:400])
    return out.decode(errors="replace")


async def _preflight():
    """Auth precheck every gh-touching command starts with."""
    try:
        await _gh(["auth", "status"])
    except RuntimeError as exc:
        msg = str(exc)
        print(msg if "not found" in msg
              else "gh is not authenticated — run: gh auth login")
        raise SystemExit(1)


def _repo_target(repo):
    """Accept owner/name or a local checkout path; return (gh args, cwd)."""
    p = Path(repo).expanduser()
    if p.is_dir():
        return [], str(p.resolve())
    return ["--repo", repo], None


def _driver(model, role):
    """A gh role needs a model the roster trusts to PLAN; the driver enforces it.

    Eligibility is constructed from the ROSTER, never from hardcoded names:
    whichever live model holds the `planner` role may hold the gh roles (GLM-5.3
    today; DeepSeek-V4.1-Flash-thinking-max does not, so it is refused), and
    the harness comes from that model's roster row via drivers.driver_for.
    """
    model = model or config.GH_MODEL or config.PLANNER_MODEL
    if model not in config.MODEL_ROLES:
        raise ValueError(f"gh role {role!r}: {model!r} is not on today's roster "
                         f"({sorted(config.MODEL_ROLES)}); set ARC_GH_MODEL")
    if not config.model_may(model, "planner"):
        raise ValueError(f"gh role {role!r}: {model} may hold "
                         f"{sorted(config.MODEL_ROLES[model])}, and gh roles "
                         f"need planner permission; set ARC_GH_MODEL")
    return drivers.driver_for(model, role)


def _json_from(text, key):
    """Last balanced {...} span that parses to a dict containing `key`."""
    spans = []
    for m in re.finditer(r"\{", text):
        span = _balanced_span(text, m.start())
        if span is not None:
            spans.append(span)
    for span in reversed(spans):
        try:
            obj = json.loads(span)
        except ValueError:
            continue
        if isinstance(obj, dict) and key in obj:
            return obj
    return None


def _reviewer_for(model, i):
    """Cross-review pairing (Rule 2), from the roster.

    A strong model gets the strongest OTHER review-capable family; a model
    below the top tier alternates across every review-capable family that is
    not its own, so neither idles nor saturates."""
    fam = config.MODEL_FAMILY.get(model)
    others = [f for f in config.REVIEW_FAMILIES if f != fam]
    if not others:
        return config.cross_family_reviewer(model)
    if fam in config.REVIEW_FAMILIES:          # a strong model: strongest other
        return others[0]
    return others[i % len(others)]


# --- issue-triager ------------------------------------------------------------

def _triage_prompt(repo, issues_json):
    # Tiers and models come from the roster, never a literal list: one here
    # named Kimi-K3 as a hard-tier implementer and had the two tiers' models
    # the wrong way round after the 2026-09-12 pin.
    hint = {config.TIER_ORDER[0]: "a self-contained feature, one endpoint, "
                                  "mechanical fixes",
            config.TIER_ORDER[-1]: "multi-file reasoning, delicate design"}
    tiers = "\n".join(f"- {tier} -> {' or '.join(models)}: {hint[tier]}"
                      for tier, models in sorted(config.IMPLEMENT_TIERS.items()))
    example = next(iter(config.IMPLEMENT_TIERS.get(config.TIER_ORDER[0], [])), "")
    return (
        "You are triaging GitHub issues for a multi-model coding fleet.\n\n"
        f"REPO: {repo}\nOPEN ISSUES (JSON):\n{issues_json}\n\n"
        "Classify EACH issue: kind (bug|feature|question|docs), size (S|M|L), "
        "and the implementer per these routing tiers (enforced downstream):\n"
        f"{tiers}\n"
        "Reply with STRICT JSON only, no prose:\n"
        '{"issues": [{"number": 1, "title": "short title", "kind": "bug", '
        f'"size": "S", "tier": "{config.TIER_ORDER[0]}", "model": "{example}", '
        '"actionable": true, "summary": "one line: what to do"}]}\n'
        "actionable=false for questions, and for issues too vague to implement."
    )


def _write_taskfile(repo, rows):
    # code_tasks.load_taskfile resolves project.repo against the CWD, so a bare
    # 'owner/name' would not be runnable: pass through the real local path when
    # repo is a directory, resolve the blessed clone ~/repos/<name> when it
    # exists, and otherwise write a clearly-marked placeholder.
    _, cwd = _repo_target(repo)
    if cwd:
        proj_repo = cwd
    else:
        clone = Path.home() / "repos" / repo.rstrip("/").split("/")[-1]
        proj_repo = str(clone) if clone.is_dir() else \
            f"EDIT-ME/local/path/to/{repo.rstrip('/').split('/')[-1]}"
    tasks = []
    for i, r in enumerate(rows):
        if r.get("kind") not in ("bug", "feature") or not r.get("actionable", True):
            continue
        model = r.get("model")
        if model not in config.IMPLEMENTER_MODELS:
            model = config.IMPLEMENT_TIERS.get(
                r.get("tier", config.TIER_ORDER[0]),
                [config.ESCALATION_PATH[0]])[-1]
        n = r.get("number", i)
        view = f"gh issue view {n}" if cwd else \
            f"gh issue view {n} --repo {repo}"
        tasks.append({
            "id": re.sub(r"[^a-z0-9-]+", "-", f"issue-{n}".lower()),
            "title": str(r.get("title", f"issue {n}"))[:80],
            "prompt": (f"Resolve GitHub issue #{n} of {repo}: "
                       f"{r.get('title', '')}\n\n{r.get('summary', '')}\n\n"
                       f"Full detail: `{view}`."),
            "model": model,
            "reviewer": _reviewer_for(model, i),
            "verify_cmd": "",
            "deps": [],
        })
    if not tasks:
        print("\nno actionable issues; no taskfile written")
        return
    slug = re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")[:50]
    Path(config.TASKS_DIR).mkdir(parents=True, exist_ok=True)
    out = Path(config.TASKS_DIR) / f"{slug}-issues.json"
    tf = {"project": {"repo": proj_repo, "title": f"{repo} issue triage",
                      "tasks": tasks}}
    out.write_text(json.dumps(tf, indent=2) + "\n", encoding="utf-8")
    print(f"\ntaskfile: {out}  (project.repo: {proj_repo})")
    if proj_repo.startswith("EDIT-ME"):
        print("NOTE: project.repo is a placeholder — no local checkout of "
              f"{repo} was found; EDIT the taskfile and point it at a local "
              "clone before running.")
    print("NOTE: fill in an honest verify_cmd per task, then dry-run it "
          f"(main.py code run {out} --dry-run) before executing.")


async def triage(repo, model=None, apply_labels=False):
    await _preflight()
    rargs, cwd = _repo_target(repo)
    raw = await _gh(["issue", "list", "--state", "open", "--limit", "100",
                     "--json", "number,title,labels,body,createdAt"] + rargs,
                    cwd=cwd)
    issues = [{"number": i.get("number"), "title": i.get("title"),
               "labels": [lb.get("name") for lb in i.get("labels") or []],
               "body": (i.get("body") or "")[:1000],
               "createdAt": i.get("createdAt")} for i in json.loads(raw)]
    if not issues:
        print("no open issues")
        return 0
    res = await _driver(model, "issue-triager").run(
        _triage_prompt(repo, json.dumps(issues, indent=1)),
        Path(cwd or "."), task_id="gh-triage")
    data = _json_from(res.text, "issues")
    if not data:
        print("triager returned no parseable JSON; raw reply tail:\n"
              + res.text[-2000:])
        return 1
    rows = data["issues"]
    print(f"{'#':>5}  {'kind':<8}  {'sz':<2}  {'tier':<6}  {'model':<17}  title")
    for r in rows:
        print(f"{str(r.get('number', '?')):>5}  {r.get('kind', '?'):<8}  "
              f"{r.get('size', '?'):<2}  {r.get('tier', '?'):<6}  "
              f"{r.get('model', '?'):<17}  {str(r.get('title', ''))[:60]}")
    _write_taskfile(repo, rows)
    if not apply_labels:
        print("\n(preview only — re-run with --apply-labels to write labels)")
        return 0
    for r in rows:
        n = r.get("number")
        labels = [x for x in (r.get("kind"), f"size:{r.get('size')}") if x]
        if not isinstance(n, int) or not labels:
            continue
        try:
            await _gh(["issue", "edit", str(n)]
                      + [a for lb in labels for a in ("--add-label", lb)]
                      + rargs, cwd=cwd)
            print(f"issue #{n}: labels += {', '.join(labels)}")
        except RuntimeError as exc:
            print(f"issue #{n}: labelling failed: {exc}")
    return 0


# --- issue-maker --------------------------------------------------------------

def _issue_prompt(desc):
    return (
        "You turn a rough goal/bug description into a well-structured GitHub "
        "issue.\n\nDESCRIPTION:\n" + desc + "\n\n"
        "Reply with STRICT JSON only, no prose:\n"
        '{"title": "concise imperative title", "body": "markdown body"}\n'
        "The body MUST have '## Context', '## Reproduction' (or '## Expected "
        "behavior' for a feature) and '## Acceptance criteria' (a checklist of "
        "observable outcomes). Be specific; never invent file paths, versions, "
        "or errors you were not given."
    )


async def make_issue(desc, repo, model=None, create=False):
    _, cwd = _repo_target(repo)
    res = await _driver(model, "issue-maker").run(
        _issue_prompt(desc), Path(cwd or "."), task_id="gh-issue")
    data = _json_from(res.text, "title")
    if not data:
        print("issue-maker returned no parseable JSON; raw reply tail:\n"
              + res.text[-2000:])
        return 1
    title, body = str(data["title"]), str(data.get("body", ""))
    print(f"TITLE: {title}\n\n{body}\n")
    if not create:
        print("(preview only — re-run with --create to file it on GitHub)")
        return 0
    await _preflight()
    rargs, cwd = _repo_target(repo)
    out = await _gh(["issue", "create", "--title", title, "--body", body]
                    + rargs, cwd=cwd)
    print(f"filed: {out.strip()}")
    return 0


# --- pr-reviewer --------------------------------------------------------------

def _pr_prompt(repo, n, meta, diff):
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + f"\n... [diff truncated to {MAX_DIFF} chars]"
    return (
        f"You are reviewing pull request #{n} of {repo}. The PR body below is "
        "the spec the author claims to implement.\n\n"
        f"PR METADATA (JSON):\n{meta}\n\nFULL DIFF:\n{diff}\n\n"
        "Review for: spec compliance, correctness, scope discipline (nothing "
        "unrelated), and regressions in code that calls what changed. Reply "
        "with STRICT JSON only, no prose, of the form:\n"
        '{"pass": true}  or  '
        '{"pass": false, "issues": ["specific issue 1", ...]}\n'
        "Pass only if the change fully and correctly implements the spec."
    )


async def pr_review(repo, number, model=None, post=False):
    await _preflight()
    rargs, cwd = _repo_target(repo)
    meta = await _gh(["pr", "view", str(number), "--json",
                      "number,title,body,author,baseRefName,headRefName,files"]
                     + rargs, cwd=cwd)
    diff = await _gh(["pr", "diff", str(number)] + rargs, cwd=cwd)
    res = await _driver(model, "pr-reviewer").run(
        _pr_prompt(repo, number, meta, diff),
        Path(cwd or "."), task_id=f"gh-pr-{number}")
    # Same verdict contract as the code workload's internal reviewer.
    verdict = _parse_verdict(res.text)
    print(f"PR #{number}: {'PASS' if verdict['pass'] else 'FAIL'}")
    for issue in verdict["issues"]:
        print(f"  - {issue}")
    if not post:
        print("\n(preview only — re-run with --post to submit via gh pr review)")
        return 0
    if verdict["pass"]:
        await _gh(["pr", "review", str(number), "--approve",
                   "--body", "gh_ops pr-reviewer: pass."] + rargs, cwd=cwd)
        print("approved via gh pr review")
    else:
        body = ("gh_ops pr-reviewer found blocking issues:\n"
                + "\n".join(f"- {i}" for i in verdict["issues"]))
        await _gh(["pr", "review", str(number), "--comment", "--body", body]
                  + rargs, cwd=cwd)
        print("issues posted as a PR comment")
    return 0
