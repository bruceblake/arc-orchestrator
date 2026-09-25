#!/usr/bin/env python3
"""Serve one source tree's dashboard over FIXTURE state, for screenshots.

    ./py tools/visual/serve.py --tree <checkout> --fixture <empty dir> [--port 0]

Prints `PORT <n>` on its first line once it is listening, then serves until
killed. The tree can be ANY checkout of this repo — the task worktree, or the
merge base extracted to a temp dir — so the same script renders "before" and
"after" with the same fixture and the same clock.

Why not `main.py serve`: that reads the operator's live orchestrator.db, event
log, ~/tasks and /proc, and it also arms the daily audit, the captain queue
drain and the studio autopilot, which must never run from a screenshot job.
This builds the handler directly over a fixture world instead:

- every path config derives from the tree (DB, events, logs/*) is rewritten
  under the fixture dir, HOME points into it (so ~/tasks, ~/repos and friends
  are the fixture's), and gh tokens are dropped from the environment;
- `/proc`-derived "live runs" are reported empty — a real fleet run on this
  machine must not show up in a golden image;
- time.time() starts at fixture.FROZEN_NOW, so server-side "N minutes ago"
  renders the same on every capture (the browser's clock is pinned to the same
  instant by capture.py).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _isolate_env(fx):
    os.environ.update({
        "HOME": fx["home"],
        "TZ": "UTC",
        "ARC_FLEET": "local",
        "ARC_DB_PATH": fx["db"],
        "ARC_EVENTS_LOG": fx["events"],
        "ARC_TASKS_DIR": fx["tasks"],
        "ARC_WORKTREE_ROOT": fx["worktrees"],
        "ARC_REPO_ROOT": fx["home"],
        "ARC_REPOS_DIR": fx["repos"],
        "ARC_DASHBOARD_BIND": "127.0.0.1",
    })
    for k in ("GH_TOKEN", "GITHUB_TOKEN", "ARC_DASHBOARD_TOKEN",
              "ARC_ESCALATION_PATH", "ARC_ALLOW_SAME_FAMILY_REVIEW"):
        os.environ.pop(k, None)
    try:
        time.tzset()
    except AttributeError:
        pass


def _freeze_clock(now):
    """time.time() == `now`, always.

    Stopped, not merely started there: the header's "updated HH:MM:SS" comes
    from the server's clock, and a clock that ticked during the capture moved
    that label by a second between two runs of identical code. Nothing the
    dashboard's GET handlers do waits on a wall-clock deadline (no
    `while time.time() < ...` loop in dashboard.py, store.py, reconcile.py,
    agentboard.py or captain.py); sleeps and socket timeouts use the monotonic
    clock, which is left alone."""
    time.time = lambda: now


def _redirect_config(config, tree, root):
    """Point every path config derived from the tree at the fixture root."""
    tree_s = str(Path(tree).resolve())
    root_s = str(root)
    for name in dir(config):
        if not name.isupper() or name == "ROOT":
            continue
        val = getattr(config, name)
        if isinstance(val, (str, Path)):
            s = str(val)
            if s == tree_s or s.startswith(tree_s + os.sep):
                new = root_s + s[len(tree_s):]
                setattr(config, name, Path(new) if isinstance(val, Path) else new)
    config.ROOT = Path(root_s)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tree", required=True, help="checkout whose dashboard to serve")
    ap.add_argument("--fixture", required=True, help="directory for the fixture world")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port")
    a = ap.parse_args(argv)
    tree = Path(a.tree).resolve()
    fxdir = Path(a.fixture).resolve()
    fxdir.mkdir(parents=True, exist_ok=True)
    # The tree's own modules, never this tool's checkout: "before" must render
    # with the merge base's dashboard.py, not today's.
    sys.path.insert(0, str(tree))
    sys.path.insert(1, str(HERE))
    os.chdir(tree)
    import fixture
    root = fxdir / "root"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    link = root / "static"
    if not link.exists():
        link.symlink_to(tree / "static", target_is_directory=True)
    fx = {"db": str(root / "orchestrator.db"), "events": str(root / "logs" / "events.jsonl"),
          "tasks": str(fxdir / "tasks"), "home": str(fxdir / "home"),
          "repos": str(fxdir / "repos"), "worktrees": str(fxdir / "worktrees")}
    _isolate_env(fx)
    _freeze_clock(fixture.FROZEN_NOW)
    import config
    _redirect_config(config, tree, root)
    config.DB_PATH, config.EVENTS_LOG = fx["db"], fx["events"]
    config.TASKS_DIR, config.WORKTREE_ROOT = fx["tasks"], fx["worktrees"]
    config.REPO_ROOT = fx["home"]
    built = fixture.build(fxdir)
    # fixture.build writes its db/events under fxdir; serve those exact files.
    os.replace(built["db"], fx["db"])
    os.replace(built["events"], fx["events"])
    import reconcile
    reconcile.live_runs = lambda: []
    import dashboard
    from http.server import ThreadingHTTPServer
    from store import Store
    dashboard.Handler.store = Store(fx["db"])
    dashboard.Handler.log_message = lambda *a, **k: None
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), dashboard.Handler)
    print(f"PORT {httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
