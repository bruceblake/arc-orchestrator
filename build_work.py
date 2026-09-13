"""Minecraft-style web game build workload.

Topology:
    planner -> produce_{module} x6 (parallel gauntlet inside each) -> assemble [gather]
    assemble -> integration_review -> metrics_store
    integration_review -> wiring_fix -> integration_review   (bounded cycle)

The per-module gauntlet lives INSIDE produce nodes (implement -> syntax gate ->
contract check -> cross-model review -> fix loop), so the assemble gather fires
exactly once per build; the bounded cycle only loops integration_review/wiring_fix.
"""
import logging
import re
import subprocess

import config
import events
from config import FAMILY_ORDER
from graph import Graph
from work import parse_json_loose

log = logging.getLogger("build")

MODULES = [
    {"name": "engine", "file": "js/engine.js",
     "desc": "fixed-timestep game loop, keyboard/mouse input state, simple event bus"},
    {"name": "world", "file": "js/world.js",
     "desc": "voxel chunk storage, block get/set, procedural terrain (value noise), meshing into THREE.BufferGeometry"},
    {"name": "player", "file": "js/player.js",
     "desc": "AABB physics vs voxels (gravity, jump, collision), pointer-lock camera, block break/place via raycast"},
    {"name": "ui", "file": "js/ui.js",
     "desc": "HUD: crosshair, hotbar with block palette (keys 1-9), FPS counter, pause/help overlay"},
    {"name": "main", "file": "js/main.js",
     "desc": "bootstrap: create Engine/World/Player/UI, THREE.Scene/camera/renderer, wire update+render loop, resize handling"},
    {"name": "html", "file": "index.html",
     "desc": "page shell: dark styling, canvas mount point, three.js CDN script, then engine/world/player/ui/main script tags in dependency order"},
]
MODULE_NAMES = [m["name"] for m in MODULES]

DEFAULT_CONTRACTS = {
    "engine": ["Engine"],
    "world": ["World"],
    "player": ["Player"],
    "ui": ["UI"],
    "main": ["Main"],
    "html": ["<html", "</html>"],
}

GAME_SPEC = (
    "A browser Minecraft-style voxel sandbox (three.js from CDN, plain <script> tags in "
    "dependency order, NO ES module imports/exports — classes attach to window). Features: "
    "procedural terrain with hills and trees, walk/jump/sprint physics with AABB collision, "
    "pointer-lock mouse look, left-click break / right-click place blocks, hotbar with several "
    "block types selectable via number keys, crosshair + FPS counter, pause on Escape, fog + "
    "simple directional lighting, fixed-timestep update loop."
)


def extract_code(text, module):
    fences = re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL)
    if fences:
        return max(fences, key=len).rstrip() + "\n"
    low = text.lower()
    if module == "html" and ("<html" in low or "<!doctype" in low):
        i = low.find("<!doctype")
        if i == -1:
            i = low.find("<html")
        return text[i:].rstrip() + "\n"
    raise ValueError("no fenced code block in model output")


