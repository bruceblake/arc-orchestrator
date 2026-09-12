"""Orchestration-level benchmark: whole-DAG runs over policy variants.

bench.py measures single model x harness cells on micro tasks; this module
measures the *orchestration option set*: which routing/reviewer/harness/
fix-loop policy makes the full governed pipeline (alloc -> implement -> gate
-> review -> publish -> merge, worktrees, transcripts, events) deliver the
most merged, green code on a real project.

A benchmark project (6 deterministic tasks across basic/medium/hard tiers,
disjoint files, full fanout) is stamped as a fresh git repo per variant, the
real graph engine runs the DAG under the variant's policy, and results are
aggregated from the governed evidence tables (code_tasks, harness_runs).
"""
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import code_tasks
import config
import events

log = logging.getLogger("orchbench")

REPOS = Path.home() / "repos"
OUT_ROOT = Path(config.ROOT) / "logs" / "orchbench"

# ---------------------------------------------------------------------------
# benchmark project: filetoolkit (stdlib-only), 6 tasks, 3 tiers, no deps
# ---------------------------------------------------------------------------

_BOOT = ("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname"
         "(os.path.abspath(__file__))))\n")


def _t(body):
    return _BOOT + body + '\nprint("ok")\n'


TASKS = [
    {
        "id": "slug", "tier": "basic",
        "title": "slugify utility",
        "files": ["filetoolkit/slugify.py"],
        "prompt": (
            "Create `filetoolkit/slugify.py` (package `filetoolkit` already "
            "exists with an empty `__init__.py`; do not edit it) defining "
            "`slugify(text: str) -> str`: lowercase the input, replace every "
            "run of one or more non-alphanumeric characters with a single "
            "dash, strip leading/trailing dashes. ASCII only after lowering; "
            "strip characters that are not [a-z0-9]. Examples: "
            "slugify('Hello, World!') == 'hello-world'; "
            "slugify(' --A  b__c-- ') == 'a-b-c'; slugify('') == ''; "
            "slugify('Café') == 'caf'. No dependencies, stdlib only."),
        "verify_cmd": "python3 tests/test_slug.py",
        "test": _t('''
from filetoolkit.slugify import slugify
assert slugify("Hello, World!") == "hello-world"
assert slugify(" --A  b__c-- ") == "a-b-c"
assert slugify("") == ""
assert slugify("Café") == "caf"
assert slugify("one--TWO_ _three") == "one-two-three"
'''),
    },
    {
        "id": "hist", "tier": "basic",
        "title": "character histogram",
        "files": ["filetoolkit/hist.py"],
        "prompt": (
            "Create `filetoolkit/hist.py` (do not edit `__init__.py`) "
            "defining `char_hist(text: str, top: int | None = None) -> list`: "
            "a list of (character, count) tuples for every character in "
            "text (whitespace included), sorted by count descending, ties "
            "broken by character ascending (ordinal). If `top` is an int, "
            "return only the first `top` tuples. Examples: "
            "char_hist('aab') == [('a', 2), ('b', 1)]; "
            "char_hist('ba a', top=2) == [('a', 2), (' ', 1)]. "
            "Empty text -> []. Stdlib only."),
        "verify_cmd": "python3 tests/test_hist.py",
        "test": _t('''
from filetoolkit.hist import char_hist
assert char_hist("aab") == [("a", 2), ("b", 1)]
assert char_hist("ba a", top=2) == [("a", 2), (" ", 1)]
assert char_hist("") == []
assert char_hist("xyz") == [("x", 1), ("y", 1), ("z", 1)]
assert char_hist("aabbcc") == [("a", 2), ("b", 2), ("c", 2)]
'''),
    },
    {
        "id": "jflat", "tier": "medium",
        "title": "json flatten/unflatten",
        "files": ["filetoolkit/jflat.py"],
        "prompt": (
            "Create `filetoolkit/jflat.py` defining two functions. "
            "`flatten(obj, sep='.') -> dict`: flatten a nested structure of "
            "dicts and lists into a single dict with joined keys; dict keys "
            "are their string form, list indices are their decimal index. "
            "`flatten({'a': {'b': [1, 2]}, 'c': 3}) == {'a.b.0': 1, "
            "'a.b.1': 2, 'c': 3}`. Flattening a scalar raises ValueError. "
            "`unflatten(d, sep='.') -> object`: exact inverse — rebuild "
            "nested dicts/lists (consecutive integer keys from 0 become "
            "lists), e.g. unflatten(flatten(x)) == x for nested x. Keys "
            "like '01' or non-integers stay dict keys. Stdlib only."),
        "verify_cmd": "python3 tests/test_jflat.py",
        "test": _t('''
from filetoolkit.jflat import flatten, unflatten
x = {"a": {"b": [1, 2], "c": {"d": "e"}}, "f": [True, None]}
f = flatten(x)
assert f == {"a.b.0": 1, "a.b.1": 2, "a.c.d": "e", "f.0": True, "f.1": None}, f
assert unflatten(f) == x
assert flatten({"k": 1}) == {"k": 1}
try:
    flatten(5)
except ValueError:
    pass
else:
    raise AssertionError("scalar flatten did not raise")
assert unflatten({"a.01.b": 2}) == {"a": {"01": {"b": 2}}}
'''),
    },
    {
        "id": "wrap", "tier": "medium",
        "title": "greedy word wrap",
        "files": ["filetoolkit/wrap.py"],
        "prompt": (
            "Create `filetoolkit/wrap.py` defining "
            "`wrap(text: str, width: int) -> list[str]`: greedy word wrap. "
            "Split on whitespace (collapsing all runs), pack words "
            "left-to-right so no line exceeds `width` characters, separate "
            "words on a line with single spaces. A single word longer than "
            "`width` gets its own line unbroken. Raise ValueError if "
            "width < 1. Empty or all-whitespace text -> []. Examples: "
            "wrap('the quick brown fox', 10) == ['the quick', 'brown fox']; "
            "wrap('aaa bbbbbb cc', 5) == ['aaa', 'bbbbbb', 'cc']. Stdlib "
            "only."),
        "verify_cmd": "python3 tests/test_wrap.py",
        "test": _t('''
from filetoolkit.wrap import wrap
assert wrap("the quick brown fox", 10) == ["the quick", "brown fox"]
assert wrap("aaa bbbbbb cc", 5) == ["aaa", "bbbbbb", "cc"]
assert wrap("", 5) == []
assert wrap("   ", 5) == []
assert wrap("a b c d e", 3) == ["a b", "c d", "e"]
assert wrap("hello", 5) == ["hello"]
try:
    wrap("x", 0)
except ValueError:
    pass
else:
    raise AssertionError("width=0 did not raise")
'''),
    },
    {
        "id": "tmpl", "tier": "hard",
        "title": "mini template engine",
        "files": ["filetoolkit/tmpl.py"],
        "prompt": (
            "Create `filetoolkit/tmpl.py` defining "
            "`render(template: str, context: dict) -> str`, a minimal "
            "template engine supporting: `{{name}}` and dotted "
            "`{{user.name}}` substitution (str() of the looked-up value; "
            "missing key raises KeyError); `{% for item in items %}..."
            "{% endfor %}` loops over any iterable, binding `item` inside "
            "the body (the loop variable name is whatever the tag says); "
            "`{% if name %}...{% else %}...{% endif %}` conditionals where "
            "the `{%- else -%}` arm is optional and truthiness is Python "
            "truthiness of the looked-up value. Tags and expressions may "
            "have arbitrary surrounding whitespace. Nesting of for inside "
            "for and if inside for must work. Anything outside tags is "
            "copied verbatim. Unbalanced tags raise ValueError. Stdlib "
            "only. Do not edit `__init__.py`."),
        "verify_cmd": "python3 tests/test_tmpl.py",
        "test": _t('''
from filetoolkit.tmpl import render
assert render("Hi {{name}}!", {"name": "Ada"}) == "Hi Ada!"
assert render("{{a.b}}", {"a": {"b": 3}}) == "3"
assert render("{% for x in xs %}{{x}};{% endfor %}", {"xs": [1, 2]}) == "1;2;"
assert render("{% if ok %}Y{% else %}N{% endif %}", {"ok": 0}) == "N"
assert render("{% if ok %}Y{% endif %}", {"ok": 1}) == "Y"
assert render("{% for p in ps %}[{{p.n}}]{% endfor %}",
              {"ps": [{"n": "a"}, {"n": "b"}]}) == "[a][b]"
try:
    render("{{missing}}", {})
except KeyError:
    pass
else:
    raise AssertionError("missing key did not raise")
try:
    render("{% for x in xs %}never closed", {"xs": []})
except ValueError:
    pass
else:
    raise AssertionError("unbalanced tags did not raise")
'''),
    },
    {
        "id": "topo", "tier": "hard",
        "title": "deterministic topological sort",
        "files": ["filetoolkit/topo.py"],
        "prompt": (
            "Create `filetoolkit/topo.py` defining "
            "`toposort(deps: dict) -> list[str]`: keys are node names, each "
            "value is a list of nodes that must come BEFORE the key. Return "
            "an ordering where every dependency precedes its dependent. "
            "Among all valid choices be deterministic: whenever several "
            "nodes are simultaneously available, pick the lexicographically "
            "smallest. Nodes referenced only inside value lists are part of "
            "the graph too. If the graph has a cycle, raise ValueError whose "
            "message starts with 'cycle'. Examples: "
            "toposort({'b': ['a'], 'c': ['a']}) == ['a', 'b', 'c']; "
            "toposort({'x': ['y', 'z']}) == ['x' is unavailable until 'y' "
            "and 'z']; toposort({}) == []. Stdlib only. Do not edit "
            "`__init__.py`."),
        "verify_cmd": "python3 tests/test_topo.py",
        "test": _t('''
from filetoolkit.topo import toposort
assert toposort({"b": ["a"], "c": ["a"]}) == ["a", "b", "c"]
assert toposort({"x": ["y", "z"]}) == ["y", "z", "x"]
assert toposort({}) == []
assert toposort({"a": []}) == ["a"]
r = toposort({"mod": ["lib", "util"], "lib": ["util"], "main": ["mod"]})
assert r == ["util", "lib", "mod", "main"], r
try:
    toposort({"a": ["b"], "b": ["a"]})
except ValueError as e:
    assert str(e).startswith("cycle"), str(e)
else:
    raise AssertionError("cycle did not raise")
'''),
    },
]

