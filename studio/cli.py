"""`main.py studio ...` — the operator surface for the game workload.

Every command here is either read-only or writes into the studio's own
directory, with three exceptions that say so loudly: `provision --write`
(edits the operator's opencode config, after a backup), `scaffold` (writes a
new game project), and `plan` (writes a taskfile). Nothing here runs git,
merges anything, or starts a fleet run — `main.py code run` does that, because
a studio task is an ordinary task and must go through the ordinary pipeline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import config


def _need_studio():
    if not config.STUDIO:
        print("this command needs the studio fleet: run it with "
              "ARC_FLEET=studio (today's fleet is "
              f"{config.FLEET!r}).", file=sys.stderr)
        return False
    return True


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_doctor(args):
    from studio import provision
    report = provision.doctor(probe=not args.no_probe)
    if args.json:
        _print(report)
        return 0 if report["ready"] else 1
    print(f"fleet:      {report['fleet']}"
          f"{'' if report['studio_profile_active'] else '   (studio NOT active)'}")
    print(f"planner:    {report['planner']}")
    print(f"external:   {', '.join(report['external_models']) or '(none)'}")
    print(f"key:        {'present' if report['openrouter_key'] else 'MISSING'}")
    g, a = report["godot"], report["astra"]
    print(f"godot:      {g['godot_bin'] or 'not installed'}"
          f"{'  ' + g['godot_version'] if g['godot_version'] else ''}")
    print(f"render:     {'yes' if g['can_render'] else 'no — ' + g['why_not_render']}")
    print(f"blender:    {a['blender'] or 'not installed'}")
    print(f"computer:   see={'yes' if a['can_see_screen'] else 'no'} "
          f"click={'yes' if a['can_click'] else 'no'}")
    b = report["budget"]
    print(f"budget:     ${b['spent_usd']:.2f} spent"
          + (f" of ${b['ceiling_usd']:.2f}" if b["ceiling_usd"] else " (no ceiling)"))
    for name, st in (report.get("cli") or {}).items():
        mark = "ok" if st["logged_in"] else ("NOT LOGGED IN" if st["bin"]
                                             else "not installed")
        plan = f"  [{st['plan']}]" if st.get("plan") else ""
        print(f"{name + ':':12s}{mark}{plan}")
    probe = report.get("provider_probe") or {}
    if probe.get("ok"):
        print(f"provider:   all {len(probe['models'])} studio models served"
              if not probe.get("unserved")
              else f"provider:   NOT SERVED: {probe['unserved']}")
    print()
    if report["ready"]:
        print("ready.")
        return 0
    print("problems:")
    for p in report["problems"]:
        print(f"  - {p}")
    return 1


def cmd_provision(args):
    from studio import provision
    plan = provision.ensure_opencode_models(write=args.write)
    if plan.get("written"):
        print(f"added {len(plan['missing'])} model(s) to {plan['path']}")
        print(f"backup: {plan['backup']}")
        for mid in sorted(plan["missing"]):
            print(f"  + {mid}")
        return 0
    if not plan.get("missing"):
        print(f"{plan['path']}: all studio models already present")
        return 0
    print(f"{plan['path']} is missing {len(plan['missing'])} studio model(s):")
    for mid, entry in sorted(plan["missing"].items()):
        print(f"  + {mid}  (context {entry['limit']['context']})")
    print("\nre-run with --write to apply (the file is backed up first)")
    return 1


def cmd_plan(args):
    if not _need_studio():
        return 2
    from studio import planner
    from studio.engine import stage_manager
    repo = str(Path(args.repo).expanduser().resolve())
    phase = args.phase or stage_manager.current_phase(args.project)
    print(f"planning {args.project} in {phase} against {repo} "
          f"with {config.PLANNER_MODEL} ...", file=sys.stderr)
    result = planner.plan(args.goal, repo, phase=phase, project=args.project,
                          out_path=args.out)
    print(f"wrote {result['path']}  ({result['tasks']} tasks, "
          f"${result['cost_usd']:.3f})")
    for t in result["taskfile"]["project"]["tasks"]:
        deps = f"  deps={t['deps']}" if t["deps"] else ""
        print(f"  {t['id']:28s} {t['model']:34s} rev={t['reviewer']}{deps}")
    print(f"\nnext: .venv/bin/python main.py code run {result['path']} --dry-run")
    return 0


def cmd_status(args):
    from studio.engine import stage_manager
    _print(stage_manager.describe(args.project, str(Path(args.repo).expanduser())))
    return 0


def _measure_first(repo, project=None, phase=None):
    """Refresh gate evidence on main; failed refreshes cannot use stale reports."""
    from studio.engine import godot, stage_manager
    from studio.schemas.task import phase_index
    needs_perf = bool(project and phase_index(phase or stage_manager.current_phase(project)) >= 3)
    needs_engine = (needs_perf or (Path(repo) / godot.MEASURER).exists()
                    or (Path(repo) / godot.PLAYTEST).exists())
    if not godot.available():
        if needs_engine:
            print("  godot not installed: cannot refresh gate evidence")
        return not needs_engine
    fresh = True
    try:
        m = godot.measure(repo)
        if m is not None:
            print(f"  measured {len(m)} dimension(s) on {repo}")
    except (godot.GodotError, ValueError) as exc:
        print(f"  measurer FAILED — {str(exc).splitlines()[0]}")
        fresh = False
    try:
        rep = godot.playtest(repo)
        if rep is not None:
            checks = rep.get("checks") or []
            ok = sum(1 for c in checks if isinstance(c, dict) and c.get("passed"))
            print(f"  playtest: {ok}/{len(checks)} checks passed")
    except (godot.GodotError, ValueError) as exc:
        print(f"  playtest FAILED — {str(exc).splitlines()[0]}")
        fresh = False
    if needs_perf:
        try:
            rep = godot.perf(repo)
            if rep is None:
                print("  perf FAILED — tools/perf.gd is missing")
                fresh = False
            else:
                print(f"  perf: {rep.get('fps_p5', rep.get('fps_avg'))} fps p5, "
                      f"{rep.get('shadow_lights')} shadow light(s)")
        except (godot.GodotError, ValueError) as exc:
            print(f"  perf FAILED — {str(exc).splitlines()[0]}")
            fresh = False
    return fresh


def cmd_gate(args):
    from studio.engine import stage_manager
    repo = str(Path(args.repo).expanduser())
    if not _measure_first(repo, args.project, args.phase):
        print("phase gate failed: fresh evidence could not be obtained")
        return 1
    result = stage_manager.check(args.project, repo, args.phase)
    print(f"phase {result['phase']}: {'PASS' if result['passed'] else 'FAIL'}")
    for f in result["failures"]:
        print(f"  - {f}")
    return 0 if result["passed"] else 1


def cmd_promote(args):
    from studio.engine import stage_manager
    if not _measure_first(str(Path(args.repo).expanduser()), args.project):
        print("not promoted: fresh evidence could not be obtained")
        return 1
    result = stage_manager.promote(args.project, str(Path(args.repo).expanduser()),
                                   force=args.force, reason=args.reason or "")
    if result["promoted"]:
        print(f"promoted {result['from']} -> {result['to']}"
              + ("  (FORCED over a failing gate)" if result.get("failures") else ""))
        for f in result.get("failures", []):
            print(f"  ! {f}")
        return 0
    print(f"not promoted: the {result['from']} gate has "
          f"{len(result['failures'])} failure(s)")
    for f in result["failures"]:
        print(f"  - {f}")
    return 1


def cmd_render(args):
    from studio.engine import godot, stage_manager
    from studio.evaluation import camera_system
    from studio.memory import compactor
    repo = str(Path(args.repo).expanduser().resolve())
    phase = args.phase or stage_manager.current_phase(args.project)
    round_n = args.round or (compactor.latest_round(args.project) + 1)
    cams = camera_system.cameras_for_round(args.project, round_n, project_dir=repo)
    out = compactor.round_dir(args.project, round_n, create=True) / "raw"
    print(f"rendering round {round_n}: {len(cams)} cameras "
          f"({sum(1 for c in cams if c.kind == 'adversarial')} adversarial)")
    shots = godot.render(repo, camera_system.to_json(cams), out,
                         scene=args.scene or "", resolution=args.resolution)
    meta = compactor.archive(args.project, round_n, shots, cameras=cams, phase=phase)
    print(f"archived {len(meta['images'])} renders to "
          f"{compactor.round_dir(args.project, round_n)}")
    return 0


def cmd_judge(args):
    if not _need_studio():
        return 2
    from studio.engine import stage_manager
    from studio.evaluation import judge_loop
    from studio.memory import compactor
    repo = str(Path(args.repo).expanduser().resolve())
    round_n = args.round or compactor.latest_round(args.project)
    if not round_n:
        print("no rendered rounds yet — run `studio render` first", file=sys.stderr)
        return 1
    phase = args.phase or stage_manager.current_phase(args.project)
    verdict = judge_loop.judge(args.project, round_n, project_dir=repo,
                               phase=phase, model=args.model)
    if verdict.get("crashed"):
        # Deliberately not a score. A judge that crashed did not judge, and
        # recording it as a low score would send the build back for defects
        # nobody named (AGENTS.md Rule 2).
        print(f"judge CRASHED ({verdict['model']}): {verdict['error']}",
              file=sys.stderr)
        print("re-run the judge; do not treat this as a rejection.",
              file=sys.stderr)
        return 3
    print(f"round {round_n}  judge {verdict['model']}  score {verdict['score']}"
          f"  (A {verdict['bucket_a_score']} / B {verdict['bucket_b_score']})"
          f"  {'PASS' if verdict['pass'] else 'FAIL'}   ${verdict['cost_usd']:.3f}")
    if verdict["summary"]:
        print(f"  {verdict['summary']}")
    for art in verdict["artifacts"]:
        print(f"  [{art['severity']:6s}] {art['camera']}: {art['kind']} — {art['note']}")
    for d in verdict["directives"]:
        print(f"  -> {d}")
    arb = judge_loop.assess(args.project)
    if arb.halt:
        print(f"\nARBITRATOR HALT: {arb.reason}", file=sys.stderr)
        return 4
    return 0 if verdict["pass"] else 1


def cmd_fuzz(args):
    from studio.qa import deepseek_fuzzer
    repo = str(Path(args.repo).expanduser().resolve())
    report = deepseek_fuzzer.fuzz(args.project, repo, bots=args.bots,
                                  seconds=args.seconds)
    print(f"{report['bots']} bots x {report['seconds']}s against "
          f"{report['endpoint']} ({report['transport']})")
    print(f"  sent {report['messages_sent']}, replies {report['replies_seen']}, "
          f"tick p50 {report['tick_p50_ms']}ms p95 {report['tick_p95_ms']}ms")
    print(f"  authority violations: {report['authority_violations']}")
    print(f"  crashes: {report['crashes']}")
    for v in report["violations"][:10]:
        print(f"    ! {v['case']}: server reported {v['server_state']}")
    for n in report["notes"]:
        print(f"    note: {n}")
    return 0 if not (report["authority_violations"] or report["crashes"]) else 1


def cmd_astra(args):
    if not _need_studio():
        return 2
    from studio.engine.operators import astra_operator
    result = astra_operator.run(args.goal, project=args.project,
                                max_steps=args.max_steps)
    for step in result["steps"]:
        tools = ", ".join(f"{t['tool']}{'' if t['ok'] else ' (FAILED)'}"
                          for t in step["tools"])
        print(f"[{step['step']:2d}] {tools or 'no tools'}")
        if step["text"]:
            print(f"      {step['text'][:400]}")
    print(f"\n{result['final']}")
    return 0 if result["ok"] else 1


def cmd_playtest(args):
    """Run the scripted playtest; its screenshots become a round for the judge.

    MEASURE and LOOK, the two halves of the published playtest method: the
    numbers print here and gate phase 1 onward; the screenshots taken along
    the route are archived as a render round, so `studio judge` looks at the
    game as it was actually PLAYED, not only from fixed cameras.
    """
    from studio.engine import godot
    from studio.memory import compactor
    from studio.engine import stage_manager
    repo = Path(args.repo).expanduser()
    rep = godot.playtest(repo)
    if rep is None:
        print(f"{repo} has no tools/playtest.gd — see docs/studio-fleet.md "
              "(Scripted playtest)")
        return 1
    checks = [c for c in rep.get("checks") or [] if isinstance(c, dict)]
    for c in checks:
        mark = "✓" if c.get("passed") else "✗"
        print(f"  {mark} {c.get('name')}: {c.get('value')!r} (expected {c.get('expected')!r})")
    shots = []
    for sp in rep.get("screenshots") or []:
        path = Path(str(sp).replace("res://", "")) if str(sp).startswith("res://") else Path(sp)
        path = path if path.is_absolute() else repo / path
        if path.exists():
            shots.append(path)
    if shots:
        n = compactor.latest_round(args.project) + 1
        cams = [{"name": s.stem, "kind": "playtest",
                 "note": "Screenshot taken during the scripted playtest, along "
                         "the route a player walks."} for s in shots]
        compactor.archive(args.project, n, shots, cameras=cams,
                          phase=stage_manager.current_phase(args.project),
                          note="scripted playtest")
        print(f"archived {len(shots)} playtest screenshot(s) as round {n} — "
              f"`studio judge {args.project} {args.repo}` to have them looked at")
    passed = bool(checks) and all(c.get("passed") for c in checks)
    print("PLAYTEST PASS" if passed else "PLAYTEST FAIL")
    return 0 if passed else 1


def cmd_approve(args):
    from studio import approvals
    state = "rejected" if args.reject else "approved"
    d = approvals.decide(args.project, args.asset, state, note=args.reject or "")
    print(f"{args.asset}: {d['state']}" + (f" — {d['note']}" if d["note"] else ""))
    return 0


def cmd_review_pack(args):
    from studio import review_pack
    out = review_pack.write(args.repo, args.pr, args.out or None)
    print(f"wrote {out}")
    print("Paste it into Gemini (Antigravity) or Cursor. Their verdict becomes a")
    print("label: manual-approved merges, manual-rejected + a comment sends it back.")
    if not config.PR_MANUAL_REVIEW:
        print("NOTE: the manual gate is OFF, so the fleet will not wait for this "
              "verdict. Run the fleet with ARC_PR_MANUAL_REVIEW=1 to make it wait.")
    return 0


def cmd_play(args):
    """Launch a build for a HUMAN to play (the dashboard's ▶ Play, from a shell)."""
    from studio import playtest
    rows = playtest.builds(args.project)
    if not rows:
        print(f"{args.project}: no builds (is its game repo known and on disk?)",
              file=sys.stderr)
        return 1
    build = args.build or rows[0]["id"]
    try:
        s = playtest.launch(args.project, build)
    except KeyError:
        print(f"unknown build {build!r}; known: {', '.join(b['id'] for b in rows)}",
              file=sys.stderr)
        return 2
    except playtest.Unavailable as exc:
        print(f"cannot play here: {exc}", file=sys.stderr)
        return 1
    snap = playtest.ensure_snapshot(args.project, build, do_import=False)
    sess = config.studio_run_dir(args.project) / "playtest" / "sessions" / s["id"]
    print(f"session:  {s['id']}  (pid {s['pid']})")
    print(f"build:    {build} @ {s['sha'][:12]}")
    print(f"snapshot: {snap}")
    print(f"session:  {sess}")
    print("F8 in game = log a finding with a screenshot; triage in the "
          "dashboard (Studio → Playtest) or with `studio findings`.")
    return 0


def cmd_findings(args):
    from studio import playtest
    rows = sorted(playtest.load_findings(args.project).values(),
                  key=lambda f: (f.get("severity") or 4, -(f.get("ts") or 0)))
    if args.state:
        rows = [f for f in rows if f.get("state") == args.state]
    if not rows:
        print("no findings" + (f" in state {args.state}" if args.state else ""))
        return 0
    print(f"{'id':11s} {'state':9s} sev {'category':8s} {'build':7s}  note")
    for f in rows:
        note = " ".join(str(f.get("note", "")).split())
        print(f"{f['id']:11s} {f.get('state', ''):9s} {f.get('severity')!s:3s} "
              f"{f.get('category', ''):8s} {(f.get('sha') or '')[:7]:7s}  "
              f"{note[:70]}{'…' if len(note) > 70 else ''}")
    return 0


def cmd_budget(args):
    from studio import budget
    _print(budget.summary())
    return 0


def cmd_scaffold(args):
    from studio import scaffold
    written = scaffold.create(args.repo, project=args.project, force=args.force)
    print(f"scaffolded {len(written)} files into {Path(args.repo).expanduser()}")
    for path in written:
        print(f"  + {path}")
    print("\nnext:")
    print("  1. edit studio_target.json — Bucket A must hold NUMBERS")
    print("  2. .venv/bin/python main.py studio gate "
          f"{args.project} {args.repo}")
    return 0


def run(args):
    table = {
        "doctor": cmd_doctor, "provision": cmd_provision, "plan": cmd_plan,
        "status": cmd_status, "gate": cmd_gate, "promote": cmd_promote,
        "render": cmd_render, "judge": cmd_judge, "fuzz": cmd_fuzz,
        "astra": cmd_astra, "budget": cmd_budget, "scaffold": cmd_scaffold,
        "playtest": cmd_playtest, "approve": cmd_approve,
        "review-pack": cmd_review_pack, "play": cmd_play,
        "findings": cmd_findings,
    }
    return table[args.studio_cmd](args)
