import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

API_KEY = os.getenv("ARC_API_KEY", "")
BASE_URL = os.getenv("ARC_BASE_URL", "https://llm-api.arc.vt.edu/api/v1")
DB_PATH = os.getenv("ARC_DB_PATH") or str(ROOT / "orchestrator.db")

FAMILY_ORDER = ["gpt-oss", "glm", "kimi", "deepseek"]


@dataclass(frozen=True)
class Family:
    name: str
    limit: int
    models: dict
    websearch_model: str = ""


FAMILIES = {
    "gpt-oss": Family(
        "gpt-oss",
        5,
        {
            "default": "gpt-oss-120b",
            "low": "gpt-oss-120b-thinking-low",
            "high": "gpt-oss-120b-thinking-high",
        },
        websearch_model="gpt-oss-120b-thinking-high-legacy-tool-calling",
    ),
    "glm": Family(
        "glm",
        4,
        {
            "default": "GLM-5.3",
            "high": "GLM-5.3-thinking-high",
        },
        websearch_model="glm-52-thinking-high-legacy-tool-calling",
    ),
    "kimi": Family(
        "kimi",
        3,
        {
            "default": "Kimi-K3",
            "low": "Kimi-K3-thinking-low",
            "high": "Kimi-K3-thinking-high",
        },
        websearch_model="Kimi-K3-thinking-max-legacy-tool-calling",
    ),
    "deepseek": Family(
        "deepseek",
        5,
        {
            "default": "DeepSeek-V4-Flash",
            "low": "DeepSeek-V4-Flash-thinking-low",
            "max": "DeepSeek-V4-Flash-thinking-max",
        },
        websearch_model="DeepSeek-V4-Flash-thinking-max-legacy-tool-calling",
    ),
}

QUESTIONS_PER_ROUND = int(os.getenv("ARC_QUESTIONS_PER_ROUND", "10"))
SEEDS_PER_ROUND = int(os.getenv("ARC_SEEDS_PER_ROUND", "3"))
PIPELINE_ROUNDS = int(os.getenv("ARC_PIPELINE_ROUNDS", "2"))
MAX_VERIFY_ROUNDS = int(os.getenv("ARC_MAX_VERIFY_ROUNDS", "3"))
VERIFY_PASS_SCORE = float(os.getenv("ARC_VERIFY_PASS_SCORE", "7.5"))
REQUEST_TIMEOUT = float(os.getenv("ARC_REQUEST_TIMEOUT", "600"))
# Retry budgets are GENEROUS on purpose. Tokens are not the scarce resource
# here; a task abandoned one attempt short of working is. Every budget below
# is still finite, because a task that cannot succeed must eventually stop
# rather than hold a worktree, a branch and a model slot forever — but the
# ceilings are set where "gave up" means "genuinely could not", not "ran out
# of patience". Raised 09-11 from 4/3/3/2/3/3 after graph-admission-control
# died with a passing gate because its reviewer crashed three times.
MAX_RETRIES = int(os.getenv("ARC_MAX_RETRIES", "12"))
ROUND_COOLDOWN = float(os.getenv("ARC_ROUND_COOLDOWN", "5"))
STATS_INTERVAL = float(os.getenv("ARC_STATS_INTERVAL", "300"))
MAX_GRAPH_STEPS = int(os.getenv("ARC_MAX_GRAPH_STEPS", "6000"))
SESSION_RETRIES = int(os.getenv("ARC_SESSION_RETRIES", "12"))
SESSION_BACKOFF_CAP = float(os.getenv("ARC_SESSION_BACKOFF_CAP", "30"))

EVENTS_LOG = os.getenv("ARC_EVENTS_LOG") or str(ROOT / "logs" / "events.jsonl")
BUILD_OUTPUT_DIR = os.getenv("ARC_BUILD_OUTPUT_DIR") or str(ROOT / "production" / "minecraft")
DASHBOARD_PORT = int(os.getenv("ARC_DASHBOARD_PORT", "8787"))
# --- dashboard exposure -----------------------------------------------------
# The dashboard listens on every interface so a phone on the same wifi can
# open it; that is the point of it. It has NO login. Every GET is open to
# whoever can reach the port, and so was every POST — and a POST is not a
# view: it writes a task file, starts or stops a fleet run, opens a pull
# request. /api/projects/create takes a verify_cmd the gate later runs as a
# shell command, and /api/projects/run launches agents that push to GitHub.
#
# Two knobs. BIND narrows who can reach the port at all (127.0.0.1 for this
# machine only; a Tailscale address for your own devices anywhere). TOKEN
# gates every POST: when set, a request must carry
# "Authorization: Bearer <token>"; the dashboard asks for it once and keeps
# it in the browser. GETs stay open either way — the pages are meant to be
# glanced at from a phone without a login step.
DASHBOARD_BIND = os.getenv("ARC_DASHBOARD_BIND", "0.0.0.0")
DASHBOARD_TOKEN = os.getenv("ARC_DASHBOARD_TOKEN", "")
MAX_MODULE_RETRIES = int(os.getenv("ARC_MAX_MODULE_RETRIES", "3"))
MAX_INTEGRATION_ROUNDS = int(os.getenv("ARC_MAX_INTEGRATION_ROUNDS", "3"))
REVIEW_PASS_SCORE = float(os.getenv("ARC_REVIEW_PASS_SCORE", "6.5"))


