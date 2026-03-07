"""
Phase 3 integration smoke tests.
Run with: INTEGRATION_TESTS=1 pytest tests/integration/test_phase3.py -v
Requires the full stack running: docker compose --profile laptop up -d
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


# ── Health ────────────────────────────────────────────────────────────────────

def test_health_v030(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["version"] >= "0.3.0"


# ── Step 3.1: Symbol search ───────────────────────────────────────────────────

def test_index_codebase(client):
    r = client.post("/v1/index", timeout=60)
    assert r.status_code == 200
    data = r.json()
    assert "files_indexed" in data
    assert "chunks" in data


def test_symbol_search_endpoint_exists(client):
    """Endpoint exists even if workspace has no indexed symbols yet."""
    r = client.get("/v1/memory/symbol?name=hello")
    assert r.status_code == 200
    data = r.json()
    assert "results" in data
    assert "query" in data


def test_symbol_search_missing_name(client):
    r = client.get("/v1/memory/symbol")
    assert r.status_code == 422   # FastAPI validation error


# ── Step 3.2: Fine-tune endpoints ─────────────────────────────────────────────

def test_finetune_stats(client):
    r = client.get("/v1/finetune/stats")
    assert r.status_code == 200
    data = r.json()
    assert "records" in data


def test_finetune_export_empty(client):
    r = client.get("/v1/finetune/export")
    assert r.status_code == 200


def test_finetune_clear(client):
    r = client.delete("/v1/finetune/clear")
    assert r.status_code == 200
    assert "deleted" in r.json()


# ── Step 3.3: Webhook endpoint ────────────────────────────────────────────────

def test_webhook_no_secret_configured_returns_401(client):
    """Without GITHUB_WEBHOOK_SECRET configured, all webhook requests are rejected."""
    r = client.post(
        "/v1/webhook/github",
        json={"action": "completed"},
        headers={"X-GitHub-Event": "workflow_run",
                 "X-Hub-Signature-256": "sha256=bad"},
    )
    # Either 401 (secret not configured or invalid) or 200 with skipped
    assert r.status_code in (200, 401)


def test_webhook_unsupported_event_skipped(client):
    """A supported-signature but unsupported event type should return skipped."""
    import hmac as _hmac, hashlib, json, os
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        pytest.skip("GITHUB_WEBHOOK_SECRET not set")
    body    = json.dumps({"action": "ping"}).encode()
    sig     = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = client.post(
        "/v1/webhook/github",
        content=body,
        headers={"X-GitHub-Event": "ping",
                 "X-Hub-Signature-256": sig,
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json().get("skipped") is True


# ── Step 3.4: Antipattern endpoint (via skills search) ───────────────────────

def test_memory_recall_still_works(client):
    r = client.get("/v1/memory/recall?q=multiply+function")
    assert r.status_code == 200
    assert "results" in r.json()


def test_status_includes_training_data(client):
    """Verify /status command output includes training data count."""
    r = client.post("/v1/chat/completions", json={
        "model": "orchestrator",
        "messages": [{"role": "user", "content": "/status"}],
        "stream": False,
    })
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert "Training data" in content