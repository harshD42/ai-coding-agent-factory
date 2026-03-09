"""
model_registry.py — Model catalog, on-disk detection, and capability filtering.

Phase 4A.1 additions:
  - MODEL_CATALOG: known models with role-affinity tags, context_length,
    vram_approx_gb (indicative only), and supported backends
  - ROLE_TAG_MAP: which tags qualify a model for a given agent role
  - ModelRegistry: runtime availability detection, role filtering, pull-on-demand
  - get_context_length(): used by context_manager to set per-model token budgets
  - detect_available(): queries live Ollama/vLLM endpoints at startup

Design notes:
  - VRAM numbers are approximate — actual usage depends on quantization,
    tensor parallelism, and KV cache size
  - Tags are role-affinity hints, not objective capability claims
  - Pull is non-streaming via REST; TUI streaming pull added in Phase 4B.4
  - detect_available() is called once at startup; results cached in _on_disk set
  - Registry never touches routing — that is RoutingPolicy's job (Phase 4A.2)
"""

import asyncio
import logging
from typing import Optional

import httpx

import config

log = logging.getLogger("model_registry")

DEFAULT_CONTEXT_LENGTH = 32768   # fallback if model not in catalog

# ── Model catalog ─────────────────────────────────────────────────────────────
# vram_approx_gb is indicative only. Real usage varies by quant/TP/KV config.
# context_length is authoritative — taken from model cards / vLLM launch flags.
# tags are role-affinity hints used by get_models_for_role(); not capability claims.

MODEL_CATALOG: dict[str, dict] = {
    # ── Ollama / laptop ───────────────────────────────────────────────────────
    "qwen2.5-coder:7b": {
        "tags":           ["coder", "tester"],
        "context_length": 32768,
        "vram_approx_gb": 6,
        "backend":        ["ollama"],
        "display_name":   "Qwen2.5-Coder 7B",
    },
    "qwen2.5-coder:32b": {
        "tags":           ["coder", "tester"],
        "context_length": 32768,
        "vram_approx_gb": 20,
        "backend":        ["ollama"],
        "display_name":   "Qwen2.5-Coder 32B",
    },
    "qwen3:8b": {
        "tags":           ["architect", "coder", "documenter"],
        "context_length": 32768,
        "vram_approx_gb": 6,
        "backend":        ["ollama"],
        "display_name":   "Qwen3 8B",
    },
    "qwen3:14b": {
        "tags":           ["architect", "reviewer", "documenter"],
        "context_length": 32768,
        "vram_approx_gb": 10,
        "backend":        ["ollama"],
        "display_name":   "Qwen3 14B",
    },
    "qwen3:32b": {
        "tags":           ["architect", "reviewer", "documenter"],
        "context_length": 32768,
        "vram_approx_gb": 20,
        "backend":        ["ollama"],
        "display_name":   "Qwen3 32B",
    },
    "nomic-embed-text": {
        "tags":           ["embedding"],
        "context_length": 8192,
        "vram_approx_gb": 1,
        "backend":        ["ollama"],
        "display_name":   "Nomic Embed Text",
    },

    # ── vLLM / GPU ────────────────────────────────────────────────────────────
    "Qwen/Qwen3-Coder-Next-80B-A3B-Instruct": {
        "tags":           ["coder", "tester"],
        "context_length": 32768,
        "vram_approx_gb": 46,
        "backend":        ["vllm"],
        "display_name":   "Qwen3-Coder-Next 80B",
    },
    "Qwen/Qwen3.5-35B-A3B": {
        "tags":           ["architect", "documenter"],
        "context_length": 65536,
        "vram_approx_gb": 20,
        "backend":        ["vllm"],
        "display_name":   "Qwen3.5 35B",
    },
    "Qwen/QwQ-32B": {
        "tags":           ["reviewer"],
        "context_length": 32768,
        "vram_approx_gb": 20,
        "backend":        ["vllm"],
        "display_name":   "QwQ 32B",
    },
    "Qwen/Qwen3-Embedding-0.6B": {
        "tags":           ["embedding"],
        "context_length": 8192,
        "vram_approx_gb": 1,
        "backend":        ["vllm"],
        "display_name":   "Qwen3 Embedding 0.6B",
    },
}