def family_limit(name):
    """Per-family concurrency, overridable via ARC_LIMIT_<FAMILY> env vars.

    ARC enforces some caps per user account (not per process), so when other
    agents share the key these overrides keep this orchestrator polite.
    """
    override = os.getenv(f"ARC_LIMIT_{name.upper().replace('-', '_')}")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return FAMILIES[name].limit

# --- multi-harness code workload -------------------------------------------
# Implementation is tiered by task difficulty: gpt-oss-120b takes very basic
# tasks, DeepSeek-V4-Flash takes medium ones, and GLM-5.3 / Kimi-K3 take the
# hard tasks on top of their planning and reviewing duties. Every task is
# reviewed by kimi or glm, never by the same harness that implemented it.
# ARC rejects over-limit requests per model, so driver caps reserve headroom
# for interactive use of the account.
WORKTREE_ROOT = os.getenv("ARC_WORKTREE_ROOT") or str(Path.home() / "worktrees")
TASKS_DIR = os.getenv("ARC_TASKS_DIR") or str(Path.home() / "tasks")
# The only directory tree the dashboard will accept a project repo from
# (/api/projects/create and every taskfile it runs). A network client can
# name any path in a POST body; this is the fence. It was the operator's
# literal home directory, which made the dashboard — and its tests — refuse
# every path on any other machine.
REPO_ROOT = os.getenv("ARC_REPO_ROOT") or str(Path.home())
# Outer backstop only. DRIVER_IDLE_TIMEOUT below is the instrument that
# actually detects a hung harness, and it is the precise one: it measures
# silence. This wall clock exists for the pathological case where a harness
# dribbles output forever without converging.
#
# It was 900s, which made it the BINDING limit on real work rather than a
# backstop: a Kimi-K3 implement was killed at exactly 900s having written
# 149KB with only 80s of idle — it was demonstrably still working, and each
# such kill costs a full retry (MAX_RETRIES=4, so an hour per task).
DRIVER_TIMEOUT = float(os.getenv("ARC_DRIVER_TIMEOUT", "2700"))
# A harness that produces no stdout for this long is killed and retried.
#
# This is NOT a hang detector, and treating it as one cost real work. ARC
# QUEUES requests rather than refusing them: measured across 1927 completed
# steps, median time-to-first-token is 1.0s at every context size, but the
# tail grows with context and reaches 308.9s — after which the response
# streams normally in 0.3s. Time-to-first-token IS stdout silence, so a short
# idle timeout kills requests that were about to succeed.
#
# Per-task probability of killing healthy work at >=40k context (and these are
# LOWER bounds — steps we killed leave no telemetry, so the real tail is worse):
#
#     idle    per-step   median task   p90 task
#      60s      0.91%        5.3%       18.1%
#     120s      0.41%        2.4%        8.7%
#     300s      0.08%        0.5%        1.8%
#     420s      0.00%        0.0%        0.0%
#
# 420s clears the observed tail. A genuinely dead request costs 7 minutes;
# DRIVER_TIMEOUT bounds the total. Shortening this to "fail fast" trades a
# small latency saving for a large chance of destroying finished work.
DRIVER_IDLE_TIMEOUT = float(os.getenv("ARC_DRIVER_IDLE_TIMEOUT", "420"))
# While a driver runs, emit driver.progress this often: bytes written, idle
# time, and a /proc sample. Makes a live agent's progress observable instead of
# inferred from transcript file size, and gives the stall event a CPU baseline
# to diff against (spinning agent vs blocked request).
DRIVER_PROGRESS_INTERVAL = float(os.getenv("ARC_DRIVER_PROGRESS_INTERVAL", "60"))
# A harness rejected at the ARC account cap never got a slot, so retrying it
# on the crash schedule (2s, 4s, 8s) walks straight back into the same cap.
# Capacity rejections back off on this longer, jittered ladder instead.
DRIVER_CAPACITY_BACKOFF = float(os.getenv("ARC_DRIVER_CAPACITY_BACKOFF", "45"))
DRIVER_CAPACITY_BACKOFF_CAP = float(os.getenv("ARC_DRIVER_CAPACITY_BACKOFF_CAP", "300"))
# Driver leases (store.driver_leases) enforce per-model driver caps ACROSS
# orchestrator processes — a terminal queue and dashboard-launched runs cannot
# stack. Rows this old are reaped (owner assumed dead; pid liveness is checked
# first). Must exceed DRIVER_TIMEOUT + retry backoffs.
# DERIVED, not a free constant: a lease reaped while its driver is still
# running lets another driver take the slot, and the model goes over its ARC
# cap — the exact failure the leases exist to prevent. It must therefore
# outlast the longest an attempt can legitimately hold one, which is
# DRIVER_TIMEOUT plus the retry backoff before the next attempt. Pinning this
# to a literal meant raising DRIVER_TIMEOUT silently broke the invariant.
DRIVER_LEASE_TTL = float(os.getenv("ARC_DRIVER_LEASE_TTL", "0")) or (
    DRIVER_TIMEOUT + DRIVER_CAPACITY_BACKOFF_CAP + 300)
