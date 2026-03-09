"""
tests/unit/test_model_registry.py — Unit tests for Phase 4A.1 model registry.

Covers:
  - Catalog integrity (every entry has required fields)
  - Role filtering via ROLE_TAG_MAP
  - context_length resolution (known + unknown models)
  - on_disk status after detect_available()
  - Ollama and vLLM detection paths (mocked)
  - Pull endpoint validation
  - Singleton lifecycle

sys.path is managed by tests/conftest.py — no local path manipulation needed.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch


# ── Fixtures ──────────────────────────────────────────────────────────────────

# Registry reset is handled by the autouse fixture in tests/conftest.py.

@pytest.fixture
def registry():
    from model_registry import init_model_registry
    return init_model_registry()


# ── Catalog integrity ─────────────────────────────────────────────────────────

def test_all_catalog_entries_have_required_fields():
    from model_registry import MODEL_CATALOG
    required = {"tags", "context_length", "vram_approx_gb", "backend"}
    for name, meta in MODEL_CATALOG.items():
        missing = required - set(meta.keys())
        assert not missing, f"Model {name!r} missing fields: {missing}"


def test_all_catalog_entries_have_positive_context_length():
    from model_registry import MODEL_CATALOG
    for name, meta in MODEL_CATALOG.items():
        assert meta["context_length"] > 0, f"{name!r} has invalid context_length"


def test_all_catalog_entries_have_valid_backend():
    from model_registry import MODEL_CATALOG
    valid_backends = {"ollama", "vllm"}
    for name, meta in MODEL_CATALOG.items():
        for b in meta["backend"]:
            assert b in valid_backends, f"{name!r} has unknown backend: {b!r}"


def test_all_catalog_entries_have_nonempty_tags():
    from model_registry import MODEL_CATALOG
    for name, meta in MODEL_CATALOG.items():
        assert meta["tags"], f"{name!r} has empty tags"


def test_role_tag_map_covers_all_roles():
    from model_registry import ROLE_TAG_MAP
    expected = {"architect", "coder", "reviewer", "tester", "documenter"}
    assert set(ROLE_TAG_MAP.keys()) == expected


# ── Role filtering ─────────────────────────────────────────────────────────────

def test_get_models_for_role_coder_returns_coder_models(registry):
    models = registry.get_models_for_role("coder")
    assert models, "Expected at least one coder model in catalog"
    for m in models:
        assert any(t in ["coder", "tester"] for t in m["tags"]), \
            f"Model {m['name']!r} returned for coder but has no coder/tester tag"


def test_get_models_for_role_architect_excludes_coder_only_models(registry):
    from model_registry import MODEL_CATALOG
    models = registry.get_models_for_role("architect")
    model_names = {m["name"] for m in models}
    for name, meta in MODEL_CATALOG.items():
        tags = set(meta["tags"])
        if tags <= {"coder", "tester"}:
            assert name not in model_names, \
                f"Coder-only model {name!r} should not appear for architect role"


def test_get_models_for_role_tester_includes_coder_models(registry):
    """Tester role accepts coder-tagged models per ROLE_TAG_MAP."""
    from model_registry import MODEL_CATALOG
    tester_models = {m["name"] for m in registry.get_models_for_role("tester")}
    for name, meta in MODEL_CATALOG.items():
        if "coder" in meta["tags"]:
            assert name in tester_models, \
                f"Coder model {name!r} should be available for tester role"


def test_get_models_for_unknown_role_returns_empty(registry):
    result = registry.get_models_for_role("nonexistent_role")
    assert result == []


def test_get_models_for_role_returns_dicts_with_expected_keys(registry):
    expected_keys = {"name", "display_name", "tags", "context_length",
                     "vram_approx_gb", "backend", "on_disk"}
    for role in ["architect", "coder", "reviewer", "tester", "documenter"]:
        for m in registry.get_models_for_role(role):
            assert expected_keys <= set(m.keys()), \
                f"Missing keys in model entry for role {role!r}: {m}"


def test_get_models_for_role_on_disk_models_listed_first(registry):
    """On-disk models should sort before off-disk models."""
    registry._on_disk = {"qwen2.5-coder:7b"}
    models = registry.get_models_for_role("coder")
    if len(models) < 2:
        pytest.skip("need at least 2 coder models in catalog for sort test")
    on_disk_indices  = [i for i, m in enumerate(models) if m["on_disk"]]
    off_disk_indices = [i for i, m in enumerate(models) if not m["on_disk"]]
    if on_disk_indices and off_disk_indices:
        assert max(on_disk_indices) < min(off_disk_indices), \
            "On-disk models should appear before off-disk models"


# ── Context length resolution ─────────────────────────────────────────────────

def test_get_context_length_known_model(registry):
    assert registry.get_context_length("qwen2.5-coder:7b") == 32768


def test_get_context_length_architect_model(registry):
    """Architect model has 65536 context per catalog."""
    assert registry.get_context_length("Qwen/Qwen3.5-35B-A3B") == 65536


def test_get_context_length_unknown_model_returns_default(registry):
    from model_registry import DEFAULT_CONTEXT_LENGTH
    result = registry.get_context_length("unknown/model-that-doesnt-exist")
    assert result == DEFAULT_CONTEXT_LENGTH


def test_get_context_length_embedding_model(registry):
    assert registry.get_context_length("nomic-embed-text") == 8192


# ── on_disk status ────────────────────────────────────────────────────────────

def test_is_on_disk_false_before_detection(registry):
    assert not registry.is_on_disk("qwen2.5-coder:7b")


def test_is_on_disk_true_after_marking(registry):
    registry._on_disk = {"qwen2.5-coder:7b"}
    assert registry.is_on_disk("qwen2.5-coder:7b")
    assert not registry.is_on_disk("Qwen/QwQ-32B")


# ── detect_available ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_detect_available_ollama_success(registry):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "models": [
            {"name": "qwen2.5-coder:7b"},
            {"name": "nomic-embed-text"},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(registry._client, "get", new=AsyncMock(return_value=mock_response)):
        result = await registry._detect_ollama()

    assert "qwen2.5-coder:7b" in result
    assert "nomic-embed-text" in result


@pytest.mark.asyncio
async def test_detect_available_ollama_failure_returns_none(registry):
    with patch.object(registry._client, "get", new=AsyncMock(side_effect=Exception("conn refused"))):
        result = await registry._detect_ollama()
    assert result is None


@pytest.mark.asyncio
async def test_detect_available_vllm_success(registry):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [{"id": "Qwen/Qwen3-Coder-Next-80B-A3B-Instruct"}]
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(registry._client, "get", new=AsyncMock(return_value=mock_response)):
        result = await registry._detect_vllm("http://vllm-coder:8000/v1")

    assert "Qwen/Qwen3-Coder-Next-80B-A3B-Instruct" in result


@pytest.mark.asyncio
async def test_detect_available_vllm_failure_returns_none(registry):
    with patch.object(registry._client, "get", new=AsyncMock(side_effect=Exception("timeout"))):
        result = await registry._detect_vllm("http://vllm-coder:8000/v1")
    assert result is None


@pytest.mark.asyncio
async def test_detect_available_populates_on_disk(registry):
    ollama_resp = MagicMock()
    ollama_resp.status_code = 200
    ollama_resp.json.return_value = {"models": [{"name": "qwen2.5-coder:7b"}]}
    ollama_resp.raise_for_status = MagicMock()

    async def mock_get(url, **kwargs):
        if "11434" in url or "ollama" in url:
            return ollama_resp
        raise Exception("vllm not available")

    with patch.object(registry._client, "get", new=AsyncMock(side_effect=mock_get)):
        await registry.detect_available()

    assert registry.is_on_disk("qwen2.5-coder:7b")
    assert not registry.is_on_disk("Qwen/QwQ-32B")


@pytest.mark.asyncio
async def test_detect_available_all_backends_down_does_not_crash(registry):
    with patch.object(registry._client, "get", new=AsyncMock(side_effect=Exception("all down"))):
        result = await registry.detect_available()
    assert result == {}
    assert len(registry._on_disk) == 0


# ── catalog_with_status ───────────────────────────────────────────────────────

def test_catalog_with_status_returns_all_models(registry):
    from model_registry import MODEL_CATALOG
    catalog = registry.catalog_with_status()
    assert len(catalog) == len(MODEL_CATALOG)


def test_catalog_with_status_has_on_disk_field(registry):
    catalog = registry.catalog_with_status()
    for entry in catalog:
        assert "on_disk" in entry
        assert isinstance(entry["on_disk"], bool)


def test_catalog_with_status_on_disk_false_before_detection(registry):
    catalog = registry.catalog_with_status()
    assert all(not m["on_disk"] for m in catalog)


def test_catalog_with_status_on_disk_true_after_marking(registry):
    registry._on_disk = {"qwen2.5-coder:7b"}
    catalog = registry.catalog_with_status()
    on_disk = [m for m in catalog if m["on_disk"]]
    assert len(on_disk) == 1
    assert on_disk[0]["name"] == "qwen2.5-coder:7b"


# ── Pull ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pull_unknown_model_raises_value_error(registry):
    with pytest.raises(ValueError, match="not in the catalog"):
        await registry.pull_model("unknown/model-xyz")


@pytest.mark.asyncio
async def test_pull_vllm_only_model_raises_value_error(registry):
    """vLLM-only models cannot be pulled via Ollama pull endpoint."""
    with pytest.raises(ValueError, match="only Ollama models"):
        await registry.pull_model("Qwen/QwQ-32B")


@pytest.mark.asyncio
async def test_pull_ollama_model_success(registry):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    with patch.object(registry._client, "post", new=AsyncMock(return_value=mock_resp)), \
         patch.object(registry, "detect_available", new=AsyncMock(return_value={})):
        result = await registry.pull_model("qwen2.5-coder:7b")

    assert result["pulled"] is True
    assert result["name"] == "qwen2.5-coder:7b"


@pytest.mark.asyncio
async def test_pull_http_failure_raises_runtime_error(registry):
    import httpx
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )

    with patch.object(registry._client, "post", new=AsyncMock(return_value=mock_resp)):
        with pytest.raises(RuntimeError, match="Ollama pull failed"):
            await registry.pull_model("qwen2.5-coder:7b")


# ── Singleton ─────────────────────────────────────────────────────────────────

def test_init_model_registry_returns_instance():
    from model_registry import init_model_registry, ModelRegistry
    reg = init_model_registry()
    assert isinstance(reg, ModelRegistry)


def test_get_model_registry_before_init_raises():
    from model_registry import get_model_registry
    with pytest.raises(RuntimeError, match="not initialised"):
        get_model_registry()


def test_get_model_registry_after_init_returns_same_instance():
    from model_registry import init_model_registry, get_model_registry
    reg1 = init_model_registry()
    reg2 = get_model_registry()
    assert reg1 is reg2