"""The studio workload: an autonomous 3D multiplayer game-development fleet.

This package is what `ARC_FLEET=studio` is for. It adds a game-development
workload on top of the orchestrator core the same way `code_tasks.py`,
`work.py` does — it does NOT replace or fork any of it.

What lives here (and only here):

    schemas/task.py            a game task, and how it compiles DOWN to an
                               ordinary taskfile entry so Rules 1-9 apply
    engine/stage_manager.py    Module B: the five phases and their promotion
                               gates
    engine/godot.py            the Godot 4 CLI: syntax checks, headless tests,
                               renders, exports
    engine/operators/          Module A: the Astra computer-use / Blender /
                               rigging operator
    evaluation/                Module C: anchor + adversarial cameras, the
                               blind judge loop, the oscillation arbitrator
    memory/compactor.py        Module D: render archival and context bounding
    qa/deepseek_fuzzer.py      Module E: the headless multiplayer fuzz swarm
    planner.py                 the studio planner prompt (phase- and
                               worker-aware)
    provision.py               one-time setup + `studio doctor`
    budget.py                  the spend guard the local fleet never needed

What does NOT live here, on purpose: worktrees, gates, review, publishing,
merging, escalation, resume. A game task is a taskfile task. It allocates a
worktree, runs its verify_cmd, is reviewed cross-family, opens a pull request
and merges through `gitstore` exactly like every other task in this repo. The
moment that stops being true, this package has become a second orchestrator.
"""