DEFAULT_ROUTING = {  # tier -> (implementer model, reviewer token)
    "basic": ("gpt-oss-120b", "glm"),
    "medium": ("DeepSeek-V4.1-Flash-thinking-max", "kimi"),
}
DEFAULT_HARD = {"tmpl": ("GLM-5.3", "kimi"), "topo": ("Kimi-K3", "glm")}


# ---------------------------------------------------------------------------
# policy variants — the option set under test
# ---------------------------------------------------------------------------
# Each variant: {"desc", "policy": {...code_tasks policy hooks...},
#                "route": fn(tid, tier, model, reviewer) -> (model, reviewer)}

def _keep(tid, tier, model, reviewer):
    return model, reviewer


def _reviews_to(token):
    def route(tid, tier, model, reviewer):
        return model, token
    return route


def _route_all(model, reviewer):
    def route(tid, tier, m, r):
        return model, reviewer
    return route


def _self_review(tid, tier, model, reviewer):
    return model, {"Kimi-K3": "kimi", "GLM-5.3": "glm"}.get(model, model)


def _misroute(tid, tier, model, reviewer):
    swap = {"slug": ("Kimi-K3", "glm"), "hist": ("Kimi-K3", "glm"),
            "jflat": ("gpt-oss-120b", "glm"), "wrap": ("gpt-oss-120b", "glm"),
            "tmpl": ("gpt-oss-120b", "kimi"), "topo": ("DeepSeek-V4.1-Flash-thinking-max", "kimi")}
    return swap[tid]