# How long a driver may wait for a per-model lease before giving up. Without a
# bound this wait was `while True:` — a task could queue behind a saturated
# model forever, before its own timeout clock had even started. Exceeding it
# raises a capacity-classified DriverError, so the retry ladder backs off
# instead of the run silently stalling.
DRIVER_LEASE_WAIT = float(os.getenv("ARC_DRIVER_LEASE_WAIT", "5400"))
# --- branch model + PR review ------------------------------------------------
# The pull request is the GATE, not a receipt: the branch is pushed, PR_REVIEWERS
# reviewers read the actual PR diff, and a merger only merges once every one of
# them approves. Before this, work was merged locally and the PR opened
# afterwards — reviewers could object to nothing, because it had already landed.
#
# ONE branch by default. The fleet opens its pull requests against BASE_BRANCH
# and that is where reviewed work lands.
#
# This was development -> main with a manual promotion PR between them. The
# split cost more than it bought here: the operator's checkout, the fleet's
# base and the promotion target were three different moving refs, and several
# bugs came straight out of that — reconcile compared task branches against
# main while the fleet merged into development, so its cleanup never ran; and a
# day's work sat on an unpushed local main while every task branched from a
# development that did not contain it.
#
# Set ARC_BASE_BRANCH=development (and keep PROD_BRANCH=main) to restore the
# two-branch flow with `main.py code promote`; nothing about it was removed.
BASE_BRANCH = os.getenv("ARC_BASE_BRANCH", "main")
PROD_BRANCH = os.getenv("ARC_PROD_BRANCH", "main")


def promotion_configured():
    """True when there is a separate branch to promote INTO.

    With one branch, a promotion PR would be main -> main: GitHub rejects it,
    and offering the button implies a gate that does not exist.
    """
    return BASE_BRANCH != PROD_BRANCH
# What the operator ASKED for: two independent cross-family readings per PR.
PR_REVIEWERS_WANTED = int(os.getenv("ARC_PR_REVIEWERS", "2"))
# What the roster can DELIVER: an implementer's PR can only be read by the
# other PR-review-capable families (pr_reviewer role — a superset of the
# pre-merge `reviewer` role), so the ceiling is (families - 1). Three
# families -> 2; after Kimi-K3 leaves on 09-19, two families -> 1. The
# effective value is the smaller, so the config never promises a gate the
# fleet cannot staff; the audit compares delivered against wanted and says so.
# PR_REVIEWERS (the effective value) is computed after the roster below.
# How many times a PR may go back to the implementer before the task fails.
PR_MAX_ROUNDS = int(os.getenv("ARC_PR_MAX_ROUNDS", "8"))
# Retries of a review that reached NO verdict (every reviewer crashed).
# Separate from PR_MAX_ROUNDS on purpose: an infrastructure failure must
# not consume the rounds reserved for real disagreement about the code.
PR_MAX_INCONCLUSIVE = int(os.getenv("ARC_PR_MAX_INCONCLUSIVE", "10"))

# How many times a conflicting PR may be resynced with the base before
# giving up. Each resync rewrites the branch and costs a fresh review,
# so this is deliberately small.
PR_MAX_RESYNCS = int(os.getenv("ARC_PR_MAX_RESYNCS", "6"))

