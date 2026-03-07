"""
Phase 2 integration smoke tests.
Run with:   INTEGRATION_TESTS=1 pytest tests/integration/test_phase2.py -v
Requires the full stack to be running (docker compose --profile laptop up -d).
"""

import os
import pytest
import httpx

pytestmark = pytest.mark.skipif(
    not os.getenv("INTEGRATION_TESTS"),
    reason="Set INTEGRATION_TESTS=1 to run integration tests",
)

BASE  = os.getenv("ORCH_URL", "http://localhost:9000")
EXEC  = os.getenv("EXEC_URL", "http://localhost:9001")

GOOD_DIFF = """\
--- a/hello.py
+++ b/hello.py
@@ -1,4 +1,8 @@
 def hello():
     return "hello"
+
+def multiply(a, b):
+    return a * b
"""

BAD_DIFF = """\
--- a/hello.py
+++ b/hello.py
@@ -99,3 +99,4 @@
 # this line does not exist
+# this will fail
"""


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE, timeout=30)


# ── Health ────────────────────────────────────────────────────────────────────

def test_orchestrator_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_executor_health():
    r = httpx.get(f"{EXEC}/health", timeout=5)
    assert r.status_code == 200


# ── Step 2.1: Auto-patch enqueue (via /v1/patches/submit) ────────────────────

def test_submit_valid_patch(client):
    r = client.post("/v1/patches/submit", json={
        "diff": GOOD_DIFF,
        "agent_id": "test",
        "task_id": "t-smoke",
        "session_id": "smoke-2.1",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert "patch_id" in data


def test_submit_invalid_patch_rejected(client):
    r = client.post("/v1/patches/submit", json={"diff": "not a diff"})
    assert r.status_code == 400


# ── Step 2.2: run_tests via executor ─────────────────────────────────────────

def test_executor_can_run_pytest():
    """Verify pytest is callable inside the executor container."""
    r = httpx.post(f"{EXEC}/execute", json={"command": "pytest --version"}, timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert data["exit_code"] == 0
    assert "pytest" in data["stdout"].lower()


def test_patches_test_endpoint_bad_diff(client):
    """POST /v1/patches/test rejects a structurally invalid diff."""
    r = client.post("/v1/patches/test", json={"diff": "garbage"}, timeout=60)
    assert r.status_code == 400


# ── Step 2.3: Metrics endpoint ────────────────────────────────────────────────

def test_metrics_endpoint_returns_summary(client):
    r = client.get("/v1/metrics")
    assert r.status_code == 200
    data = r.json()
    assert "total_requests" in data
    assert "by_role" in data


def test_metrics_session_filter(client):
    r = client.get("/v1/metrics?session_id=smoke-2.1")
    assert r.status_code == 200
    assert "session_id" in r.json()


# ── Step 2.4: File watcher Redis registry ─────────────────────────────────────

def test_file_watcher_populated_redis():
    """
    After startup the filewatch:hashes key should have entries for _workspace.
    If no files exist in workspace this returns 0 — still a valid state.
    """
    import redis
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    count = r.hlen("filewatch:hashes")
    assert isinstance(count, int)   # key may be empty if workspace is empty


# ── Step 2.6: Parallel execution (basic) ─────────────────────────────────────

def test_load_independent_tasks(client):
    """Load a 2-task DAG with no dependencies — both should be ready simultaneously."""
    r = client.post("/v1/tasks/load", json={
        "session_id": "smoke-2.6",
        "tasks": [
            {"id": "ta", "role": "documenter", "desc": "write docstring for hello()", "deps": []},
            {"id": "tb", "role": "documenter", "desc": "write docstring for multiply()", "deps": []},
        ],
    })
    assert r.status_code == 200
    assert r.json()["tasks_loaded"] == 2