def _extra_reviewers(model, tiers):
    def route(tid, tier, m, rev):
        return m, (model if tier in tiers else rev)
    return route


ALL_MODELS = ["gpt-oss-120b", "DeepSeek-V4.1-Flash-thinking-max", "GLM-5.3", "Kimi-K3"]

VARIANTS = {
    "default": {
        "desc": "governed baseline: tier routing, mandatory cross-review kimi<->glm",
        "policy": {}, "route": _keep, "code": "d",
    },
    "glm-implement-only": {
        "desc": "GLM-5.3 never reviews; Kimi-K3 reviews everything (incl. itself)",
        "policy": {"allow_self_review": True}, "route": _reviews_to("kimi"), "code": "gio",
    },
    "kimi-implement-only": {
        "desc": "Kimi-K3 never reviews; GLM-5.3 reviews everything (incl. itself)",
        "policy": {"allow_self_review": True}, "route": _reviews_to("glm"), "code": "kio",
    },
    "deepseek-reviews": {
        "desc": "DeepSeek-V4.1-Flash-thinking-max allowed as reviewer; reviews basic+medium tasks",
        "policy": {"reviewers": ("kimi", "glm", "DeepSeek-V4.1-Flash-thinking-max"),
                   "allow_self_review": True},
        "route": _extra_reviewers("DeepSeek-V4.1-Flash-thinking-max", ("basic", "medium")),
        "code": "dr",
    },
    "gptoss-reviews": {
        "desc": "gpt-oss-120b allowed as reviewer; reviews medium-tier tasks",
        "policy": {"reviewers": ("kimi", "glm", "gpt-oss-120b")},
        "route": _extra_reviewers("gpt-oss-120b", ("medium",)),
        "code": "gr",
    },
    "self-review": {
        "desc": "implementer reviews its own work (no cross-review rule)",
        "policy": {"reviewers": ("kimi", "glm") + tuple(ALL_MODELS),
                   "allow_self_review": True},
        "route": _self_review, "code": "sr",
    },
    "no-review": {
        "desc": "review stage skipped entirely (gate is the only check)",
        "policy": {"review": False}, "route": _keep, "code": "nr",
    },
    "kimi-via-opencode": {
        "desc": "Kimi-K3 implements through opencode instead of the kimi CLI",
        "policy": {"harness": {"Kimi-K3": "opencode"}},
        "route": _keep, "code": "kvo",
    },
    "all-glm": {
        "desc": "flat routing: every task implemented by GLM-5.3",
        "policy": {}, "route": _route_all("GLM-5.3", "kimi"), "code": "ag",
    },
    "all-deepseek": {
        "desc": "flat routing: every task implemented by DeepSeek-V4.1-Flash-thinking-max",
        "policy": {}, "route": _route_all("DeepSeek-V4.1-Flash-thinking-max", "glm"), "code": "ad",
    },
    "all-kimi": {
        "desc": "flat routing: every task implemented by Kimi-K3",
        "policy": {}, "route": _route_all("Kimi-K3", "glm"), "code": "ak",
    },
    "misroute": {
        "desc": "deliberate anti-tier routing (weak on hard, strong on basic)",
        "policy": {}, "route": _misroute, "code": "mr",
    },
    "no-fixloop": {
        "desc": "no fix rounds: first gate/review failure fails the task",
        "policy": {"max_fix_rounds": 0}, "route": _keep, "code": "nf",
    },
    "fixloop-1": {
        "desc": "exactly one fix round allowed",
        "policy": {"max_fix_rounds": 1}, "route": _keep, "code": "f1",
    },
}