# Retries of a PRE-MERGE review that crashed instead of reaching a
# verdict. Separate from the fix budget on purpose: a reviewer that
# could not run has not objected to anything, and spending a fix round
# on it sends the implementer to repair code nobody criticised.
MAX_REVIEW_CRASHES = int(os.getenv("ARC_MAX_REVIEW_CRASHES", "10"))
# Every task must add or update tests. Reviewers are told to reject a code
# change that ships none, and the gate reports it.
REQUIRE_TESTS = os.getenv("ARC_REQUIRE_TESTS", "1").lower() not in ("0", "false", "no", "")

GATE_TIMEOUT = float(os.getenv("ARC_GATE_TIMEOUT", "180"))
MAX_FIX_ROUNDS = int(os.getenv("ARC_MAX_FIX_ROUNDS", "8"))
# Project chaining (code workload): a taskfile that declares `after` waits for
# every task in those upstream taskfiles to reach 'merged' before any of its
# worktrees allocate. Upstream projects can legitimately take hours (fix
# loops, PR review rounds, escalation tiers), so the wait budget is hours,
# not minutes. A chain that never settles must eventually fail loudly rather
# than sit on the dashboard forever.
CHAIN_TIMEOUT = float(os.getenv("ARC_CHAIN_TIMEOUT", str(6 * 3600)))

# --- GitHub operations agents (gh_ops.py) ------------------------------------
# Standalone gh-CLI agents (issue triage, issue drafting, PR review) — NOT the
# governed code pipeline: no worktree, no gate, no publish. Only Kimi-K3 and
# GLM-5.3 may hold the gh roles (driver validation enforces it), every command
# previews by default, and --apply-labels/--create/--post are the only writes.
# Default None: gh_ops falls back to PLANNER_MODEL, which follows the roster
# (Kimi-K3 until 09-19, GLM-5.3 after). A hardcoded default here would name a
# withdrawn model the morning after it left.
GH_MODEL = os.getenv("ARC_GH_MODEL") or None
GH_TIMEOUT = float(os.getenv("ARC_GH_TIMEOUT", "60"))

# Model escalation (code workload): when a task exhausts its fix rounds at its
# current tier, it retries one tier stronger with a fresh fix budget instead of
# failing; it only fails when the last tier exhausts. gpt-oss-120b may
# legitimately exhaust immediately on a task planned too optimistically, so the
# path still reaches a strong model within a couple of escalations.
# DeepSeek-V4-Flash is the ENTRY tier, not gpt-oss-120b. Measured over 103
# implement runs across 31 tasks:
#
#            gate pass   end-to-end   implement runs/task   escalated
#   gpt-oss     59.1%       25.0%            5.5              36%
#   DeepSeek    71.0%       51.6%            1.7               0%
#
# gpt-oss time is cheap (cap 8) but its REVIEWS are not: gpt-oss-started tasks
# were 35% of tasks and consumed 54.7% of all reviewer runs, every one of them
# executed by GLM-5.3 or Kimi-K3 — the two capped models that are the fleet's
# actual scarce resource. Cheap retries paid for with expensive reviews is a
# bad trade. gpt-oss stays available for explicitly-routed mechanical work
# (docs, one-line edits); it is just no longer where every task starts.
# ---------------------------------------------------------------------------
# THE MODEL ROSTER — the one table every other model constant derives from.
#
# Models come and go on dates the provider sets, not on dates we choose:
#   - gpt-oss-120b is retired from this fleet now (operator decision, 09-11).
#   - DeepSeek-V4-Flash is replaced by DeepSeek-V4.1-Flash on 2026-09-12.
#   - Kimi-K3 is withdrawn on 2026-09-19.
# Each row carries the window it is available in. Everything below — tiers,
# families, the escalation path, concurrency caps, harness routing — is
# computed from the rows that are live TODAY, so a transition is a date in
# this table rather than an edit in six places on the morning it happens.
#
# ARC_ROSTER_DATE=YYYY-MM-DD previews any day's roster without waiting for it.
# That is how the 09-12 and 09-19 states were tested before they arrived.
# ---------------------------------------------------------------------------
import datetime as _dt