# ── Role → tag affinity ───────────────────────────────────────────────────────
# A model qualifies for a role if any of its tags appear in this map's list.
# "tester" accepts coder-tagged models because coding ability covers test writing.

ROLE_TAG_MAP: dict[str, list[str]] = {
    "architect":  ["architect"],
    "coder":      ["coder"],
    "reviewer":   ["reviewer"],
    "tester":     ["tester", "coder"],
    "documenter": ["documenter", "architect"],
}

ALL_ROLES: list[str] = list(ROLE_TAG_MAP.keys())


# ── ModelRegistry ─────────────────────────────────────────────────────────────

class ModelRegistry:
    """
    Runtime model registry.

    Responsibilities:
      - Detect which models are actually on disk / loaded in each backend
      - Filter catalog by role affinity for TUI role selectors
      - Provide authoritative context_length per model for context_manager
      - Pull models on demand (startup or explicit user request only)

    Does NOT handle routing. RoutingPolicy (Phase 4A.2) owns endpoint resolution.
    """

    def __init__(self) -> None:
        self._client:  httpx.AsyncClient = httpx.AsyncClient(timeout=10.0)
        self._on_disk: set[str]          = set()   # populated by detect_available()
        self._available: dict[str, list[str]] = {}  # backend → [model_names]

    # ── Detection ─────────────────────────────────────────────────────────────

    async def detect_available(self) -> dict[str, list[str]]:
        """
        Query live endpoints to discover which models are loaded.
        Populates self._on_disk and self._available.
        Called once at orchestrator startup; safe to call again for refresh.

        Returns {backend_name: [model_name, ...]}
        Endpoint failures are logged as warnings, never crash startup.
        """
        results: dict[str, list[str]] = {}

        ollama_models = await self._detect_ollama()
        if ollama_models is not None:
            results["ollama"] = ollama_models

        vllm_endpoints = {
            "vllm-coder":      config.CODER_URL,
            "vllm-architect":  config.ARCHITECT_URL,
            "vllm-reviewer":   config.REVIEWER_URL,
        }
        for label, url in vllm_endpoints.items():
            models = await self._detect_vllm(url)
            if models is not None:
                results.setdefault("vllm", []).extend(models)

        self._available = results
        self._on_disk   = {m for models in results.values() for m in models}

        log.info(
            "model detection complete — %d model(s) available across %d backend(s): %s",
            len(self._on_disk), len(results),
            {k: len(v) for k, v in results.items()},
        )
        return results

    async def _detect_ollama(self) -> Optional[list[str]]:
        """Query Ollama /api/tags. Returns list of model names or None on failure."""
        try:
            r = await self._client.get(
                f"{config.OLLAMA_URL.rstrip('/')}/api/tags", timeout=5.0
            )
            r.raise_for_status()
            data   = r.json()
            models = [m["name"] for m in data.get("models", [])]
            log.info("ollama: %d model(s) detected", len(models))
            return models
        except Exception as e:
            log.warning("ollama detection failed (%s) — no Ollama models available", e)
            return None

    async def _detect_vllm(self, base_url: str) -> Optional[list[str]]:
        """Query vLLM /v1/models. Returns list of model names or None on failure."""
        try:
            url = f"{base_url.rstrip('/')}/models"
            r   = await self._client.get(url, timeout=5.0)
            r.raise_for_status()
            data   = r.json()
            models = [m["id"] for m in data.get("data", [])]
            log.info("vllm (%s): %d model(s) detected", base_url, len(models))
            return models
        except Exception as e:
            log.warning("vllm detection failed for %s (%s)", base_url, e)
            return None

    # ── Catalog queries ───────────────────────────────────────────────────────

    def get_models_for_role(self, role: str) -> list[dict]:
        """
        Return catalog entries for models that:
          1. Have a tag matching ROLE_TAG_MAP[role]
          2. Were found by the most recent detect_available() call

        Returned list includes on_disk status and full catalog metadata.
        Result is sorted: on-disk models first, then by display_name.
        """
        required_tags = ROLE_TAG_MAP.get(role, [])
        if not required_tags:
            log.warning("unknown role requested: %s", role)
            return []

        matches = []
        for model_name, meta in MODEL_CATALOG.items():
            if meta.get("tags") and any(t in required_tags for t in meta["tags"]):
                on_disk = model_name in self._on_disk
                matches.append({
                    "name":           model_name,
                    "display_name":   meta.get("display_name", model_name),
                    "tags":           meta["tags"],
                    "context_length": meta["context_length"],
                    "vram_approx_gb": meta["vram_approx_gb"],
                    "backend":        meta["backend"],
                    "on_disk":        on_disk,
                })

        matches.sort(key=lambda m: (not m["on_disk"], m["display_name"]))
        return matches

    def get_context_length(self, model_name: str) -> int:
        """
        Return context_length for model_name.
        Used by context_manager.build_prompt() to set per-agent token budget.
        Falls back to DEFAULT_CONTEXT_LENGTH if model is not in catalog.
        """
        entry = MODEL_CATALOG.get(model_name)
        if entry:
            return entry["context_length"]
        log.warning(
            "model %r not in catalog — using default context length %d",
            model_name, DEFAULT_CONTEXT_LENGTH,
        )
        return DEFAULT_CONTEXT_LENGTH

    def is_on_disk(self, model_name: str) -> bool:
        """True if the most recent detect_available() found this model."""
        return model_name in self._on_disk

    def catalog_with_status(self) -> list[dict]:
        """
        Full catalog annotated with on_disk and available flags.
        Used by GET /v1/models/catalog.
        """
        result = []
        for name, meta in MODEL_CATALOG.items():
            result.append({
                "name":           name,
                "display_name":   meta.get("display_name", name),
                "tags":           meta["tags"],
                "context_length": meta["context_length"],
                "vram_approx_gb": meta["vram_approx_gb"],
                "backend":        meta["backend"],
                "on_disk":        name in self._on_disk,
            })
        # Sort: on-disk first, then by display name
        result.sort(key=lambda m: (not m["on_disk"], m["display_name"]))
        return result

    # ── Pull ──────────────────────────────────────────────────────────────────

    async def pull_model(self, name: str) -> dict:
        """
        Pull a model via Ollama (non-streaming).
        Only valid for Ollama-backend models.
        TUI streaming pull progress is handled in Phase 4B.4.

        Raises ValueError if model is not in catalog or not an Ollama model.
        Raises RuntimeError if pull fails.
        """
        entry = MODEL_CATALOG.get(name)
        if not entry:
            raise ValueError(f"Model {name!r} is not in the catalog")
        if "ollama" not in entry.get("backend", []):
            raise ValueError(
                f"Model {name!r} uses backend {entry['backend']} — "
                "only Ollama models can be pulled via this endpoint"
            )

        log.info("pulling model %r via Ollama...", name)
        try:
            resp = await self._client.post(
                f"{config.OLLAMA_URL.rstrip('/')}/api/pull",
                json={"name": name},
                timeout=600.0,   # large models take time
            )
            resp.raise_for_status()
            log.info("model %r pulled successfully", name)
            # Refresh on-disk cache after pull
            await self.detect_available()
            return {"pulled": True, "name": name}
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Ollama pull failed for {name!r}: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Pull error for {name!r}: {e}") from e

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._client.aclose()


# ── Singleton ─────────────────────────────────────────────────────────────────

_registry: Optional[ModelRegistry] = None


def get_model_registry() -> ModelRegistry:
    if _registry is None:
        raise RuntimeError("ModelRegistry not initialised — call init_model_registry() first")
    return _registry


def init_model_registry() -> ModelRegistry:
    global _registry
    _registry = ModelRegistry()
    log.info("ModelRegistry initialised with %d catalog entries", len(MODEL_CATALOG))
    return _registry