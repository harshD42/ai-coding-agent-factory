"""
tests/integration/test_api.py — Integration smoke tests.

These tests require the Docker stack to be running:
    docker compose --profile laptop up -d

Run with:
    pytest tests/integration/ -v --timeout=60

Skipped automatically in CI (no real services available).
"""

import os
import pytest
import httpx

# Skip all integration tests if INTEGRATION_TESTS env var is not set
# This prevents them from running in unit-test-only CI jobs
pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION_TESTS") != "1",
    reason="Set INTEGRATION_TESTS=1 to run integration tests (requires Docker stack)"
)

ORCH_URL     = os.environ.get("ORCH_URL",     "http://localhost:9000")
EXECUTOR_URL = os.environ.get("EXECUTOR_URL", "http://localhost:9001")
TIMEOUT      = 30


@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=TIMEOUT) as c:
        yield c


class TestOrchestratorHealth:
    def test_health_returns_ok(self, client):
        r = client.get(f"{ORCH_URL}/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "profile" in data
        assert "version" in data

    def test_models_endpoint(self, client):
        r = client.get(f"{ORCH_URL}/v1/models")
        assert r.status_code == 200
        data = r.json()
        assert data["object"] == "list"
        assert any(m["id"] == "orchestrator" for m in data["data"])


class TestExecutorHealth:
    def test_executor_health(self, client):
        r = client.get(f"{EXECUTOR_URL}/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_list_files(self, client):
        r = client.post(f"{EXECUTOR_URL}/list-files", json={"pattern": "**/*"})
        assert r.status_code == 200
        assert "files" in r.json()


    def test_execute_allowed_command(self, client):
        # Use 'sh' which is in the whitelist — 'echo' alone is not
        r = client.post(f"{EXECUTOR_URL}/execute", json={"command": "sh -c 'echo hello'"})
        assert r.status_code == 200
        data = r.json()
        assert data["exit_code"] == 0
        assert "hello" in data["stdout"]

    def test_execute_disallowed_command_rejected(self, client):
        r = client.post(f"{EXECUTOR_URL}/execute", json={"command": "rm -rf /"})
        assert r.status_code == 400
        assert "not in allowed list" in r.json()["detail"]

    def test_path_traversal_rejected(self, client):
        r = client.post(f"{EXECUTOR_URL}/read-file", json={"path": "../../etc/passwd"})
        assert r.status_code == 400

    def test_patch_validation_bad_diff(self, client):
        # The executor's /apply-patch does basic size/binary checks only.
        # Structural diff validation (hunk headers etc) lives in the orchestrator's
        # patch_queue.validate_patch(). A non-diff string passes HTTP validation
        # but git apply rejects it — response is 200 with applied=false.
        r = client.post(
            f"{EXECUTOR_URL}/apply-patch",
            json={"diff": "this is not a diff", "target": "sandbox"}
        )
        assert r.status_code == 200
        assert r.json()["applied"] is False

    def test_patch_sandbox_valid_diff(self, client):
        diff = (
            "--- a/hello.py\n+++ b/hello.py\n"
            "@@ -1 +1,2 @@\n"
            "-def hello(): print('hello world')\n"
            "+def hello():\n"
            "+    print('hello world')\n"
        )
        r = client.post(
            f"{EXECUTOR_URL}/apply-patch",
            json={"diff": diff, "target": "sandbox"}
        )
        assert r.status_code == 200
        # sandbox either passes or fails git apply, but doesn't 500


class TestCommandInterception:
    def test_status_command_intercepted(self, client):
        r = client.post(
            f"{ORCH_URL}/v1/chat/completions",
            json={
                "model": "orchestrator",
                "messages": [{"role": "user", "content": "/status"}],
                "stream": False,
            }
        )
        assert r.status_code == 200
        data    = r.json()
        content = data["choices"][0]["message"]["content"]
        assert "Status" in content or "status" in content

    def test_unknown_command_returns_help(self, client):
        r = client.post(
            f"{ORCH_URL}/v1/chat/completions",
            json={
                "model": "orchestrator",
                "messages": [{"role": "user", "content": "/unknowncmd"}],
                "stream": False,
            }
        )
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert "Unknown" in content or "command" in content.lower()

    def test_non_command_passes_to_model(self, client):
        r = client.post(
            f"{ORCH_URL}/v1/chat/completions",
            json={
                "model": "orchestrator",
                "messages": [{"role": "user", "content": "What is 2+2?"}],
                "stream": False,
            },
            timeout=60,
        )
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        assert len(content) > 0


class TestMemoryEndpoints:
    def test_index_endpoint(self, client):
        r = client.post(f"{ORCH_URL}/v1/index")
        assert r.status_code == 200
        data = r.json()
        assert "files_indexed" in data
        assert "chunks" in data

    def test_save_and_recall(self, client):
        # Save
        r = client.post(
            f"{ORCH_URL}/v1/memory/save",
            json={"session_id": "integ-test", "content": "Integration test memory entry XYZ123"}
        )
        assert r.status_code == 200
        assert r.json()["saved"] is True

        # Recall
        r = client.get(f"{ORCH_URL}/v1/memory/recall?q=XYZ123")
        assert r.status_code == 200
        results = r.json()["results"]
        assert any("XYZ123" in res["content"] for res in results)

    def test_recall_empty_query_rejected(self, client):
        r = client.get(f"{ORCH_URL}/v1/memory/recall")
        assert r.status_code == 422  # missing required query param


class TestPatchEndpoints:
    def test_submit_invalid_diff_rejected(self, client):
        r = client.post(
            f"{ORCH_URL}/v1/patches/submit",
            json={"diff": "not a diff", "agent_id": "test", "task_id": "t1", "session_id": "s1"}
        )
        assert r.status_code == 400

    def test_submit_empty_diff_rejected(self, client):
        r = client.post(
            f"{ORCH_URL}/v1/patches/submit",
            json={"diff": "", "agent_id": "test", "task_id": "t1", "session_id": "s1"}
        )
        assert r.status_code == 400

    def test_patches_status(self, client):
        r = client.get(f"{ORCH_URL}/v1/patches/status")
        assert r.status_code == 200
        data = r.json()
        for key in ("total", "pending", "applied", "rejected", "conflict"):
            assert key in data


class TestTaskEndpoints:
    def test_load_valid_dag(self, client):
        tasks = [
            {"id": "t1", "role": "coder",  "desc": "task 1", "deps": []},
            {"id": "t2", "role": "tester", "desc": "task 2", "deps": ["t1"]},
        ]
        r = client.post(
            f"{ORCH_URL}/v1/tasks/load",
            json={"session_id": "integ-dag-test", "tasks": tasks}
        )
        assert r.status_code == 200
        assert r.json()["tasks_loaded"] == 2

    def test_load_cyclic_dag_rejected(self, client):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": ["t2"]},
            {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
        ]
        r = client.post(
            f"{ORCH_URL}/v1/tasks/load",
            json={"session_id": "integ-cycle-test", "tasks": tasks}
        )
        assert r.status_code == 400

    def test_task_status(self, client):
        r = client.get(f"{ORCH_URL}/v1/tasks/status?session_id=integ-dag-test")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        assert "pending" in data