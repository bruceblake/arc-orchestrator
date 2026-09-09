import os
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
        10,
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
        10,
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
MAX_RETRIES = int(os.getenv("ARC_MAX_RETRIES", "4"))
ROUND_COOLDOWN = float(os.getenv("ARC_ROUND_COOLDOWN", "5"))
STATS_INTERVAL = float(os.getenv("ARC_STATS_INTERVAL", "300"))
MAX_GRAPH_STEPS = int(os.getenv("ARC_MAX_GRAPH_STEPS", "1500"))
SESSION_RETRIES = int(os.getenv("ARC_SESSION_RETRIES", "12"))
SESSION_BACKOFF_CAP = float(os.getenv("ARC_SESSION_BACKOFF_CAP", "30"))

EVENTS_LOG = os.getenv("ARC_EVENTS_LOG") or str(ROOT / "logs" / "events.jsonl")
BUILD_OUTPUT_DIR = os.getenv("ARC_BUILD_OUTPUT_DIR") or str(ROOT / "production" / "minecraft")
DASHBOARD_PORT = int(os.getenv("ARC_DASHBOARD_PORT", "8787"))
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
# A harness that stops producing stdout for this long has a hung API request;
# kill and retry instead of waiting out the full DRIVER_TIMEOUT.
#
# Confirmed rather than inferred, 2026-09-09: at the moment of a stall the
# harness sits in state 'S' burning no CPU, and its kimi session log ends on an
# llm.request that ARC had left unanswered for 320s. Nothing arrives after the
# hang, so waiting is pure cost — lowered 300 -> 120. drivers._pump records
# that evidence on every driver.stalled event, so shortening the wait does not
# cost us the diagnosis.
DRIVER_IDLE_TIMEOUT = float(os.getenv("ARC_DRIVER_IDLE_TIMEOUT", "120"))
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
GATE_TIMEOUT = float(os.getenv("ARC_GATE_TIMEOUT", "180"))
MAX_FIX_ROUNDS = int(os.getenv("ARC_MAX_FIX_ROUNDS", "3"))

# Model escalation (code workload): when a task exhausts its fix rounds at its
# current tier, it retries one tier stronger with a fresh fix budget instead of
# failing; it only fails when the last tier exhausts. gpt-oss-120b may
# legitimately exhaust immediately on a task planned too optimistically, so the
# path still reaches a strong model within a couple of escalations.
ESCALATION_PATH = [m.strip() for m in os.getenv(
    "ARC_ESCALATION_PATH",
    "gpt-oss-120b,DeepSeek-V4-Flash,GLM-5.3,Kimi-K3").split(",") if m.strip()]
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
# Context budget the fleet declares to its harnesses. Below the range where
# ARC starts leaving requests unanswered, so compaction fires in time.
HARNESS_CONTEXT = int(os.getenv("ARC_HARNESS_CONTEXT", "65536"))
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


IMPLEMENTER_MODELS = {"gpt-oss-120b", "DeepSeek-V4-Flash", "GLM-5.3", "Kimi-K3"}
IMPLEMENT_TIERS = {"basic": ["gpt-oss-120b"], "medium": ["DeepSeek-V4-Flash"],
                   "hard": ["GLM-5.3", "Kimi-K3"]}
MODEL_FAMILY = {
    "gpt-oss-120b": "gpt-oss",
    "DeepSeek-V4-Flash": "deepseek",
    "GLM-5.3": "glm",
    "Kimi-K3": "kimi",
}
_MODEL_DRIVER_CAP = {"Kimi-K3": 2, "GLM-5.3": 3, "gpt-oss-120b": 8, "DeepSeek-V4-Flash": 8}


def driver_limit(model):
    """Max concurrent harness instances for a model (ARC cap minus headroom)."""
    override = os.getenv(f"ARC_DRIVER_LIMIT_{MODEL_FAMILY[model].upper().replace('-', '_')}")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return _MODEL_DRIVER_CAP[model]

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