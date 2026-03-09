"""
tests/unit/test_routing_policy.py — Unit tests for Phase 4A.2 routing policy.

Covers:
  - Profile default resolution (laptop / gpu-shared / gpu)
  - Session Redis override resolution
  - Redis failure graceful fallback to profile defaults
  - _endpoint_for_model() mapping
  - _backend_type() detection
  - Singleton lifecycle
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import config


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_policy():
    import routing_policy as rp
    original = getattr(rp, "_policy", None)
    rp._policy = None
    yield
    rp._policy = original


@pytest.fixture
def policy():
    from routing_policy import init_routing_policy
    return init_routing_policy(redis=None)


@pytest.fixture
def mock_redis():
    r = MagicMock()
    r.hget = AsyncMock(return_value=None)
    r.hgetall = AsyncMock(return_value={})
    return r


@pytest.fixture
def policy_with_redis(mock_redis):
    from routing_policy import init_routing_policy
    return init_routing_policy(redis=mock_redis)


# ── _backend_type ─────────────────────────────────────────────────────────────

def test_backend_type_ollama_by_port():
    from routing_policy import _backend_type
    assert _backend_type("http://ollama:11434") == "ollama"


def test_backend_type_ollama_by_name():
    from routing_policy import _backend_type
    assert _backend_type("http://localhost:11434") == "ollama"


def test_backend_type_vllm():
    from routing_policy import _backend_type
    assert _backend_type("http://vllm-coder:8000/v1") == "vllm"


def test_backend_type_case_insensitive():
    from routing_policy import _backend_type
    assert _backend_type("http://Ollama:11434") == "ollama"


# ── _profile_endpoint and _profile_model ─────────────────────────────────────

def test_profile_endpoint_laptop_all_roles_use_ollama():
    from routing_policy import _profile_endpoint
    with patch.object(config, "PROFILE", "laptop"):
        for role in ["architect", "coder", "reviewer", "tester", "documenter"]:
            assert _profile_endpoint(role) == config.OLLAMA_URL


def test_profile_endpoint_gpu_shared_all_roles_use_coder_url():
    from routing_policy import _profile_endpoint
    with patch.object(config, "PROFILE", "gpu-shared"):
        for role in ["architect", "coder", "reviewer", "tester", "documenter"]:
            assert _profile_endpoint(role) == config.CODER_URL


def test_profile_endpoint_gpu_architect_uses_architect_url():
    from routing_policy import _profile_endpoint
    with patch.object(config, "PROFILE", "gpu"):
        assert _profile_endpoint("architect") == config.ARCHITECT_URL


def test_profile_endpoint_gpu_reviewer_uses_reviewer_url():
    from routing_policy import _profile_endpoint
    with patch.object(config, "PROFILE", "gpu"):
        assert _profile_endpoint("reviewer") == config.REVIEWER_URL


def test_profile_endpoint_gpu_coder_uses_coder_url():
    from routing_policy import _profile_endpoint
    with patch.object(config, "PROFILE", "gpu"):
        assert _profile_endpoint("coder") == config.CODER_URL


def test_profile_endpoint_gpu_tester_uses_coder_url():
    from routing_policy import _profile_endpoint
    with patch.object(config, "PROFILE", "gpu"):
        assert _profile_endpoint("tester") == config.CODER_URL


def test_profile_model_laptop_returns_ollama_model():
    from routing_policy import _profile_model
    with patch.object(config, "PROFILE", "laptop"):
        assert _profile_model("architect") == config.OLLAMA_MODEL
        assert _profile_model("coder")     == config.OLLAMA_MODEL


def test_profile_model_gpu_architect_returns_architect_model():
    from routing_policy import _profile_model
    with patch.object(config, "PROFILE", "gpu"):
        assert _profile_model("architect") == config.ARCHITECT_MODEL


def test_profile_model_gpu_reviewer_returns_reviewer_model():
    from routing_policy import _profile_model
    with patch.object(config, "PROFILE", "gpu"):
        assert _profile_model("reviewer") == config.REVIEWER_MODEL


# ── resolve() — no Redis ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_no_redis_returns_profile_defaults(policy):
    with patch.object(config, "PROFILE", "laptop"):
        endpoint, model, btype = await policy.resolve("coder", "session-123")
    assert endpoint == config.OLLAMA_URL
    assert model    == config.OLLAMA_MODEL
    assert btype    == "ollama"


@pytest.mark.asyncio
async def test_resolve_default_session_skips_redis(policy_with_redis, mock_redis):
    """session_id='default' should skip Redis lookup."""
    with patch.object(config, "PROFILE", "laptop"):
        await policy_with_redis.resolve("coder", "default")
    mock_redis.hget.assert_not_called()


# ── resolve() — with Redis override ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_redis_override_used_when_present(policy_with_redis, mock_redis):
    mock_redis.hget = AsyncMock(return_value="qwen2.5-coder:7b")
    endpoint, model, btype = await policy_with_redis.resolve("coder", "session-abc")
    assert model == "qwen2.5-coder:7b"
    assert btype == "ollama"
    mock_redis.hget.assert_called_once_with("session:models:session-abc", "coder")


@pytest.mark.asyncio
async def test_resolve_redis_none_falls_back_to_profile(policy_with_redis, mock_redis):
    mock_redis.hget = AsyncMock(return_value=None)
    with patch.object(config, "PROFILE", "laptop"):
        endpoint, model, btype = await policy_with_redis.resolve("architect", "session-xyz")
    assert model == config.OLLAMA_MODEL


@pytest.mark.asyncio
async def test_resolve_redis_error_falls_back_to_profile(policy_with_redis, mock_redis):
    """Redis failure must not crash resolution — falls back to profile defaults."""
    mock_redis.hget = AsyncMock(side_effect=Exception("connection refused"))
    with patch.object(config, "PROFILE", "laptop"):
        endpoint, model, btype = await policy_with_redis.resolve("coder", "session-err")
    assert endpoint == config.OLLAMA_URL
    assert model    == config.OLLAMA_MODEL


@pytest.mark.asyncio
async def test_resolve_returns_tuple_of_three(policy):
    result = await policy.resolve("coder")
    assert len(result) == 3
    endpoint, model, btype = result
    assert isinstance(endpoint, str)
    assert isinstance(model,    str)
    assert btype in ("ollama", "vllm")


# ── get_session_models ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_session_models_returns_stored_map(policy_with_redis, mock_redis):
    mock_redis.hgetall = AsyncMock(return_value={
        "architect": "Qwen/Qwen3.5-35B-A3B",
        "coder":     "qwen2.5-coder:7b",
    })
    models = await policy_with_redis.get_session_models("session-xyz")
    assert models["architect"] == "Qwen/Qwen3.5-35B-A3B"
    assert models["coder"]     == "qwen2.5-coder:7b"


@pytest.mark.asyncio
async def test_get_session_models_no_redis_returns_empty(policy):
    models = await policy.get_session_models("any-session")
    assert models == {}


@pytest.mark.asyncio
async def test_get_session_models_redis_error_returns_empty(policy_with_redis, mock_redis):
    mock_redis.hgetall = AsyncMock(side_effect=Exception("timeout"))
    models = await policy_with_redis.get_session_models("session-err")
    assert models == {}


# ── _endpoint_for_model ───────────────────────────────────────────────────────

def test_endpoint_for_ollama_model_returns_ollama_url():
    from routing_policy import _endpoint_for_model
    from model_registry import init_model_registry
    init_model_registry()
    with patch.object(config, "PROFILE", "laptop"):
        endpoint = _endpoint_for_model("qwen2.5-coder:7b")
    assert endpoint == config.OLLAMA_URL


def test_endpoint_for_vllm_model_gpu_profile(mock_redis):
    from routing_policy import _endpoint_for_model
    from model_registry import init_model_registry
    init_model_registry()
    with patch.object(config, "PROFILE", "gpu"), \
         patch.object(config, "ARCHITECT_MODEL", "Qwen/Qwen3.5-35B-A3B"):
        endpoint = _endpoint_for_model("Qwen/Qwen3.5-35B-A3B")
    assert endpoint == config.ARCHITECT_URL


def test_endpoint_for_unknown_model_returns_coder_url():
    from routing_policy import _endpoint_for_model
    from model_registry import init_model_registry
    init_model_registry()
    with patch.object(config, "PROFILE", "gpu"):
        endpoint = _endpoint_for_model("some/unknown-model")
    assert endpoint == config.CODER_URL


# ── Singleton ─────────────────────────────────────────────────────────────────

def test_init_routing_policy_returns_instance():
    from routing_policy import init_routing_policy, RoutingPolicy
    p = init_routing_policy()
    assert isinstance(p, RoutingPolicy)


def test_get_routing_policy_before_init_raises():
    from routing_policy import get_routing_policy
    with pytest.raises(RuntimeError, match="not initialised"):
        get_routing_policy()


def test_get_routing_policy_after_init_returns_same_instance():
    from routing_policy import init_routing_policy, get_routing_policy
    p1 = init_routing_policy()
    p2 = get_routing_policy()
    assert p1 is p2


def test_set_redis_wires_redis_client(policy, mock_redis):
    policy.set_redis(mock_redis)
    assert policy._redis is mock_redis