# (model, family, harness, tier, measured_concurrency, roles, from, until)
# `from` inclusive, `until` exclusive; None = open-ended.
#
# roles: which of implementer / reviewer / pr_reviewer / planner the model may
# hold. DeepSeek-V4-Flash may review an open PR but not gate or plan (judging
# a bounded diff is a smaller job than authoring; planning is not). Its 4.1
# successor gets the full set — after Kimi-K3 leaves on 09-19 it is the ONLY
# cross-family reviewer GLM's work can have, and a fleet with one reviewable
# family has no cross-review at all.
ALL_ROLES = ("implementer", "reviewer", "pr_reviewer", "planner")
ROSTER = [
    ("DeepSeek-V4-Flash",   "deepseek", "opencode", "medium", 5,
     ("implementer", "pr_reviewer"),                       None,         "2026-09-12"),
    ("DeepSeek-V4.1-Flash", "deepseek", "opencode", "medium", 5,
     ALL_ROLES,                                            "2026-09-12", None),
    ("GLM-5.3",             "glm",      "opencode", "hard",   4,
     ALL_ROLES,                                            None,         None),
    ("Kimi-K3",             "kimi",     "kimi",     "hard",   3,
     ALL_ROLES,                                            None,         "2026-09-19"),
]
TIER_ORDER = ["medium", "hard"]   # weakest first; "basic" is gone with gpt-oss


def roster_date():
    """Today, or ARC_ROSTER_DATE for previewing a future roster."""
    override = os.getenv("ARC_ROSTER_DATE")
    if override:
        return _dt.date.fromisoformat(override)
    return _dt.date.today()


def live_roster(day=None):
    day = day or roster_date()
    out = []
    for m, fam, harness, tier, cap, roles, start, end in ROSTER:
        if start and day < _dt.date.fromisoformat(start):
            continue
        if end and day >= _dt.date.fromisoformat(end):
            continue
        out.append((m, fam, harness, tier, cap, roles))
    return out


def roster_changes(day=None, horizon_days=14):
    """Transitions inside the next `horizon_days`, for the audit to announce."""
    day = day or roster_date()
    out = []
    for m, fam, harness, tier, cap, roles, start, end in ROSTER:
        for kind, d in (("arrives", start), ("leaves", end)):
            if not d:
                continue
            dd = _dt.date.fromisoformat(d)
            if day <= dd <= day + _dt.timedelta(days=horizon_days):
                out.append({"model": m, "change": kind, "on": d,
                            "in_days": (dd - day).days})
    return sorted(out, key=lambda c: c["on"])


_LIVE = live_roster()
IMPLEMENTER_MODELS = {m for m, _f, _h, _t, _c, roles in _LIVE if "implementer" in roles}
IMPLEMENT_TIERS = {tier: [m for m, _f, _h, t, _c, _r in _LIVE if t == tier]
                   for tier in TIER_ORDER}
IMPLEMENT_TIERS = {k: v for k, v in IMPLEMENT_TIERS.items() if v}
MODEL_FAMILY = {m: fam for m, fam, *_ in _LIVE}
MODEL_HARNESS = {m: h for m, _f, h, *_ in _LIVE}
MODEL_ROLES = {m: set(roles) for m, _f, _h, _t, _c, roles in _LIVE}
_MEASURED_CONCURRENCY = {m: cap for m, _f, _h, _t, cap, _r in _LIVE}
# Strongest first: the roster is ordered weakest tier -> strongest, and within
# a tier by preference, so reversing it yields "the best available" first.
_STRONGEST_FIRST = [row for tier in reversed(TIER_ORDER)
                    for row in reversed(_LIVE) if row[3] == tier]
# Families that can hold the pre-merge `reviewer` role, and the model each one
# reviews with, STRONGEST FIRST. A taskfile's `reviewer:` names a family here.
REVIEW_FAMILIES = {}
for _m, _fam, _h, _t, _c, _roles in _STRONGEST_FIRST:
    if "reviewer" in _roles and _fam not in REVIEW_FAMILIES:
        REVIEW_FAMILIES[_fam] = _m
PLANNER_MODEL = next((m for m, _f, _h, _t, _c, roles in _STRONGEST_FIRST
                      if "planner" in roles), None)
# Families that may review an OPEN PR. A superset of REVIEW_FAMILIES: DeepSeek
# V4 may judge a bounded diff (pr_reviewer) but not gate or plan (reviewer).
PR_REVIEW_FAMILIES = {fam for _m, fam, _h, _t, _c, roles in _LIVE if "pr_reviewer" in roles}
# Weakest live tier first. Within a tier, the order in ROSTER.
_DEFAULT_PATH = [m for tier in TIER_ORDER for m, _f, _h, t, _c, _r in _LIVE if t == tier]


PR_REVIEWERS = max(1, min(PR_REVIEWERS_WANTED, len(PR_REVIEW_FAMILIES) - 1))


def model_may(model, role):
    """May this model hold this role today? Unknown model -> False."""
    return role in MODEL_ROLES.get(model, set())


