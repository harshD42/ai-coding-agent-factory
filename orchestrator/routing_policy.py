"""
routing_policy.py — Per-session model resolution for the router.

Phase 4A.2:
  Extracts all endpoint/model resolution logic out of router.py into this
  dedicated class. router.py becomes a thin dispatcher that calls
  RoutingPolicy.resolve() — it owns no selection logic itself.

  Resolution order:
    1. Redis session override  → session:models:{session_id} HGET role
    2. Profile defaults        → same logic previously in config.py ROLE_ENDPOINTS/ROLE_MODELS

  Only this module reads session:models:{session_id}. All other Redis
  keys are defined in the canonical key schema (see build plan / README).
"""

import logging
from typing import Optional

import redis.asyncio as aioredis

import config

log = logging.getLogger("routing_policy")

# ── Backend type detection ────────────────────────────────────────────────────

def _backend_type(url: str) -> str:
    """Return 'ollama' or 'vllm' based on URL heuristic."""
    if "11434" in url or "ollama" in url.lower():
        return "ollama"
    return "vllm"


def _endpoint_for_model(model_name: str) -> str:
    """
    Map a model name to its service URL using config URL constants.
    Called when a session override is set and we need to route to the
    correct backend for the chosen model.
    """
    from model_registry import MODEL_CATALOG
    entry = MODEL_CATALOG.get(model_name, {})
    backends = entry.get("backend", [])

    if "ollama" in backends:
        return config.OLLAMA_URL

    # vLLM models — map by which vLLM service carries this model
    # gpu-shared profile: single endpoint for everything
    if config.PROFILE == "gpu-shared":
        return config.CODER_URL  # gpu-shared uses CODER_URL as its single vLLM endpoint

    # gpu profile: match by model name
    if model_name == config.ARCHITECT_MODEL:
        return config.ARCHITECT_URL
    if model_name == config.REVIEWER_MODEL:
        return config.REVIEWER_URL
    # coder, tester, documenter → coder endpoint
    return config.CODER_URL


# ── Profile defaults (replaces config.py ROLE_ENDPOINTS / ROLE_MODELS) ───────

def _profile_endpoint(role: str) -> str:
    """Profile-default endpoint for a role. Mirrors old config.ROLE_ENDPOINTS logic."""
    if config.PROFILE == "laptop":
        return config.OLLAMA_URL
    elif config.PROFILE == "gpu-shared":
        return config.CODER_URL
    else:  # gpu
        mapping = {
            "architect":  config.ARCHITECT_URL,
            "coder":      config.CODER_URL,
            "reviewer":   config.REVIEWER_URL,
            "tester":     config.CODER_URL,
            "documenter": config.ARCHITECT_URL,
        }
        return mapping.get(role, config.CODER_URL)


def _profile_model(role: str) -> str:
    """Profile-default model for a role. Mirrors old config.ROLE_MODELS logic."""
    if config.PROFILE == "laptop":
        return config.OLLAMA_MODEL
    elif config.PROFILE == "gpu-shared":
        shared = getattr(config, "SHARED_MODEL", config.CODER_MODEL)
        return shared
    else:  # gpu
        mapping = {
            "architect":  config.ARCHITECT_MODEL,
            "coder":      config.CODER_MODEL,
            "reviewer":   config.REVIEWER_MODEL,
            "tester":     config.CODER_MODEL,
            "documenter": config.ARCHITECT_MODEL,
        }
        return mapping.get(role, config.CODER_MODEL)


# ── RoutingPolicy ─────────────────────────────────────────────────────────────

class RoutingPolicy:
    """
    Resolves (endpoint_url, model_name, backend_type) for a given role
    and session. The router calls this; it owns no HTTP logic.

    Injected with a Redis client at startup. If redis is None (tests or
    early startup), falls back to profile defaults immediately.
    """

    def __init__(self, redis: Optional[aioredis.Redis] = None) -> None:
        self._redis = redis

    def set_redis(self, redis: aioredis.Redis) -> None:
        """Wire in Redis after construction (same pattern as PatchQueue)."""
        self._redis = redis

    async def resolve(
        self, role: str, session_id: str = "default"
    ) -> tuple[str, str, str]:
        """
        Return (endpoint_url, model_name, backend_type).

        1. Check Redis for a session-specific model override.
        2. Fall back to profile defaults if none found or Redis unavailable.
        """
        if self._redis and session_id and session_id != "default":
            try:
                model_name = await self._redis.hget(
                    f"session:models:{session_id}", role
                )
                if model_name:
                    endpoint = _endpoint_for_model(model_name)
                    btype    = _backend_type(endpoint)
                    log.debug(
                        "session override: session=%s role=%s model=%s endpoint=%s",
                        session_id, role, model_name, endpoint,
                    )
                    return endpoint, model_name, btype
            except Exception as e:
                log.warning(
                    "Redis lookup failed for session=%s role=%s, "
                    "falling back to profile defaults: %s",
                    session_id, role, e,
                )

        # Profile defaults
        endpoint   = _profile_endpoint(role)
        model_name = _profile_model(role)
        btype      = _backend_type(endpoint)
        log.debug(
            "profile default: profile=%s role=%s model=%s endpoint=%s",
            config.PROFILE, role, model_name, endpoint,
        )
        return endpoint, model_name, btype

    async def get_session_models(self, session_id: str) -> dict[str, str]:
        """
        Return the full role→model map stored for a session.
        Returns empty dict if no overrides are set or Redis unavailable.
        """
        if not self._redis:
            return {}
        try:
            return await self._redis.hgetall(f"session:models:{session_id}") or {}
        except Exception as e:
            log.warning("get_session_models failed for %s: %s", session_id, e)
            return {}


# ── Singleton ─────────────────────────────────────────────────────────────────

_policy: Optional[RoutingPolicy] = None


def get_routing_policy() -> RoutingPolicy:
    if _policy is None:
        raise RuntimeError("RoutingPolicy not initialised — call init_routing_policy() first")
    return _policy


def init_routing_policy(redis: Optional[aioredis.Redis] = None) -> RoutingPolicy:
    global _policy
    _policy = RoutingPolicy(redis)
    log.info("RoutingPolicy initialised  profile=%s", config.PROFILE)
    return _policy