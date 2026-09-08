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
MAX_GRAPH_STEPS = int(os.getenv("ARC_MAX_GRAPH_STEPS", "200"))
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