def cross_family_reviewer(impl_model):
    """The family token that reviews `impl_model`'s work, or None.

    Cross-review means a DIFFERENT family. Deterministic: the STRONGEST
    review-capable family that is not the implementer's. Before 09-19 that
    pairs kimi<->glm as it always did — DeepSeek 4.1 arriving on 09-12 does not
    demote GLM's reviewer a week early; after 09-19, glm<->deepseek.
    """
    fam = MODEL_FAMILY.get(impl_model)
    for f in REVIEW_FAMILIES:
        if f != fam:
            return f
    return None

ESCALATION_PATH = [m.strip() for m in os.getenv(
    "ARC_ESCALATION_PATH", ",".join(_DEFAULT_PATH)).split(",") if m.strip()]
# An override naming a model that is not live today is a misconfiguration
# that would route work to a withdrawn model; drop those rather than try.
ESCALATION_PATH = [m for m in ESCALATION_PATH if m in IMPLEMENTER_MODELS] or _DEFAULT_PATH
MAX_ESCALATIONS = int(os.getenv("ARC_MAX_ESCALATIONS",
                                str(max(0, len(ESCALATION_PATH) - 1))))

# --- harness context budget -------------------------------------------------
# Both harnesses ship configured for a 131072-token context and only compact
# near that ceiling (kimi: max_context_size - reserved_context_size; opencode:
# limit.context * compaction.threshold). ARC leaves requests unanswered well
# before it — measured 2026-09-09, hangs cluster around 55-60k input tokens —
# so neither harness ever reaches its own compaction point. It simply grows
# context until the server stops replying, and the task dies with it.
#
# The fix is config, not code: fleet-only model aliases declaring a context
# budget the server will actually serve, so the harness compacts in time.
#   ~/.kimi-code/config.toml        [models."arc/<m>-fleet"] max_context_size
#   ~/.config/opencode/opencode.json  ARC.models["<m>-fleet"].limit.context
# Interactive sessions keep the full window: they use the unsuffixed aliases.
USE_FLEET_ALIASES = os.getenv("ARC_USE_FLEET_ALIASES", "1").lower() not in (
    "0", "false", "no", "")
# Context budget the fleet declares to its harnesses — PER HARNESS, because
# the two behave differently when they hit it (measured 2026-09-09):
#
#   opencode  compaction works. Observed firing twice inside one GLM-5.3 run,
#             after which the session carried on to 621KB of output — against
#             ~350KB when it was left at the 131072 default and never
#             compacted at all. A smaller budget is a WIN here: it keeps each
#             request small enough to come back.
#
#   kimi      compaction never completes against this provider. Across the
#             whole session history: 20 `full_compaction.begin`, 0
#             `full_compaction.end`, interactive sessions included. Lowering
#             its budget only makes it reach that dead end sooner — tried,
#             measured, reverted. It stays at the harness default until
#             compaction is fixed upstream.
OPENCODE_CONTEXT = int(os.getenv("ARC_OPENCODE_CONTEXT", "65536"))
KIMI_CONTEXT = int(os.getenv("ARC_KIMI_CONTEXT", "131072"))
KIMI_CONFIG = Path.home() / ".kimi-code" / "config.toml"
OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
OPENCODE_FLEET_CONFIG = OPENCODE_CONFIG.with_name("opencode-fleet.json")
_KIMI_ALIAS = {"Kimi-K3": "arc/kimi-k3-fleet"}


def harness_model(model, harness):
    """The model alias to pass the CLI, or None to use its configured default.

    opencode takes its budget from OPENCODE_CONFIG instead (see drivers): it
    sends the model KEY to the API, so a differently-keyed alias is rejected
    with "Model not found" — verified, not assumed.
    """
    if not USE_FLEET_ALIASES or harness != "kimi":
        return None
    alias = _KIMI_ALIAS.get(model)
    if not alias:
        return None
    # Fall back to the default model rather than failing the run if the alias
    # is missing (a reset or reinstalled kimi config).
    try:
        if f'"{alias}"' not in KIMI_CONFIG.read_text(encoding="utf-8"):
            return None
    except OSError:
        return None
    return alias


def kimi_plan_mode_on():
    """True when kimi-code would start fleet agents in PLAN mode.

    Plan mode makes an agent research and propose instead of edit, and leaving
    it requires approving ExitPlanMode — which nothing does in a headless run.
    Measured on this box before it was found: 182 of 206 sessions entered plan
    mode and only 44 ever left, so most agents produced long transcripts and
    changed no files. This is a global kimi setting, not ours, so the run
    checks it rather than assuming.
    """
    try:
        for line in KIMI_CONFIG.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("default_plan_mode"):
                return line.split("=", 1)[1].strip().lower() == "true"
    except (OSError, IndexError):
        pass
    return False


