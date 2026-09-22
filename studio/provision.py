"""One-time setup, and the `studio doctor` report.

Two jobs:

  PROVISION. The studio models reach opencode through the `openrouter`
  provider in the operator's own opencode config. Each needs a model entry
  there or the harness cannot name it. `ensure_opencode_models` adds them
  idempotently, backing the file up first, and by default only SHOWS what it
  would do — this edits a file outside the repo that the operator also uses
  interactively, so it asks before it writes.

  DIAGNOSE. `doctor` answers "what can this machine actually do right now",
  honestly, per capability. The studio has more external dependencies than
  the rest of this repo put together — a key, six models, Godot, Blender, a
  display, a screenshot tool, an input tool — and a half-provisioned machine
  fails deep inside a run, hours in, with an error about something else.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import config
from studio import budget, openrouter
from studio.engine import godot
from studio.engine.operators import astra_operator

# Real windows as OpenRouter published them on 2026-09-22. The fleet config
# generator lowers `context` to config.EXTERNAL_CONTEXT for fleet runs; these
# are what the operator's own interactive opencode sees.
MODEL_LIMITS = {
    "openai/gpt-6-astra":        {"context": 1050000, "output": 32768},
    "openai/gpt-6-sol":          {"context": 1050000, "output": 32768},
    "openai/gpt-6-luna":         {"context": 1050000, "output": 32768},
    "anthropic/claude-opus-5.5": {"context": 1000000, "output": 32768},
    "x-ai/grok-4.7":             {"context": 500000,  "output": 32768},
    "google/gemini-3.8-flash":   {"context": 1048576, "output": 32768},
}


def _studio_entries():
    """{provider_model_id: entry} for every external model on the roster."""
    out = {}
    for model in sorted(config.EXTERNAL_MODELS):
        alias = config.MODEL_HARNESS_ALIAS.get(model, "")
        prov, _, mid = alias.partition("/")
        if prov != "openrouter" or not mid:
            continue
        out[mid] = {"name": model,
                    "limit": dict(MODEL_LIMITS.get(mid, {"context": 262144,
                                                         "output": 32768}))}
    return out


def opencode_plan():
    """What provisioning would change in the operator's opencode config."""
    path = config.OPENCODE_CONFIG
    if not path.exists():
        return {"path": str(path), "exists": False, "missing": _studio_entries(),
                "present": {}, "error": f"{path} does not exist"}
    doc = json.loads(path.read_text(encoding="utf-8"))
    models = ((doc.get("provider") or {}).get("openrouter") or {}).get("models") or {}
    wanted = _studio_entries()
    missing = {k: v for k, v in wanted.items() if k not in models}
    return {"path": str(path), "exists": True, "present": {k: models[k]
                                                           for k in wanted if k in models},
            "missing": missing,
            "has_openrouter_provider": bool(
                (doc.get("provider") or {}).get("openrouter"))}


def ensure_opencode_models(write=False):
    """Add the studio models to the opencode config. Returns the plan."""
    plan = opencode_plan()
    if not write or not plan.get("missing"):
        return plan
    path = Path(plan["path"])
    backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
    shutil.copy2(path, backup)
    doc = json.loads(path.read_text(encoding="utf-8"))
    prov = doc.setdefault("provider", {}).setdefault("openrouter", {})
    if not prov.get("npm"):
        prov.update({
            "name": "OpenRouter",
            "npm": "@openrouter/ai-sdk-provider",
            "options": {"baseURL": config.OPENROUTER_BASE_URL,
                        "apiKey": "{env:OPENROUTER_API_KEY}"},
        })
    prov.setdefault("models", {}).update(plan["missing"])
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    plan["written"] = True
    plan["backup"] = str(backup)
    return plan


def cli_status(timeout=45):
    """Is each subscription CLI installed AND logged in?

    This is the check that actually matters on the subscription profile. An
    unauthenticated CLI does not fail at import or at roster build; it fails
    inside the first harness run, minutes in, as an exit code with a login
    prompt in the transcript. Asking up front turns that into one line.
    """
    import subprocess
    out = {}

    claude = Path(config.claude_bin())
    logged_in = False
    try:
        doc = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        acct = doc.get("oauthAccount") or {}
        logged_in = bool(acct.get("emailAddress"))
        plan = acct.get("organizationType", "")
    except (OSError, ValueError):
        plan = ""
    out["claude"] = {"bin": str(claude) if claude.exists() else "",
                     "logged_in": logged_in, "plan": plan,
                     "login_cmd": "claude  (then /login)"}

    codex = Path(config.codex_bin())
    ok = False
    if codex.exists():
        try:
            r = subprocess.run([str(codex), "login", "status"],
                               capture_output=True, text=True, timeout=timeout)
            ok = "not logged in" not in ((r.stdout or "") + (r.stderr or "")).lower()
        except (OSError, subprocess.SubprocessError):
            ok = False
    out["codex"] = {"bin": str(codex) if codex.exists() else "",
                    "logged_in": ok, "plan": "",
                    "login_cmd": f"{codex} login"}

    gemini = Path(config.gemini_bin())
    # The Gemini CLI has no status subcommand; its OAuth credentials landing
    # under ~/.gemini is the observable signal.
    home = Path.home() / ".gemini"
    creds = any((home / n).exists() for n in
                ("oauth_creds.json", "google_accounts.json", "access_tokens.json"))
    settings_auth = False
    try:
        settings_auth = "auth" in (home / "settings.json").read_text(encoding="utf-8").lower()
    except OSError:
        pass
    out["gemini"] = {"bin": str(gemini) if gemini.exists() else "",
                     "logged_in": bool(creds or settings_auth), "plan": "",
                     "login_cmd": f"{gemini}  (choose 'Login with Google')"}
    return out


