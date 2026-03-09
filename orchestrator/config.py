"""
config.py — Load environment variables and build routing tables.

All other modules import from here. Nothing reads os.environ directly.

Phase 4A.2: ROLE_ENDPOINTS and ROLE_MODELS removed. Per-session model
resolution now lives in routing_policy.py (RoutingPolicy). Profile URL
constants and model name constants are kept — RoutingPolicy uses them.

Phase 4B.1: SESSION_TTL added (7-day canonical session lifetime).
SESSION_MODELS_TTL is deprecated — SessionManager now uses SESSION_TTL
for both session:state and session:models keys so they never diverge.
SESSION_MODELS_TTL kept temporarily so /v1/session/configure callers that
read it from config don't break before Phase 5 (Postgres) removes it.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

# ── Profile ───────────────────────────────────────────────────────────────────

def _detect_profile() -> str:
    """
    Auto-detect hardware profile if PROFILE=auto.
    Checks for NVIDIA GPU via nvidia-smi, falls back to laptop.
    Decision is logged at WARNING level so it is always visible in
    orchestrator startup logs regardless of log level.
    """
    import subprocess
    _log = logging.getLogger("config")

    requested = os.environ.get("PROFILE", "laptop")
    if requested != "auto":
        return requested

    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            timeout=5, stderr=subprocess.DEVNULL,
        )
        vrams         = [int(v.strip()) for v in out.decode().strip().splitlines() if v.strip()]
        total_vram_mb = sum(vrams)
        gpu_count     = len(vrams)
        if gpu_count >= 3 and total_vram_mb >= 120_000:
            profile = "gpu"
        elif gpu_count >= 1 and total_vram_mb >= 20_000:
            profile = "gpu-shared"
        else:
            profile = "laptop"
        _log.warning(
            "PROFILE=auto detected %d GPU(s), %dMB total VRAM → selected profile: %s",
            gpu_count, total_vram_mb, profile,
        )
    except Exception as e:
        profile = "laptop"
        _log.warning(
            "PROFILE=auto: nvidia-smi unavailable (%s) → falling back to profile: laptop", e,
        )
    return profile


PROFILE = _detect_profile()

# ── Model server URLs ─────────────────────────────────────────────────────────
OLLAMA_URL     = os.environ.get("OLLAMA_URL",     "http://ollama:11434")
CODER_URL      = os.environ.get("CODER_URL",      "http://vllm-coder:8000/v1")
ARCHITECT_URL  = os.environ.get("ARCHITECT_URL",  "http://vllm-architect:8000/v1")
REVIEWER_URL   = os.environ.get("REVIEWER_URL",   "http://vllm-reviewer:8000/v1")

# ── Model names ───────────────────────────────────────────────────────────────
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL",    "qwen2.5-coder:7b")
CODER_MODEL     = os.environ.get("CODER_MODEL",     "Qwen/Qwen3-Coder-Next-80B-A3B-Instruct")
ARCHITECT_MODEL = os.environ.get("ARCHITECT_MODEL", "Qwen/Qwen3.5-35B-A3B")
REVIEWER_MODEL  = os.environ.get("REVIEWER_MODEL",  "Qwen/QwQ-32B")
SHARED_MODEL    = os.environ.get("SHARED_MODEL",    "Qwen/Qwen3-Coder-Next-80B-A3B-Instruct")

# Phase 4A.2: ROLE_ENDPOINTS and ROLE_MODELS removed.
# Per-session model resolution lives in routing_policy.RoutingPolicy.

# Fallback chain for health-aware routing (used by router._is_healthy fallback)
FALLBACK_ORDER = [CODER_URL, ARCHITECT_URL, REVIEWER_URL, OLLAMA_URL]

# ── Orchestrator config ───────────────────────────────────────────────────────
MAX_CONTEXT_TOKENS  = int(os.environ.get("MAX_CONTEXT_TOKENS",  "24000"))
MAX_DEBATE_ROUNDS   = int(os.environ.get("MAX_DEBATE_ROUNDS",   "3"))
MAX_AGENT_RUNTIME   = int(os.environ.get("MAX_AGENT_RUNTIME",   "300"))

# ── Infrastructure URLs ───────────────────────────────────────────────────────
REDIS_URL    = os.environ.get("REDIS_URL",    "redis://redis:6379")
CHROMA_URL   = os.environ.get("CHROMA_URL",   "http://chromadb:8000")
EXECUTOR_URL = os.environ.get("EXECUTOR_URL", "http://executor:9001")

# ── Embedding ─────────────────────────────────────────────────────────────────
EMBED_MODEL = os.environ.get("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")

# ── Phase 2 ───────────────────────────────────────────────────────────────────
MAX_FIX_ATTEMPTS    = int(os.environ.get("MAX_FIX_ATTEMPTS",    "3"))
MAX_PARALLEL_AGENTS = int(os.environ.get("MAX_PARALLEL_AGENTS", "3"))

# ── Phase 3.3: GitHub webhook ─────────────────────────────────────────────────
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN          = os.environ.get("GITHUB_TOKEN",          "")
GITHUB_REPO           = os.environ.get("GITHUB_REPO",           "")

# ── Phase 3.4: Failure pattern learning ──────────────────────────────────────
N_FAILURES_THRESHOLD = int(os.environ.get("N_FAILURES_THRESHOLD", "3"))

# ── Phase 3.2: Fine-tune data collection ─────────────────────────────────────
TRAINING_DATA_PATH = os.environ.get("TRAINING_DATA_PATH", "/app/memory/training_data.jsonl")

# ── Phase 3.5: Stability & correctness ───────────────────────────────────────
MAX_AGENT_HISTORY        = int(os.environ.get("MAX_AGENT_HISTORY",        "20"))
AGENT_IDLE_TIMEOUT       = int(os.environ.get("AGENT_IDLE_TIMEOUT",       "3600"))
AGENTS_DIR               = os.environ.get("AGENTS_DIR",               "/app/agents")
MAX_PATCH_QUEUE_DEPTH    = int(os.environ.get("MAX_PATCH_QUEUE_DEPTH",    "50"))
EMBED_CACHE_MAX_SIZE     = int(os.environ.get("EMBED_CACHE_MAX_SIZE",     "1000"))
MAX_EXECUTOR_CONCURRENCY = int(os.environ.get("MAX_EXECUTOR_CONCURRENCY", "2"))
MODEL_CALL_TIMEOUT       = int(os.environ.get("MODEL_CALL_TIMEOUT",       "120"))

# ── Phase 4A.2: Dynamic model assignment ─────────────────────────────────────
# DEPRECATED: SESSION_MODELS_TTL is superseded by SESSION_TTL (Phase 4B.1).
# Both session:state and session:models now share SESSION_TTL so they never
# diverge. SESSION_MODELS_TTL is kept here to avoid breaking any callers that
# read it directly; it will be removed when Phase 5 (Postgres) lands.
SESSION_MODELS_TTL   = int(os.environ.get("SESSION_MODELS_TTL",   "86400"))   # DEPRECATED
# TTL for task lease keys — prevents duplicate execution on restart (10 min)
TASK_LEASE_TTL       = int(os.environ.get("TASK_LEASE_TTL",       "600"))

# ── Phase 4A.4: LiteLLM gateway (optional, flag-gated) ───────────────────────
USE_LITELLM = os.environ.get("USE_LITELLM", "false").lower() == "true"

# ── Phase 4B.1: Session lifecycle ─────────────────────────────────────────────
# Canonical session TTL — used for BOTH session:state and session:models keys.
# 7 days: long enough for a multi-day coding project, short enough to not
# accumulate stale state in Redis forever.
SESSION_TTL = int(os.environ.get("SESSION_TTL", str(7 * 24 * 3600)))   # 604800s

# ── Phase 4B.2: Streaming ─────────────────────────────────────────────────────
# WebSocket keepalive ping interval (seconds). Prevents proxy/load-balancer
# idle connection timeouts during long agent runs.
WS_HEARTBEAT_INTERVAL = int(os.environ.get("WS_HEARTBEAT_INTERVAL", "30"))

# ── Phase 4B.3: Agent bus ─────────────────────────────────────────────────────
# How long structured bus events linger in Redis pub/sub history.
# Not a hard expiry — Redis pub/sub is fire-and-forget; this is for any
# supplementary LIST-based event log added in a future phase.
BUS_EVENT_TTL = int(os.environ.get("BUS_EVENT_TTL", "3600"))

# ── Phase 4B.4: TUI ───────────────────────────────────────────────────────────
# Default orchestrator URL for the TUI client. Override with AICAF_URL env var
# to point the TUI at a remote GPU server.
AICAF_URL = os.environ.get("AICAF_URL", "http://localhost:9000")

# ── Roles ─────────────────────────────────────────────────────────────────────
# Canonical list used by model_registry, routing_policy, and TUI
ALL_ROLES: list[str] = ["architect", "coder", "reviewer", "tester", "documenter"]