# ---------------------------------------------------------------------------
# project factory + variant runner + scoring
# ---------------------------------------------------------------------------

def _git_sync(args, cwd):
    proc = subprocess.run(["git"] + args, cwd=str(cwd),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {proc.stderr[:300]}")


def stamp_project(repo):
    """Fresh `filetoolkit` repo: scaffold + all task tests, initial main."""
    if repo.exists():
        shutil.rmtree(repo)
    (repo / "filetoolkit").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "README.md").write_text(
        "# filetoolkit — orchbench project\n\nStdlib-only text/json utilities.\n",
        encoding="utf-8")
    (repo / "filetoolkit" / "__init__.py").write_text("", encoding="utf-8")
    for t in TASKS:
        (repo / "tests" / f"test_{t['id']}.py").write_text(t["test"],
                                                           encoding="utf-8")
    _git_sync(["init", "-b", "main"], repo)
    _git_sync(["add", "-A"], repo)
    _git_sync(["-c", "user.name=orchbench", "-c",
               "user.email=orchbench@localhost", "commit", "-m", "scaffold"],
              repo)
    return repo


def _default_route(tid, tier):
    if tid in DEFAULT_HARD:
        return DEFAULT_HARD[tid]
    return DEFAULT_ROUTING[tier]


def taskfile_for(variant, repo):
    v = VARIANTS[variant]
    tasks = []
    for t in TASKS:
        model, rev = _default_route(t["id"], t["tier"])
        model, rev = v["route"](t["id"], t["tier"], model, rev)
        tasks.append({
            "id": f"{v['code']}-{t['id']}", "title": t["title"],
            "prompt": t["prompt"], "model": model, "reviewer": rev,
            "verify_cmd": t["verify_cmd"], "files_hint": t["files"],
        })
    return {"project": {"repo": str(repo), "title": f"orchbench:{variant}",
                        "tasks": tasks}}


