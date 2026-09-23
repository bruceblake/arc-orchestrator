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


def _measure_first(repo):
    """Refresh Bucket A measurements on the repo before a gate reads them."""
    from studio.engine import godot
    if not (Path(repo) / godot.MEASURER).exists():
        return True
    if not godot.available():
        print("  godot not installed: cannot refresh measurements")
        return False
    try:
        m = godot.measure(repo)
    except godot.GodotError as exc:
        print(f"  measurer FAILED — {str(exc).splitlines()[0]}")
        return False
    if m is not None:
        print(f"  measured {len(m)} dimension(s) on {repo}")
    return True


def cmd_gate(args):
    from studio.engine import stage_manager
    repo = str(Path(args.repo).expanduser())
    if not _measure_first(repo):
        print("phase gate failed: fresh measurements could not be obtained")
        return 1
    result = stage_manager.check(args.project, repo, args.phase)
    print(f"phase {result['phase']}: {'PASS' if result['passed'] else 'FAIL'}")
    for f in result["failures"]:
        print(f"  - {f}")
    return 0 if result["passed"] else 1


def cmd_promote(args):
    from studio.engine import stage_manager
    if not _measure_first(str(Path(args.repo).expanduser())):
        print("not promoted: fresh measurements could not be obtained")
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
    }
    return table[args.studio_cmd](args)
