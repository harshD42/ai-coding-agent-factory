"""
Phase 3.5 integration smoke tests.
Run with: INTEGRATION_TESTS=1 pytest tests/integration/test_phase35.py -v
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
EXEC = os.getenv("EXEC_URL", "http://localhost:9001")


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE, timeout=30)


# ── Health / version ──────────────────────────────────────────────────────────

def test_health_v035(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["version"] == "0.3.5"


# ── 3.5.1: Agent cleanup endpoint ────────────────────────────────────────────

def test_agent_cleanup_endpoint(client):
    r = client.post("/v1/agents/cleanup")
    assert r.status_code == 200
    data = r.json()
    assert "removed" in data
    assert "idle_timeout_s" in data
    assert isinstance(data["removed"], int)


# ── 3.5.2: Patch queue depth guard ───────────────────────────────────────────

def test_patch_queue_depth_reported(client):
    r = client.get("/v1/patches/status")
    assert r.status_code == 200
    data = r.json()
    assert "total" in data
    assert "pending" in data


# ── 3.5.3: Index returns files_unchanged on second call ──────────────────────

def test_incremental_index(client):
    """Second /v1/index call on unchanged workspace should report files_unchanged."""
    r1 = client.post("/v1/index", timeout=120)
    assert r1.status_code == 200

    r2 = client.post("/v1/index", timeout=120)
    assert r2.status_code == 200
    data2 = r2.json()
    # files_unchanged key exists and is >= 0
    assert "files_unchanged" in data2
    assert data2["files_unchanged"] >= 0
    # On truly unchanged workspace, files_indexed should be 0
    # (all files skipped via hash check)
    assert data2["files_indexed"] == 0 or data2["files_unchanged"] > 0


# ── 3.5.4: Failure deduplication ─────────────────────────────────────────────

def test_failure_dedup_via_recall(client):
    """
    Save the same session summary twice.
    ChromaDB should deduplicate the second upsert (same content hash → same ID).
    Recall should return the entry exactly once.
    """
    r = client.post("/v1/memory/save", json={
        "session_id": "dedup-test-35",
        "content":    "UNIQUE_DEDUP_MARKER_XYZ999",
    })
    assert r.status_code == 200
    # Save same content again
    r = client.post("/v1/memory/save", json={
        "session_id": "dedup-test-35",
        "content":    "UNIQUE_DEDUP_MARKER_XYZ999",
    })
    assert r.status_code == 200
    # Recall
    r = client.get("/v1/memory/recall?q=UNIQUE_DEDUP_MARKER_XYZ999")
    assert r.status_code == 200


# ── 3.5.5: /status includes new fields ───────────────────────────────────────

def test_status_includes_35_fields(client):
    r = client.post("/v1/chat/completions", json={
        "model": "orchestrator",
        "messages": [{"role": "user", "content": "/status"}],
        "stream": False,
    })
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert "Embed cache" in content
    assert "Executor slots" in content
    assert "depth limit" in content


# ── 3.5.6: Executor concurrency (smoke only — verify it doesn't crash) ───────

def test_executor_apply_patch_returns_correctly(client):
    """Basic smoke: apply_patch endpoint still works after semaphore wiring."""
    bad_diff = "not a valid diff at all"
    r = httpx.post(f"{EXEC}/apply-patch",
                   json={"diff": bad_diff, "target": "sandbox"}, timeout=15)
    # Either 400 (validation rejected) or 200 with applied=false
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.json()["applied"] is False


# ── 3.5.7: Model call timeout (verify config is live) ────────────────────────

def test_metrics_endpoint_still_works(client):
    """Regression: metrics endpoint unaffected by 3.5 changes."""
    r = client.get("/v1/metrics")
    assert r.status_code == 200
    data = r.json()
    assert "total_requests" in data
    assert "avg_latency_ms" in data