def integration_score(repo):
    """Run every task test against merged main; (green, total)."""
    green = 0
    for t in TASKS:
        proc = subprocess.run(["python3", f"tests/test_{t['id']}.py"],
                              cwd=str(repo), capture_output=True)
        green += proc.returncode == 0
    return green, len(TASKS)


def tf_key(variant, stamp):
    return f"orchbench:{variant}:{stamp}"


async def run_variant(store, name, stamp, *, plan_only=False):
    v = VARIANTS[name]
    repo = REPOS / f"orchbench-{stamp}-{v['code']}"
    if not plan_only:
        stamp_project(repo)
    out_dir = OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    tf_path = out_dir / f"{name}.json"
    tf_path.write_text(json.dumps(taskfile_for(name, repo), indent=1),
                       encoding="utf-8")
    pol = {"tolerate_driver_error": True, **v["policy"]}
    taskset = code_tasks.load_taskfile(tf_path, policy=pol)
    if plan_only:
        return {"variant": name, "describe": code_tasks.describe(taskset)}
    t0 = time.monotonic()
    events.set_context(workload="orchbench", module=name)
    events.emit("orchbench.variant", variant=name, repo=str(repo),
                policy=json.dumps(pol))
    g = code_tasks.build_code_graph(store, taskset,
                                    taskfile=tf_key(name, stamp),
                                    policy=pol)
    await g.run({})
    secs = round(time.monotonic() - t0, 1)

    rows = store.code_tasks_for(tf_key(name, stamp))
    runs = store.harness_runs_prefix(f"{v['code']}-")
    merged = sum(1 for r in rows if r["status"] == "merged")
    failed = sum(1 for r in rows if r["status"] == "failed")
    conflict = sum(1 for r in rows if r["status"] == "conflict")
    impl = [r for r in runs if r["role"] == "implementer"]
    rev = [r for r in runs if r["role"] == "reviewer"]
    rejects = sum(1 for r in rev if r["verdict"] and '"pass": false' in r["verdict"])
    green, total = integration_score(repo)
    result = {
        "variant": name, "stamp": stamp, "desc": v["desc"],
        "merged": merged, "failed": failed, "conflict": conflict,
        "tasks": len(rows), "integration": f"{green}/{total}",
        "impl_sessions": len(impl), "review_sessions": len(rev),
        "fix_rounds_used": max(0, len(impl) - len(rows)),
        "review_rejects": rejects,
        "harness_seconds": round(sum(r["seconds"] or 0 for r in runs), 1),
        "wall_seconds": secs, "errors": [f"{r['id']}:{r['status']}:{(r['error'] or '')[:60]}"
                                         for r in rows if r["status"] != "merged"],
    }
    with open(out_dir / "results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")
    events.emit("orchbench.variant.done", variant=name, merged=merged,
                green=green, seconds=secs)
    return result


def report(results):
    lines = ["=== orchestration variant benchmark ===",
             "per variant: full DAG run on a fresh filetoolkit repo "
             "(6 tasks, 3 tiers), merges scored against the task test suite",
             "",
             f"{'variant':<19}{'merged':>7}{'fail':>5}{'confl':>6}"
             f"{'green':>7}{'fixr':>5}{'rejr':>5}{'har-s':>8}{'wall-s':>8}"]
    for r in sorted(results, key=lambda x: (-x["merged"], x["wall_seconds"])):
        lines.append(f"{r['variant']:<19}{r['merged']:>7}{r['failed']:>5}"
                     f"{r['conflict']:>6}{r['integration']:>7}"
                     f"{r['fix_rounds_used']:>5}{r['review_rejects']:>5}"
                     f"{r['harness_seconds']:>8.0f}{r['wall_seconds']:>8.0f}")
    lines.append("")
    lines.append("green = task tests passing on merged main; fixr = extra "
                 "implementer sessions beyond the first; rejr = review "
                 "verdicts with pass:false (review catching defects)")
    return "\n".join(lines)
