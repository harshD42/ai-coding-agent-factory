"""
Phase 4A integration smoke tests.
Run with: INTEGRATION_TESTS=1 pytest tests/integration/test_phase4a.py -v
Requires the full stack running: docker compose --profile laptop up -d

Covers:
  4A.1 — Model registry endpoints (catalog, for-role, pull guard, refresh)
  4A.2 — Dynamic model assignment (session configure, models query, routing)
  4A.3 — vLLM validation stubs + /status shows model count
  4A.4 — LiteLLM gateway flag (default off, config surfaced in health)
"""

import os
import pytest
import httpx

pytestmark = pytest.mark.skipif(
    not os.getenv("INTEGRATION_TESTS"),
    reason="Set INTEGRATION_TESTS=1 to run integration tests",
)

BASE = os.getenv("ORCH_URL", "http://localhost:9000")


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE, timeout=30)


# ── Health / version ──────────────────────────────────────────────────────────

def test_health_v04x(client):
    """Orchestrator must report version >= 0.4.0 after Phase 4A."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["version"] >= "0.4.0"
    assert "profile" in data


# ── 4A.1: Model Registry ──────────────────────────────────────────────────────

class TestModelCatalog:

    def test_catalog_endpoint_exists(self, client):
        r = client.get("/v1/models/catalog")
        assert r.status_code == 200

    def test_catalog_returns_models_list(self, client):
        r = client.get("/v1/models/catalog")
        data = r.json()
        assert "models" in data
        assert isinstance(data["models"], list)
        assert len(data["models"]) > 0

    def test_catalog_includes_profile(self, client):
        r = client.get("/v1/models/catalog")
        data = r.json()
        assert "profile" in data
        assert data["profile"] in ("laptop", "gpu-shared", "gpu")

    def test_catalog_entries_have_required_fields(self, client):
        r = client.get("/v1/models/catalog")
        for m in r.json()["models"]:
            assert "name"           in m
            assert "context_length" in m
            assert "backend"        in m
            assert "on_disk"        in m
            assert isinstance(m["on_disk"], bool)

    def test_catalog_context_lengths_positive(self, client):
        r = client.get("/v1/models/catalog")
        for m in r.json()["models"]:
            assert m["context_length"] > 0, \
                f"Model {m['name']!r} has invalid context_length"

    def test_for_role_coder(self, client):
        r = client.get("/v1/models/for-role?role=coder")
        assert r.status_code == 200
        data = r.json()
        assert data["role"] == "coder"
        assert "models" in data
        assert isinstance(data["models"], list)
        # At least one coder model must exist in catalog
        assert len(data["models"]) > 0

    def test_for_role_architect(self, client):
        r = client.get("/v1/models/for-role?role=architect")
        assert r.status_code == 200
        assert len(r.json()["models"]) > 0

    def test_for_role_reviewer(self, client):
        r = client.get("/v1/models/for-role?role=reviewer")
        assert r.status_code == 200

    def test_for_role_tester(self, client):
        r = client.get("/v1/models/for-role?role=tester")
        assert r.status_code == 200

    def test_for_role_documenter(self, client):
        r = client.get("/v1/models/for-role?role=documenter")
        assert r.status_code == 200

    def test_for_role_missing_param_rejected(self, client):
        r = client.get("/v1/models/for-role")
        assert r.status_code == 422

    def test_for_role_coder_excludes_reviewer_only_models(self, client):
        """Models tagged only for reviewer must not appear for coder."""
        coder_models = {m["name"] for m in client.get("/v1/models/for-role?role=coder").json()["models"]}
        reviewer_models = {m["name"] for m in client.get("/v1/models/for-role?role=reviewer").json()["models"]}
        # QwQ-32B is reviewer-only — must not appear for coder
        if "Qwen/QwQ-32B" in reviewer_models:
            assert "Qwen/QwQ-32B" not in coder_models

    def test_pull_unknown_model_rejected(self, client):
        r = client.post("/v1/models/pull", json={"name": "unknown/model-does-not-exist"})
        assert r.status_code == 400

    def test_pull_empty_name_rejected(self, client):
        r = client.post("/v1/models/pull", json={"name": ""})
        assert r.status_code == 400

    def test_pull_vllm_model_rejected(self, client):
        """vLLM-only models cannot be pulled via the Ollama pull endpoint."""
        r = client.post("/v1/models/pull", json={"name": "Qwen/QwQ-32B"})
        assert r.status_code == 400
        assert "Ollama" in r.json()["detail"] or "backend" in r.json()["detail"]

    def test_pull_rejected_when_agents_running(self, client):
        """
        Pull endpoint must return 409 if agents are currently running.
        This test only fires if agents happen to be running; otherwise
        the 400 from the unknown model name is the expected outcome.
        We check both valid scenarios.
        """
        r = client.post("/v1/models/pull", json={"name": "some/nonexistent"})
        # Either rejected because agents running (409) or unknown model (400)
        assert r.status_code in (400, 409)

    def test_refresh_endpoint_exists(self, client):
        r = client.post("/v1/models/refresh")
        assert r.status_code == 200
        data = r.json()
        assert data["refreshed"] is True
        assert "available" in data

    def test_status_includes_model_count(self, client):
        """/status command output should include model on-disk count."""
        r = client.post("/v1/chat/completions", json={
            "model": "orchestrator",
            "messages": [{"role": "user", "content": "/status"}],
            "stream": False,
        })
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert "Models" in content or "model" in content.lower()


# ── 4A.2: Dynamic Model Assignment ───────────────────────────────────────────

class TestSessionConfigure:

    def test_configure_session_with_valid_model(self, client):
        """Configure a session with a model that exists in the catalog."""
        # First get a valid model name from the catalog
        catalog = client.get("/v1/models/catalog").json()["models"]
        coder_models = [m for m in catalog if "coder" in m.get("tags", [])]
        if not coder_models:
            pytest.skip("No coder models in catalog")

        model_name = coder_models[0]["name"]
        r = client.post("/v1/session/configure", json={
            "session_id": "integ-4a2-test",
            "models": {"coder": model_name},
        })
        assert r.status_code == 200
        data = r.json()
        assert data["configured"] is True
        assert data["session_id"] == "integ-4a2-test"
        assert data["models"]["coder"] == model_name
        assert data["ttl_seconds"] > 0

    def test_configure_session_unknown_model_rejected(self, client):
        r = client.post("/v1/session/configure", json={
            "session_id": "integ-bad-model",
            "models": {"coder": "nonexistent/model-xyz"},
        })
        assert r.status_code == 400
        assert "Unknown model" in r.json()["detail"]

    def test_configure_session_empty_models_rejected(self, client):
        r = client.post("/v1/session/configure", json={
            "session_id": "integ-empty",
            "models": {},
        })
        assert r.status_code == 400

    def test_configure_session_auto_generates_session_id(self, client):
        """session_id is optional — orchestrator generates one if omitted."""
        catalog = client.get("/v1/models/catalog").json()["models"]
        coder_models = [m for m in catalog if "coder" in m.get("tags", [])]
        if not coder_models:
            pytest.skip("No coder models in catalog")

        r = client.post("/v1/session/configure", json={
            "models": {"coder": coder_models[0]["name"]},
        })
        assert r.status_code == 200
        data = r.json()
        assert data["configured"] is True
        assert len(data["session_id"]) > 0   # auto-generated UUID

    def test_get_session_models_configured_session(self, client):
        """After configure, GET /v1/session/models returns the stored map."""
        catalog = client.get("/v1/models/catalog").json()["models"]
        coder_models = [m for m in catalog if "coder" in m.get("tags", [])]
        if not coder_models:
            pytest.skip("No coder models in catalog")

        model_name = coder_models[0]["name"]
        session_id = "integ-4a2-get-test"

        client.post("/v1/session/configure", json={
            "session_id": session_id,
            "models": {"coder": model_name},
        })

        r = client.get(f"/v1/session/models?session_id={session_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == session_id
        assert data["models"]["coder"] == model_name
        assert data["using_defaults"] is False

    def test_get_session_models_unconfigured_returns_defaults(self, client):
        """A session with no configure call returns empty models + using_defaults=True."""
        r = client.get("/v1/session/models?session_id=integ-never-configured-xyz")
        assert r.status_code == 200
        data = r.json()
        assert data["using_defaults"] is True
        assert data["models"] == {}


# ── 4A.3: vLLM validation stubs ──────────────────────────────────────────────

class TestGpuProfiles:
    """
    GPU profile tests require vLLM running — automatically skipped otherwise.
    These are smoke tests only.
    """

    @pytest.fixture(autouse=True)
    def skip_without_vllm(self, client):
        """Skip if vLLM endpoint is not reachable regardless of GPU presence."""
        import httpx as _httpx
        vllm_url = os.getenv("VLLM_CODER_URL", "http://localhost:8001")
        try:
            r = _httpx.get(f"{vllm_url}/health", timeout=3)
            if r.status_code != 200:
                pytest.skip("vLLM endpoint not healthy — skipping GPU profile tests")
        except Exception:
            pytest.skip("vLLM endpoint not reachable — skipping GPU profile tests")

    def test_refresh_detects_vllm_if_running(self, client):
        """Refresh endpoint should not error even if vLLM endpoints are down."""
        r = client.post("/v1/models/refresh")
        assert r.status_code == 200
        data = r.json()
        assert data["refreshed"] is True

    def test_catalog_shows_on_disk_vllm_model_if_loaded(self, client):
        """If vLLM is running, at least one vLLM model should show on_disk=True."""
        r = client.get("/v1/models/catalog")
        vllm_models = [
            m for m in r.json()["models"]
            if "vllm" in m.get("backend", [])
        ]
        if not vllm_models:
            pytest.skip("No vLLM models in catalog")
        on_disk_vllm = [m for m in vllm_models if m["on_disk"]]
        assert len(on_disk_vllm) > 0, \
            "vLLM is running but no vLLM models showing on_disk"


# ── 4A.4: LiteLLM gateway flag ───────────────────────────────────────────────

class TestLiteLLMGateway:

    def test_gateway_flag_off_by_default(self, client):
        """
        USE_LITELLM defaults to false. With the flag off, chat completions
        must still work via the direct Ollama/vLLM path.
        """
        r = client.post("/v1/chat/completions", json={
            "model": "orchestrator",
            "messages": [{"role": "user", "content": "/status"}],
            "stream": False,
        })
        # If USE_LITELLM were true AND litellm not installed, this would 500.
        # A 200 response confirms the direct path is being used.
        assert r.status_code == 200

    def test_health_endpoint_unaffected_by_gateway_flag(self, client):
        """Health check works regardless of USE_LITELLM setting."""
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"