def probe_models(timeout=30):
    """Ask OpenRouter which studio models it actually serves today.

    The roster's dates are a plan; the provider is the fact — the same
    reasoning as config.live_roster, applied to the provider this fleet's
    external models come from. A renamed or withdrawn model shows up here
    instead of as a 404 twenty minutes into a run.
    """
    key = openrouter.api_key()
    if not key:
        return {"ok": False, "error": "OPENROUTER_API_KEY is not set"}
    try:
        import urllib.request
        req = urllib.request.Request(
            config.OPENROUTER_BASE_URL.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            served = {m["id"] for m in json.loads(resp.read())["data"]}
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {}
    for model in sorted(config.EXTERNAL_MODELS):
        mid = config.MODEL_HARNESS_ALIAS[model].partition("/")[2]
        out[model] = {"provider_id": mid, "served": mid in served}
    return {"ok": True, "models": out,
            "unserved": sorted(m for m, v in out.items() if not v["served"])}


def doctor(probe=True):
    """What works, what does not, and what to do about it."""
    g = godot.doctor()
    a = astra_operator.doctor()
    report = {
        "fleet": config.FLEET,
        "studio_profile_active": config.STUDIO,
        "roster": sorted(config.MODEL_ROLES) if config.STUDIO else sorted(config.MODEL_ROLES),
        "planner": config.PLANNER_MODEL,
        "external_models": sorted(config.EXTERNAL_MODELS),
        "openrouter_key": bool(openrouter.api_key()),
        "opencode": opencode_plan(),
        "godot": g,
        "astra": a,
        "budget": budget.summary(),
        "studio_dir": str(config.STUDIO_DIR),
    }
    if config.STUDIO and not config.STUDIO_API:
        report["cli"] = cli_status()
    if probe and config.STUDIO_API:
        report["provider_probe"] = probe_models()
    problems = []
    if not config.STUDIO:
        problems.append(
            "ARC_FLEET is 'local': the studio roster is not loaded. Every "
            "studio command needs ARC_FLEET=studio.")
    if not report["openrouter_key"]:
        problems.append(
            "OPENROUTER_API_KEY is not set — no studio model can be called.")
    for name, st in (report.get("cli") or {}).items():
        if not st["bin"]:
            problems.append(
                f"the {name} CLI is not installed; the studio profile routes "
                f"a worker through it")
        elif not st["logged_in"]:
            problems.append(
                f"the {name} CLI is installed but NOT logged in — run: "
                f"{st['login_cmd']}")
    if report["opencode"].get("missing"):
        problems.append(
            "the opencode config is missing studio models "
            f"({sorted(report['opencode']['missing'])}); run "
            "`main.py studio provision --write`. Without them the harness "
            "cannot run those models, though direct calls (judge, Astra) "
            "still work.")
    if not g["godot_bin"]:
        problems.append("godot is not installed: `sudo pacman -S godot`")
    if not g["display"]:
        problems.append(
            "no DISPLAY: renders are impossible (--headless has no renderer). "
            "WSLg provides :0; otherwise run Xvfb and set ARC_STUDIO_DISPLAY.")
    for missing in a["missing"]:
        if missing == "blender":
            problems.append("blender is not installed: `sudo pacman -S blender`")
        elif missing.startswith("ffmpeg"):
            problems.append("no screen capture tool: `sudo pacman -S ffmpeg`")
        elif missing == "xdotool":
            problems.append(
                "xdotool is not installed: `sudo pacman -S xdotool` "
                "(only needed for computer-use clicks)")
    probe_r = report.get("provider_probe") or {}
    if probe_r.get("unserved"):
        problems.append(
            f"OpenRouter does not serve {probe_r['unserved']} — the roster "
            "names a model the provider no longer has.")
    report["problems"] = problems
    report["ready"] = not problems
    return report