def js_syntax_ok(code):
    """Fallback JS sanity check (used when node is unavailable): a small lexer
    that skips comments/strings/regexes and validates bracket nesting,
    including nested template literals with ${...} substitutions."""
    n = len(code)
    pairs = {')': '(', ']': '[', '}': '{'}
    regex_before = set("(,=:[!&|?{};+-*%~^<>")
    regex_keywords = {"return", "typeof", "case", "in", "of", "new", "delete",
                      "void", "instanceof", "do", "else", "yield", "await"}

    def skip_line_comment(i):
        while i < n and code[i] != "\n":
            i += 1
        return i

    def skip_block_comment(i):
        j = code.find("*/", i + 2)
        return n if j == -1 else j + 2

    def skip_quoted(i, q):
        i += 1
        while i < n:
            if code[i] == "\\":
                i += 2
                continue
            if code[i] == q:
                return i + 1
            if code[i] == "\n" and q != "`":
                return -1
            i += 1
        return -1

    def skip_regex(i):
        i += 1
        in_class = False
        while i < n:
            ch = code[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "\n":
                return i
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                return i + 1
            i += 1
        return n

    def regex_context(i):
        j = i - 1
        while j >= 0 and code[j] in " \t\n":
            j -= 1
        if j < 0:
            return True
        if code[j] in regex_before:
            return True
        k = j
        while k >= 0 and (code[k].isalnum() or code[k] in "_$"):
            k -= 1
        word = code[k + 1:j + 1]
        return word in regex_keywords

    def scan_substitution(i, stack):
        depth = 1
        while i < n:
            c = code[i]
            if code.startswith("//", i):
                i = skip_line_comment(i)
            elif code.startswith("/*", i):
                i = skip_block_comment(i)
            elif c in "\"'":
                j = skip_quoted(i, c)
                if j == -1:
                    return i, False
                i = j
            elif c == "`":
                i, ok = scan_template(i + 1, stack)
                if not ok:
                    return i, False
            elif c == "/":
                if regex_context(i):
                    i = skip_regex(i)
                else:
                    i += 1
            elif c in "([{":
                stack.append(c)
                depth += 1
                i += 1
            elif c in ")]}":
                if depth == 1:
                    if c != "}":
                        return i, False
                    return i + 1, True
                if not stack or stack[-1] != pairs[c]:
                    return i, False
                stack.pop()
                depth -= 1
                i += 1
            else:
                i += 1
        return n, False

    def scan_template(i, stack):
        while i < n:
            c = code[i]
            if c == "\\":
                i += 2
            elif c == "`":
                return i + 1, True
            elif c == "$" and i + 1 < n and code[i + 1] == "{":
                i, ok = scan_substitution(i + 2, stack)
                if not ok:
                    return i, False
            else:
                i += 1
        return n, False

    stack = []
    i = 0
    while i < n:
        c = code[i]
        if code.startswith("//", i):
            i = skip_line_comment(i)
        elif code.startswith("/*", i):
            i = skip_block_comment(i)
        elif c in "\"'":
            j = skip_quoted(i, c)
            if j == -1:
                return False, "unterminated string"
            i = j
        elif c == "`":
            i, ok = scan_template(i + 1, stack)
            if not ok:
                return False, "unterminated template literal"
        elif c == "/":
            if regex_context(i):
                i = skip_regex(i)
            else:
                i += 1
        elif c in "([{":
            stack.append(c)
            i += 1
        elif c in ")]}":
            if not stack or stack.pop() != pairs[c]:
                return False, f"unbalanced '{c}'"
            i += 1
        else:
            i += 1
    if stack:
        return False, f"unclosed brackets: {''.join(stack)}"
    return True, ""


def syntax_check(path, module):
    if module == "html":
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        ok = "<html" in text and "</html>" in text and "<script" in text
        return ok, "" if ok else "missing <html>/<script> elements"
    try:
        r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stderr or "").strip()[:400]
    except FileNotFoundError:
        return js_syntax_ok(path.read_text(encoding="utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        return False, "node --check timed out"


async def produce_module(pool, store, *, build_id, iteration, module_info, contracts,
                         producer, reviewer, mode, directives, out_dir, current_code):
    """The verification gauntlet for one module: implement -> gates -> review -> fix loop."""
    name, fname = module_info["name"], module_info["file"]
    events.set_context(module=name)
    dest = out_dir / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    others = " ".join(
        f"{m['name']}({', '.join(contracts.get(m['name'], []))})" for m in MODULES if m["name"] != name
    )
    total_tokens = 0
    total_ms = 0
    gate_attempts = 0
    review_score = None
    verdict = ""
    attempt = 0
    code = current_code if mode in ("improve", "fix") else None
    best_code = None
    feedback = "\n".join(f"- {d}" for d in directives) if directives else ""

    while attempt < config.MAX_MODULE_RETRIES:
        attempt += 1
        prompt = (
            "You are writing one file of a multi-file browser game; other files are written by "
            f"other agents.\n\nGAME SPEC:\n{GAME_SPEC}\n\nMODULE: {name}\nFILE: {fname}\n"
            f"ROLE: {module_info['desc']}\n"
            f"CONTRACT — your file MUST define these global symbols: {contracts.get(name, [])}\n"
            f"OTHER MODULES (globals available at load time): {others}\n"
            "Rules: plain JavaScript for <script> tags (no import/export), define exactly the "
            "contract symbols on window, keep under 350 lines, be complete and functional.\n"
        )
        if code is not None:
            prompt += f"\nCURRENT CODE (rewrite/improve it):\n```\n{code}\n```\n"
        if feedback:
            prompt += f"\nFIX DIRECTIVES:\n{feedback}\nAddress these precisely."
        meta = {}
        text = await pool.chat(producer, [{"role": "user", "content": prompt}],
                               purpose="implement", meta=meta)
        total_tokens += meta.get("tokens", 0)
        total_ms += meta.get("latency_ms", 0)
        try:
            code = extract_code(text, name)
        except ValueError as exc:
            feedback = f"previous output unusable: {exc}"
            events.emit("gate", module=name, gate="extract", passed=False, attempt=attempt)
            continue
        dest.write_text(code, encoding="utf-8")
        ok, err = syntax_check(dest, name)
        gate_attempts += 1
        events.emit("gate", module=name, gate="syntax", passed=ok, attempt=attempt, error=err[:200])
        if not ok:
            feedback = f"syntax check failed:\n{err}"
            continue
        missing = [s for s in contracts.get(name, []) if s not in code]
        events.emit("gate", module=name, gate="contract", passed=not missing,
                    attempt=attempt, missing=missing)
        if missing:
            feedback = f"missing contract symbols: {missing}"
            continue
        best_code = code
        rmeta = {}
        rtext = await pool.chat(
            reviewer,
            [{"role": "user", "content": (
                f"Review this '{name}' module of a browser voxel game for correctness and completeness. "
                f"Contract: {contracts.get(name)}\n"
                'Return ONLY JSON: {"score": <0-10>, "verdict": "ok"|"needs_fix", "issues": "<one sentence>"}\n\n'
                f"```\n{code}\n```"
            )}],
            purpose="review", meta=rmeta,
        )
        total_tokens += rmeta.get("tokens", 0)
        total_ms += rmeta.get("latency_ms", 0)
        try:
            d = parse_json_loose(rtext)
            review_score = float(d.get("score", 0))
            verdict = str(d.get("verdict", ""))
            issues = str(d.get("issues", ""))[:300]
        except Exception:
            review_score, verdict, issues = 0.0, "needs_fix", "review unparseable"
        events.emit("review", module=name, reviewer=reviewer, score=review_score,
                    verdict=verdict, attempt=attempt)
        if verdict == "ok" and review_score >= config.REVIEW_PASS_SCORE:
            break
        feedback = f"reviewer ({reviewer}) scored {review_score}/10: {issues}"

    passed = bool(verdict == "ok" and review_score is not None
                  and review_score >= config.REVIEW_PASS_SCORE)
    final_code = best_code if best_code is not None else (
        current_code if mode in ("improve", "fix") else code)
    if final_code is not None:
        dest.write_text(final_code, encoding="utf-8")
    store.save_build_module(build_id, name, fname, producer, reviewer, attempt, gate_attempts,
                            review_score, passed, total_tokens, total_ms, final_code or "")
    log.info("module %-7s done: producer=%s reviewer=%s attempt=%d passed=%s score=%s",
             name, producer, reviewer, attempt, passed, review_score)
    return {"module": name, "file": fname, "producer": producer, "reviewer": reviewer,
            "passed": passed, "score": review_score, "attempts": attempt,
            "gate_attempts": gate_attempts, "tokens": total_tokens, "latency_ms": total_ms}


def build_build_graph(pool, store, *, build_id, iteration, mode, out_dir,
                      current_files=None, start_directives=None):
    current_files = current_files or {}
    contracts_holder = {}

    # `% 4` was written when the roster had exactly four families. It is two
    # today (gpt-oss removed 2026-09-12; Kimi-K3 retired the same day by
    # operator decision) and any literal modulus would index past the end of
    # the list. The reviewer offset is +1 off the producer rather than a fixed
    # +2, because +2 with an even number of families lands a module's producer
    # on its own review — with two families +1 is the ONLY cross-family choice.
    nfam = len(FAMILY_ORDER)

    def producer_of(name):
        return FAMILY_ORDER[(iteration + MODULE_NAMES.index(name)) % nfam]

    def reviewer_of(name):
        return FAMILY_ORDER[(iteration + MODULE_NAMES.index(name) + 1) % nfam]

    def integrator():
        return FAMILY_ORDER[(iteration + 1) % nfam]

    async def planner(ctx):
        fam = FAMILY_ORDER[iteration % nfam]
        meta = {}
        prompt = (
            "Plan file-level contracts for a multi-file browser game.\n\nGAME SPEC:\n"
            f"{GAME_SPEC}\n\nMODULES:\n"
            + "\n".join(f"- {m['name']} ({m['file']}): {m['desc']}" for m in MODULES)
            + "\n\nFor each module list 1-4 global symbols (class names / window globals) it must "
            "define. Keep names stable so other modules can reference them (e.g. main uses "
            'window.Engine).\nReturn ONLY JSON: {"modules": {"engine": ["Engine"], ...}}'
        )
        text = await pool.chat(fam, [{"role": "user", "content": prompt}], purpose="plan", meta=meta)
        mods = {}
        try:
            d = parse_json_loose(text)
            mods = {k: [str(s) for s in v] for k, v in d.get("modules", {}).items() if k in MODULE_NAMES}
        except Exception:
            pass
        for k in MODULE_NAMES:
            if not mods.get(k):
                mods[k] = DEFAULT_CONTRACTS[k]
        contracts_holder.update(mods)
        events.emit("plan", family=fam, contracts=mods, tokens=meta.get("tokens"))
        log.info("build %d contracts (planner=%s): %s", build_id, fam, mods)
        return {"contracts": mods, "planner": fam}

    def make_produce(minfo):
        name = minfo["name"]

        async def produce(ctx):
            return await produce_module(
                pool, store, build_id=build_id, iteration=iteration, module_info=minfo,
                contracts=contracts_holder, producer=producer_of(name), reviewer=reviewer_of(name),
                mode=mode, directives=start_directives or [], out_dir=out_dir,
                current_code=current_files.get(name),
            )

        return produce

    async def assemble(ctx):
        results = [ctx["results"][f"produce_{m}"] for m in MODULE_NAMES]
        passed = sum(1 for r in results if r["passed"])
        events.emit("assemble", modules=len(results), gauntlet_passed=passed)
        log.info("assembled %d modules (%d passed gauntlet)", len(results), passed)
        return {"modules": results, "passed": passed}

    async def integration_review(ctx):
        rn = ctx.get("integration_round", 1)
        fam = integrator()
        listing = []
        for m in MODULES:
            p = out_dir / m["file"]
            code = p.read_text(encoding="utf-8", errors="replace")[:9000] if p.exists() else "(missing)"
            listing.append(f"=== {m['file']} ===\n{code}")
        meta = {}
        prompt = (
            "You are reviewing an integrated browser voxel game for wiring defects between "
            "modules. Check that globals referenced by one module are defined by another, that "
            "the script load order in index.html is correct, and that cross-module APIs match.\n"
            'Return ONLY JSON: {"passed": true|false, "score": <0-10>, "fixes": '
            '[{"module": "<name>", "directive": "<precise fix>"}]}\n'
            f"Valid module names: {', '.join(MODULE_NAMES)}. If not passed, "
            "fixes MUST name at least one module to change.\n\n"
            f"Integration round: {rn}\n\n" + "\n\n".join(listing)
        )
        text = await pool.chat(fam, [{"role": "user", "content": prompt}],
                               purpose="integration", meta=meta)
        def norm_module(s):
            s = str(s).strip().lower()
            for pre in ("js/", "./"):
                if s.startswith(pre):
                    s = s[len(pre):]
            if s.endswith(".js"):
                s = s[:-3]
            return s.strip()
        try:
            d = parse_json_loose(text)
            score = float(d.get("score", 0))
            passed = bool(d.get("passed")) or score >= 8.0
            fixes = []
            for f in d.get("fixes", []):
                nm = norm_module(f.get("module", ""))
                if nm in MODULE_NAMES:
                    fixes.append({"module": nm,
                                  "directive": str(f.get("directive", ""))[:300]})
        except Exception:
            passed, score, fixes = False, 0.0, []
        events.emit("integration", family=fam, round=rn, passed=passed, score=score,
                    fixes=len(fixes), tokens=meta.get("tokens"))
        log.info("integration review round %d (reviewer=%s): passed=%s score=%s fixes=%d",
                 rn, fam, passed, score, len(fixes))
        return {"passed": passed, "score": score, "fixes": fixes, "round": rn}

    async def wiring_fix(ctx):
        ir = ctx["results"]["integration_review"]
        rn = ir["round"] + 1
        ctx["integration_round"] = rn
        by_mod = {}
        for f in ir["fixes"]:
            by_mod.setdefault(f["module"], []).append(f["directive"])
        if not by_mod:
            asm = ctx["results"]["assemble"]["modules"]
            weak = [r for r in asm if not r.get("passed")] or \
                   sorted(asm, key=lambda r: r.get("score") or 0)[:1]
            for r in weak:
                by_mod[r["module"]] = [
                    f"Integration review round {ir['round']} failed (score {ir['score']}/10) "
                    "but named no module to fix. Rework this module so it integrates cleanly "
                    "with the others: honor the contracts, the script load order in index.html, "
                    "and the cross-module global APIs."]
        events.emit("wiring", round=rn, modules=sorted(by_mod))
        log.info("wiring fix round %d: %s", rn, sorted(by_mod))
        out = {}
        for name, directives in by_mod.items():
            minfo = next(m for m in MODULES if m["name"] == name)
            p = out_dir / minfo["file"]
            cur = p.read_text(encoding="utf-8", errors="replace") if p.exists() else current_files.get(name)
            out[name] = await produce_module(
                pool, store, build_id=build_id, iteration=iteration, module_info=minfo,
                contracts=contracts_holder, producer=producer_of(name), reviewer=reviewer_of(name),
                mode="fix", directives=directives, out_dir=out_dir, current_code=cur,
            )
        return {"fixed": sorted(by_mod), "round": rn,
                **{f"refixed_{k}": v for k, v in out.items()}}

    async def metrics_store(ctx):
        ir = ctx["results"]["integration_review"]
        asm = ctx["results"]["assemble"]
        wiring = ctx["results"].get("wiring_fix", {})
        rows = {}
        for m in MODULE_NAMES:
            r = ctx["results"].get(f"produce_{m}")
            rr = wiring.get(f"refixed_{m}")
            if rr:
                r = rr
            rows[m] = r
        total_tokens = sum((r or {}).get("tokens", 0) for r in rows.values())
        events.emit("build_metrics", total_tokens=total_tokens,
                    gauntlet_passed=asm["passed"], integration_rounds=ir["round"])
        log.info("build metrics: %d modules, %d tokens, integration rounds=%d, integration passed=%s",
                 len(rows), total_tokens, ir["round"], ir["passed"])
        return {"modules": rows, "integration_rounds": ir["round"],
                "integration_passed": ir["passed"], "total_tokens": total_tokens,
                "gauntlet_passed": asm["passed"]}

    g = Graph("build", max_steps=config.MAX_GRAPH_STEPS)
    g.node("planner", planner)
    for m in MODULES:
        g.node(f"produce_{m['name']}", make_produce(m))
    g.node("assemble", assemble, gather=True)
    g.node("integration_review", integration_review)
    g.node("wiring_fix", wiring_fix)
    g.node("metrics_store", metrics_store)

    g.start("planner")
    for m in MODULES:
        g.edge("planner", f"produce_{m['name']}")
        g.edge(f"produce_{m['name']}", "assemble")
    g.edge("assemble", "integration_review")
    g.edge("integration_review", "metrics_store",
           when=lambda r, ctx: r["passed"] or r["round"] >= config.MAX_INTEGRATION_ROUNDS)
    g.edge("integration_review", "wiring_fix",
           when=lambda r, ctx: (not r["passed"]) and r["round"] < config.MAX_INTEGRATION_ROUNDS)
    g.edge("wiring_fix", "integration_review")
    return g