# Measured 2026-09-10 by ramping concurrent requests until ARC rejected, with
# the fleet's own usage counted in:
#
#     gpt-oss-120b       5 concurrent   (was configured 10 account / 8 drivers)
#     DeepSeek-V4-Flash  5 concurrent   (was configured 10 account / 8 drivers)
#     GLM-5.3            4 concurrent
#     Kimi-K3            3 concurrent
#
# gpt-oss and DeepSeek were OVER-subscribed: 8 drivers against a real ceiling
# of 5, so the fleet generated its own 400s under load and blamed the provider.
# GLM and Kimi were UNDER-subscribed by one slot each.
#
# Driver caps now equal the measured ceiling. ARC_DRIVER_HEADROOM reserves
# slots for interactive use of the same account — set it to 1 if you want to
# run an interactive `kimi` alongside the fleet without contending.

# ONE HARNESS PROCESS IS NOT ONE ARC SESSION.
#
# The numbers above are what ARC allows IN FLIGHT, and they were measured by
# ramping simple prompts — one request at a time per process. Real tasks are
# not like that: an opencode run issues parallel tool calls, so a single
# process holds MORE THAN ONE session at once, and a driver cap set equal to
# the account limit over-subscribes by that factor.
#
# Measured from the event log over four hours (23 capacity rejections):
# GLM-5.3 was refused with as few as TWO of our drivers live, against an ARC
# ceiling of four in flight. Two processes reaching four sessions is two
# sessions per process, so a cap of 4 was really asking for ~8.
#
# GLM was the only model to show it because it is the most-used opencode model
# and the only one whose account limit (4) is small enough for the doubling to
# bite before the harness pool (5) binds first. The factor is a property of the
# HARNESS, not of the model, so it applies to all three opencode models.
_SESSIONS_PER_PROCESS = {"opencode": 2, "kimi": 1}


def _harness_of_model(model):
    return "kimi" if model == "Kimi-K3" else "opencode"


