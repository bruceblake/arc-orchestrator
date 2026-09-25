"""Dashboard routes for the human checkpoint queue ("Needs you").

  GET  /api/reviews          every PR waiting for a human, with its evidence
                             (screenshots, before/after, flythrough and
                             playtest videos), the fleet reviewers' verdicts,
                             and whether its build can be played here
  POST /api/reviews/decide   {task, pr, round, decision: approve|reject,
                             comment}: record a human's decision

Kept out of dashboard.py (which many agents edit at once); the server hands
these two routes over with one lookup each.

Rule 6b. The POST goes through the dashboard's _refuse_post guard like every
other action. It acts only on a hold that manual_review already has in
`waiting`, named by (task, pr, round) — values the server listed, compared
exactly. The comment is stored text that becomes the implementer's feedback;
it never reaches a shell, a path or a git ref. Playing a build uses the
existing /api/studio/playtest/launch route and its allowlists.
"""
from __future__ import annotations

import time
from pathlib import Path

import config
import manual_review


def _attempt_n(man):
    try:
        return int(str(man.get("attempt", "x0"))[1:])
    except ValueError:
        return 0


def _evidence(task, project):
    """The newest evidence manifest of this task, with every file as a URL."""
    import dashboard
    try:
        mans = dashboard._timeline_evidence(task)
    except Exception:                                   # noqa: BLE001
        return None
    mine = [m for m in mans if m.get("project") == project] or mans
    if not mine:
        return None
    man = max(mine, key=_attempt_n)
    root = dashboard._evidence_root()

    def url(p):
        return dashboard._evidence_url(root, p) if p else None

    videos = {}
    for name, v in (man.get("videos") or {}).items():
        if isinstance(v, dict):
            videos[name] = {"mp4": url(v.get("mp4")), "gif": url(v.get("gif"))}
    compare = [{"name": c.get("name"), "changed": c.get("changed"),
                "url": url(c.get("side_by_side"))}
               for c in man.get("compare") or [] if isinstance(c, dict)]
    sheet = Path(man.get("manifest") or "").with_name("contact_sheet.png")
    return {"attempt": man.get("attempt"), "ts": man.get("ts"),
            "contact_sheet": url(sheet) if sheet.is_file() else None,
            "shots": man.get("shots") or [], "videos": videos,
            "compare": [c for c in compare if c["url"]],
            "warnings": man.get("warnings") or [],
            "godot_errors": (man.get("godot_errors") or [])[:10]}


def _play(project, task):
    """Can this task's build (and main) be played on this machine?"""
    out = {"project": project, "build": f"task/{task}", "available": False,
           "reason": "", "scenes": [], "main_scene": "", "base": None}
    try:
        from studio import playtest
        from studio import status as studio_status
        from studio.engine import godot
    except Exception as exc:                            # noqa: BLE001
        out["reason"] = f"studio unavailable: {exc}"
        return out
    try:
        if project not in studio_status.projects():
            out["reason"] = "not a studio game project"
            return out
        builds = playtest.builds(project)
    except Exception as exc:                            # noqa: BLE001
        out["reason"] = f"builds unavailable: {exc}"[:300]
        return out
    base = next((b for b in builds if not b["id"].startswith("task/")), None)
    if base:
        out["base"] = {"build": base["id"], "sha": base["sha"]}
    mine = next((b for b in builds if b["id"] == out["build"]), None)
    if mine is None:
        out["reason"] = "no task branch for this PR in the game repo"
        return out
    out.update(sha=mine["sha"], scenes=mine.get("scenes") or [],
               main_scene=mine.get("main_scene") or "")
    if not godot.godot_bin():
        out["reason"] = "Godot is not installed on this machine"
    elif not config.STUDIO_DISPLAY:
        out["reason"] = "no display to play on (ARC_STUDIO_DISPLAY)"
    else:
        out["available"] = True
    return out


def queue():
    """Everything the Needs-you panel and the phone's Review view show."""
    waiting = []
    for r in manual_review.waiting():
        item = dict(r)
        item["waited_s"] = round(time.time() - (r.get("requested_at") or time.time()))
        item["evidence"] = _evidence(r["task"], r["project"])
        item["play"] = _play(r["project"], r["task"])
        waiting.append(item)
    return {"waiting": waiting, "recent": manual_review.recent(),
            "global_default": config.PR_MANUAL_REVIEW,
            "display": config.STUDIO_DISPLAY or ""}


def decide(body):
    """(response, status) for POST /api/reviews/decide."""
    body = body if isinstance(body, dict) else {}
    task, pr, rnd = body.get("task"), body.get("pr"), body.get("round")
    decision, comment = body.get("decision"), body.get("comment", "")
    if decision not in manual_review.DECISIONS:
        return {"error": "decision must be approve or reject"}, 400
    if not isinstance(comment, str) or len(comment) > manual_review.MAX_COMMENT:
        return {"error": f"comment must be text up to {manual_review.MAX_COMMENT} chars"}, 400
    if decision == "reject" and not comment.strip():
        return {"error": "say what must change: the comment is the implementer's feedback"}, 400
    # Exact match against the server's own list of waiting holds.
    held = next((r for r in manual_review.waiting()
                 if r["task"] == task and r["pr"] == pr and r["round"] == rnd), None)
    if held is None:
        return {"error": "no pull request is waiting for a human under that "
                         "task / pr / round"}, 404
    try:
        row = manual_review.decide(held["task"], held["pr"], held["round"], decision,
                                   comment=comment, by="dashboard")
    except KeyError:
        return {"error": "already decided"}, 409
    import events
    events.emit("review.human_decision", task=row["task"], pr=row["pr"],
                round=row["round"], decision=row["status"], live=held.get("live"))
    return {"ok": True, "review": row, "live": held.get("live")}, 200


GET_ROUTES = {"/api/reviews": lambda query: (queue(), 200)}
POST_ROUTES = {"/api/reviews/decide": decide}
