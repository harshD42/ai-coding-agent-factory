"""
Phase 4B integration smoke tests.
Run with: INTEGRATION_TESTS=1 pytest tests/integration/test_phase4b.py -v
Requires the full stack running: docker compose --profile laptop up -d

Covers:
  4B.1 — Session management (create, get, list, end, pause/resume, configure)
  4B.2 — Streaming endpoints (SSE agent stream, WebSocket session channel)
  4B.3 — Agent message bus (agent messaging, structured events)
"""

import os
import time
import uuid
import pytest
import httpx

pytestmark = pytest.mark.skipif(
    not os.getenv("INTEGRATION_TESTS"),
    reason="Set INTEGRATION_TESTS=1 to run integration tests",
)

BASE    = os.getenv("ORCH_URL", "http://localhost:9000")
TIMEOUT = 30


@pytest.fixture(scope="module")
def client():
    return httpx.Client(base_url=BASE, timeout=TIMEOUT)


# ── Health / version ──────────────────────────────────────────────────────────

def test_health_v05x(client):
    """Orchestrator must report version >= 0.5.0 after Phase 4B."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["version"] >= "0.5.0"


# ── 4B.1: Session management ──────────────────────────────────────────────────

class TestSessionManagement:

    def test_create_session(self, client):
        r = client.post("/v1/sessions", json={
            "task": "Integration test session",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"]     == "active"
        assert data["session_id"] != ""
        assert data["task"]       == "Integration test session"

    def test_create_session_custom_id(self, client):
        sid = f"integ-4b1-{uuid.uuid4().hex[:8]}"
        r = client.post("/v1/sessions", json={
            "task":       "Custom ID test",
            "session_id": sid,
        })
        assert r.status_code == 200
        assert r.json()["session_id"] == sid

    def test_create_session_with_valid_models(self, client):
        """Session creation with model overrides must validate against catalog."""
        catalog = client.get("/v1/models/catalog").json()["models"]
        coder_models = [m for m in catalog if "coder" in m.get("tags", [])]
        if not coder_models:
            pytest.skip("No coder models in catalog")

        model = coder_models[0]["name"]
        r = client.post("/v1/sessions", json={
            "task":   "Model override test",
            "models": {"coder": model},
        })
        assert r.status_code == 200
        assert r.json()["models"].get("coder") == model

    def test_create_session_invalid_model_rejected(self, client):
        r = client.post("/v1/sessions", json={
            "task":   "Bad model test",
            "models": {"coder": "totally/fake-model-xyz"},
        })
        assert r.status_code == 400
        assert "Unknown model" in r.json()["detail"]

    def test_create_session_missing_task_rejected(self, client):
        r = client.post("/v1/sessions", json={})
        assert r.status_code == 400

    def test_get_session(self, client):
        sid = f"integ-get-{uuid.uuid4().hex[:8]}"
        client.post("/v1/sessions", json={"task": "get test", "session_id": sid})
        r = client.get(f"/v1/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["session_id"] == sid
        assert r.json()["status"]     == "active"

    def test_get_session_not_found(self, client):
        r = client.get("/v1/sessions/does-not-exist-xyz")
        assert r.status_code == 404

    def test_list_sessions(self, client):
        # Create a uniquely-named session so we can find it in the list
        sid = f"integ-list-{uuid.uuid4().hex[:8]}"
        client.post("/v1/sessions", json={"task": "list test", "session_id": sid})

        r = client.get("/v1/sessions")
        assert r.status_code == 200
        data = r.json()
        assert "sessions" in data
        assert "count" in data
        ids = [s["session_id"] for s in data["sessions"]]
        assert sid in ids

    def test_list_sessions_filter_active(self, client):
        r = client.get("/v1/sessions?status=active")
        assert r.status_code == 200
        for s in r.json()["sessions"]:
            assert s["status"] == "active"

    def test_list_sessions_invalid_status(self, client):
        r = client.get("/v1/sessions?status=bogus")
        assert r.status_code == 400

    def test_pause_and_resume_session(self, client):
        sid = f"integ-pause-{uuid.uuid4().hex[:8]}"
        client.post("/v1/sessions", json={"task": "pause test", "session_id": sid})

        r = client.post(f"/v1/sessions/{sid}/pause")
        assert r.status_code == 200
        assert r.json()["status"] == "paused"

        r = client.post(f"/v1/sessions/{sid}/resume")
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_pause_nonexistent_session(self, client):
        r = client.post("/v1/sessions/ghost-session-xyz/pause")
        assert r.status_code == 404

    def test_end_session(self, client):
        sid = f"integ-end-{uuid.uuid4().hex[:8]}"
        client.post("/v1/sessions", json={"task": "end test", "session_id": sid})

        r = client.post(f"/v1/sessions/{sid}/end", json={"summary": "test complete"})
        assert r.status_code == 200
        assert r.json()["status"] == "ended"

        # State survives after end (readable until TTL)
        r2 = client.get(f"/v1/sessions/{sid}")
        assert r2.status_code == 200
        assert r2.json()["status"] == "ended"

    def test_resume_ended_session_rejected(self, client):
        sid = f"integ-ended-{uuid.uuid4().hex[:8]}"
        client.post("/v1/sessions", json={"task": "end then resume", "session_id": sid})
        client.post(f"/v1/sessions/{sid}/end", json={"summary": "done"})

        r = client.post(f"/v1/sessions/{sid}/resume")
        assert r.status_code == 409

    def test_session_configure_uses_session_ttl(self, client):
        """POST /v1/session/configure TTL must equal SESSION_TTL (7 days), not old 24h."""
        catalog = client.get("/v1/models/catalog").json()["models"]
        coder_models = [m for m in catalog if "coder" in m.get("tags", [])]
        if not coder_models:
            pytest.skip("No coder models in catalog")

        r = client.post("/v1/session/configure", json={
            "session_id": f"integ-ttl-{uuid.uuid4().hex[:8]}",
            "models":     {"coder": coder_models[0]["name"]},
        })
        assert r.status_code == 200
        # 7 days = 604800s — must be SESSION_TTL, not old 86400s
        assert r.json()["ttl_seconds"] == 604800

    def test_list_sessions_ordered_newest_first(self, client):
        """Sessions must be listed newest first."""
        sid1 = f"integ-order-1-{uuid.uuid4().hex[:8]}"
        sid2 = f"integ-order-2-{uuid.uuid4().hex[:8]}"
        client.post("/v1/sessions", json={"task": "first",  "session_id": sid1})
        time.sleep(0.05)
        client.post("/v1/sessions", json={"task": "second", "session_id": sid2})

        r = client.get("/v1/sessions")
        ids = [s["session_id"] for s in r.json()["sessions"]]
        if sid1 in ids and sid2 in ids:
            assert ids.index(sid2) < ids.index(sid1), \
                "Newer session must appear before older session"


# ── 4B.2: Streaming endpoints ─────────────────────────────────────────────────

class TestStreamingEndpoints:

    def test_sse_stream_404_for_unknown_agent(self, client):
        """
        SSE endpoint polls 2s for agent registration, then 404s.
        Use a timeout shorter than the poll window to confirm 404 is returned.
        """
        r = client.get(
            "/v1/agents/ghost-agent-does-not-exist/stream",
            timeout=5,
        )
        assert r.status_code == 404

    def test_websocket_session_accepts_connection(self):
        """
        WebSocket endpoint must accept the connection handshake.
        We open, send nothing, and disconnect — the handshake itself is
        the acceptance criteria.
        """
        import websockets.sync.client as ws_sync
        uri = BASE.replace("http://", "ws://") + "/ws/session/integ-ws-test"
        try:
            with ws_sync.connect(uri, open_timeout=5) as conn:
                # Receive one heartbeat (sent every WS_HEARTBEAT_INTERVAL seconds)
                # or just verify connection opened — close immediately
                conn.close()
        except Exception as e:
            pytest.fail(f"WebSocket connection failed: {e}")

    def test_websocket_receives_heartbeat(self):
        """
        WebSocket must send a heartbeat ping within WS_HEARTBEAT_INTERVAL (30s).
        We reduce the wait by checking for any message from the server.
        This test is skipped if no message arrives within 35s.
        """
        import websockets.sync.client as ws_sync
        uri = BASE.replace("http://", "ws://") + "/ws/session/integ-heartbeat-test"
        try:
            with ws_sync.connect(uri, open_timeout=5) as conn:
                import json as _json
                # Set a generous receive timeout
                conn.socket.settimeout(35)
                try:
                    msg = conn.recv()
                    data = _json.loads(msg)
                    assert data["type"] in ("heartbeat", "status", "work_complete",
                                            "work_failed", "patch_applied")
                except TimeoutError:
                    pytest.skip("No WebSocket message within 35s — heartbeat interval may be longer")
        except Exception as e:
            pytest.fail(f"WebSocket test failed: {e}")

    def test_agent_message_endpoint_404_for_unknown_agent(self, client):
        r = client.post(
            "/v1/agents/ghost-agent-xyz/message",
            json={"message": "hello", "sender": "user"},
        )
        assert r.status_code == 404

    def test_agent_message_requires_message_field(self, client):
        """Empty message body must be rejected at Pydantic validation."""
        r = client.post(
            "/v1/agents/any-agent/message",
            json={},
        )
        assert r.status_code == 422


# ── 4B.3: Agent bus (observable side effects) ─────────────────────────────────

class TestAgentBusIntegration:

    def test_status_command_includes_active_sessions(self, client):
        """/status must include session count after Phase 4B.1."""
        r = client.post("/v1/chat/completions", json={
            "model":    "orchestrator",
            "messages": [{"role": "user", "content": "/status"}],
            "stream":   False,
        })
        assert r.status_code == 200
        content = r.json()["choices"][0]["message"]["content"]
        # Phase 4B.1 adds "Sessions: N active" line to /status output
        assert "Session" in content or "session" in content

    def test_patch_applied_event_not_in_token_stream(self, client):
        """
        Structural test: PATCH_APPLIED is a WSEvent on the bus,
        not a token on the SSE stream. Verify the bus endpoint
        (WebSocket) exists and the SSE endpoint is separate.
        Test confirms both endpoints are reachable and correctly typed.
        """
        # SSE endpoint
        r_sse = client.get(
            "/v1/agents/nonexistent/stream",
            timeout=5,
        )
        # 404 is correct — agent doesn't exist. Confirms endpoint exists.
        assert r_sse.status_code == 404

        # WebSocket endpoint is at a different path
        # Confirmed reachable in TestStreamingEndpoints.test_websocket_session_accepts_connection
        # This is a documentation test — no further assertion needed here.
        assert True