DRIVER_HEADROOM = int(os.getenv("ARC_DRIVER_HEADROOM", "0"))
_MODEL_DRIVER_CAP = {
    m: max(1, n // _SESSIONS_PER_PROCESS[_harness_of_model(m)] - DRIVER_HEADROOM)
    for m, n in _MEASURED_CONCURRENCY.items()}


# The per-MODEL caps above are the ARC API's ceiling. They are not the only
# ceiling: every opencode-backed model shares ONE local harness, and that
# harness serialises through a single ~240MB sqlite db in
# ~/.local/share/opencode. The model caps permit GLM 4 + DeepSeek 5 + gpt-oss 5
# = 14 concurrent opencode processes against it, and measured on this machine
# (identical prompt, warm cache):
#
#     3 concurrent   3/3 ok
#     4 concurrent   4/4 ok
#     5 concurrent   5/5 ok
#     6 concurrent   4/6 ok
#    10 concurrent   4/10 ok
#
# Past 5 it fails fast with an empty stderr, which the fleet logged as
# "opencode exited 1: " and retried four times per task — burning the retry
# ladder on self-inflicted contention and blaming the provider for it. The
# kimi CLI has no shared store, so its limit is just Kimi-K3's own cap.
_HARNESS_CAP = {"opencode": 5, "kimi": _MEASURED_CONCURRENCY.get("Kimi-K3", 3)}


def harness_limit(harness):
    """Max concurrent processes for a HARNESS, across every model it serves."""
    override = os.getenv(f"ARC_HARNESS_LIMIT_{harness.upper()}")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    return _HARNESS_CAP.get(harness, 8)


def driver_limit(model):
    """Max concurrent harness instances for a model (ARC cap minus headroom)."""
    override = os.getenv(f"ARC_DRIVER_LIMIT_{MODEL_FAMILY[model].upper().replace('-', '_')}")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return _MODEL_DRIVER_CAP[model]

# --- graph admission control ------------------------------------------------
# Bounded admission (graph.py max_in_flight) stops a run from starting every
# ready task at once: work started past the real ceilings never ran — it
# queued inside drivers._lease_acquire holding a worktree and a DB row. The
# default is the total harness capacity, which exceeds the root count of every
# taskfile seen so far, so unconfigured runs behave exactly as before.
def max_tasks_in_flight():
    """Max graph nodes executing at once (ARC_MAX_TASKS_IN_FLIGHT)."""
    override = os.getenv("ARC_MAX_TASKS_IN_FLIGHT")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    return sum(harness_limit(h) for h in _HARNESS_CAP)

# --- token cost attribution -------------------------------------------------
# OPERATOR-SUPPLIED estimates, USD per MILLION tokens, for attributing a dollar
# figure to fleet usage. This is not a billing ledger: the numbers are set by
# whoever runs the fleet and should be updated when the provider's published
# rates change. A model with no entry contributes 0.0 rather than guessing, and
# any model may be priced (or re-priced) at runtime via env vars:
#
#     ARC_PRICE_<MODEL>_PROMPT, ARC_PRICE_<MODEL>_COMPLETION
#
# where <MODEL> is the model name uppercased with non-alphanumerics turned to
# "_" (e.g. ARC_PRICE_KIMI_K3_PROMPT). A partial override replaces only the
# half it names, falling back to the table for the other.
#
# prompt_per_mtok and completion_per_mtok are priced separately because
# completion is normally priced higher. Cached-read prompt tokens are charged
# at the full prompt rate here: the telemetry does not distinguish a cache hit
# from a fresh prompt token, so any figure derived from it is an UPPER BOUND,
# not an exact charge.
MODEL_PRICING = {
    # Retired 2026-09-11 but kept here: the event log holds thousands of its
    # runs and the usage page prices history, not just today's roster.
    "gpt-oss-120b": {"prompt_per_mtok": 0.20, "completion_per_mtok": 0.20},
    "DeepSeek-V4-Flash": {"prompt_per_mtok": 0.15, "completion_per_mtok": 0.20},
    # 4.1 replaces V4 on 2026-09-12. Priced the same until the provider says
    # otherwise — an unpriced model reports $0.00, which is worse than an
    # estimate with a note. Override with ARC_PRICE_DEEPSEEK_V4_1_FLASH_PROMPT
    # and ARC_PRICE_DEEPSEEK_V4_1_FLASH_COMPLETION.
    "DeepSeek-V4.1-Flash": {"prompt_per_mtok": 0.15, "completion_per_mtok": 0.20},
    "GLM-5.3": {"prompt_per_mtok": 1.00, "completion_per_mtok": 2.00},
    "Kimi-K3": {"prompt_per_mtok": 0.50, "completion_per_mtok": 2.00},
}


def _env_float(name):
    v = os.getenv(name)
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _price_for(model):
    """(prompt_per_mtok, completion_per_mtok) for a model, or None if unpriced.

    Env overrides take precedence over MODEL_PRICING; a junk override value
    falls back rather than crashing, and a fully-unpriced model is None so
    callers price it at 0.0 instead of inventing a rate.
    """
    key = re.sub(r"[^A-Z0-9]+", "_", (model or "").upper())
    pr = _env_float(f"ARC_PRICE_{key}_PROMPT")
    comp = _env_float(f"ARC_PRICE_{key}_COMPLETION")
    base = MODEL_PRICING.get(model, {})
    if pr is not None or comp is not None:
        return (pr if pr is not None else float(base.get("prompt_per_mtok", 0.0)),
                comp if comp is not None else float(base.get("completion_per_mtok", 0.0)))
    if not base:
        return None
    return (float(base.get("prompt_per_mtok", 0.0)),
            float(base.get("completion_per_mtok", 0.0)))


def cost_of(model, prompt_tokens, completion_tokens):
    """USD cost of a model run at a given token count, 0.0 if the model is unpriced.

    Prices prompt and completion separately (see MODEL_PRICING). A caller that
    only knows a total token figure should pass it as completion_tokens and 0
    prompt_tokens to get an upper bound, since completion is priced at or above
    prompt; the docstring of the caller should say so. Never raises.
    """
    price = _price_for(model)
    if price is None:
        return 0.0
    ppc, cpc = price
    try:
        pt = float(prompt_tokens or 0)
        ct = float(completion_tokens or 0)
    except (TypeError, ValueError):
        # A junk count (None is fine, but a non-numeric string or a bad type
        # must not raise) yields no charge rather than a crash.
        return 0.0
    return (pt / 1e6 * ppc + ct / 1e6 * cpc)


DEFAULT_SEEDS = [
    "Scaling laws and efficiency trade-offs in mixture-of-experts LLM architectures",
    "Post-quantum cryptography migration paths for TLS infrastructure",
    "Grid-scale long-duration battery chemistry and degradation",
    "Federated learning under non-IID data distributions",
    "CRISPR base editing off-target detection methods",
    "Machine-learning interatomic potentials for materials discovery",
    "Wildlife corridor effectiveness under climate-driven range shifts",
    "mRNA vaccine stability in cold-chain-free distribution",
    "Formal verification of unsafe Rust in systems code",
    "Adversarial robustness of vision-language models",
]