"""
config.py — Load environment variables and build routing tables.

All other modules import from here. Nothing reads os.environ directly.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Profile ───────────────────────────────────────────────────────────────────
PROFILE = os.environ.get("PROFILE", "laptop")

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

# ── Role → endpoint mapping ───────────────────────────────────────────────────
if PROFILE == "laptop":
    ROLE_ENDPOINTS: dict[str, str] = {
        "architect":  OLLAMA_URL,
        "coder":      OLLAMA_URL,
        "reviewer":   OLLAMA_URL,
        "tester":     OLLAMA_URL,
        "documenter": OLLAMA_URL,
    }
    ROLE_MODELS: dict[str, str] = {r: OLLAMA_MODEL for r in ROLE_ENDPOINTS}
elif PROFILE == "gpu-shared":
    shared       = os.environ.get("CODER_URL", "http://vllm-shared:8000/v1")
    shared_model = os.environ.get("SHARED_MODEL", "Qwen/Qwen3-Coder-Next-80B-A3B-Instruct")
    ROLE_ENDPOINTS = {r: shared for r in ("architect","coder","reviewer","tester","documenter")}
    ROLE_MODELS    = {r: shared_model for r in ROLE_ENDPOINTS}
else:  # gpu
    ROLE_ENDPOINTS = {
        "architect":  ARCHITECT_URL,
        "coder":      CODER_URL,
        "reviewer":   REVIEWER_URL,
        "tester":     CODER_URL,
        "documenter": ARCHITECT_URL,
    }
    ROLE_MODELS = {
        "architect":  ARCHITECT_MODEL,
        "coder":      CODER_MODEL,
        "reviewer":   REVIEWER_MODEL,
        "tester":     CODER_MODEL,
        "documenter": ARCHITECT_MODEL,
    }

FALLBACK_ORDER = [CODER_URL, ARCHITECT_URL, REVIEWER_URL, OLLAMA_URL]

# ── Orchestrator config ───────────────────────────────────────────────────────
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", "24000"))
MAX_DEBATE_ROUNDS  = int(os.environ.get("MAX_DEBATE_ROUNDS",  "3"))
MAX_AGENT_RUNTIME  = int(os.environ.get("MAX_AGENT_RUNTIME",  "300"))

# ── Infrastructure URLs ───────────────────────────────────────────────────────
REDIS_URL    = os.environ.get("REDIS_URL",    "redis://redis:6379")
CHROMA_URL   = os.environ.get("CHROMA_URL",   "http://chromadb:8000")
EXECUTOR_URL = os.environ.get("EXECUTOR_URL", "http://executor:9001")

# ── Embedding ─────────────────────────────────────────────────────────────────
EMBED_MODEL = os.environ.get("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")

# ── Step 2.1: Auto-patch fix-loop limit ──────────────────────────────────────
MAX_FIX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "3"))

# ── Step 2.6: Parallel agent cap ─────────────────────────────────────────────
MAX_PARALLEL_AGENTS = int(os.environ.get("MAX_PARALLEL_AGENTS", "3"))