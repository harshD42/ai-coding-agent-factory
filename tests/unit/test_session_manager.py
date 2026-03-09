"""
tests/unit/test_session_manager.py — Unit tests for Phase 4B.1 SessionManager.

All tests use a fake Redis and a mock AgentManager — no real Redis or
model backend needed. Tests mirror the acceptance criteria from the build plan.
"""

import asyncio
import json
import time
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "orchestrator"))

from session_manager import SessionManager, SessionState


# ── Fake Redis ────────────────────────────────────────────────────────────────

class _FakePipeline:
    """Minimal fake for redis.pipeline() context."""
    def __init__(self, store: dict):
        self._store = store
        self._calls: list = []

    def set(self, key, value, ex=None):
        self._calls.append(("set", key, value, ex))
        return self

    def expire(self, key, ttl):
        self._calls.append(("expire", key, ttl))
        return self

    async def execute(self):
        for op, *args in self._calls:
            if op == "set":
                key, value, *_ = args
                self._store[key] = value
            elif op == "expire":
                pass   # TTL not simulated in fake
        return [True] * len(self._calls)

    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass


class FakeRedis:
    """In-memory Redis substitute for unit tests."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def pipeline(self):
        return _FakePipeline(self._store)

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value: str, ex=None, nx=False):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def hset(self, key: str, mapping: dict = None, **kwargs):
        existing = json.loads(self._store.get(key, "{}")) if key in self._store else {}
        existing.update(mapping or kwargs)
        self._store[key] = json.dumps(existing)

    async def hget(self, key: str, field: str):
        raw = self._store.get(key)
        if not raw:
            return None
        d = json.loads(raw)
        return d.get(field)

    async def hgetall(self, key: str):
        raw = self._store.get(key)
        if not raw:
            return {}
        return json.loads(raw)

    async def expire(self, key: str, ttl: int):
        return 1   # key exists (fake)

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)

    async def scan(self, cursor=0, match="*", count=100):
        import fnmatch
        pattern = match.replace("*", "**")
        matched = [k for k in self._store if fnmatch.fnmatch(k, match)]
        return 0, matched   # cursor=0 means scan complete


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def mock_agent_mgr():
    mgr = MagicMock()
    mgr.cleanup_idle_agents = AsyncMock(return_value=3)
    return mgr


@pytest.fixture
def sm(fake_redis, mock_agent_mgr):
    """SessionManager backed by in-memory fake Redis."""
    return SessionManager(fake_redis, mock_agent_mgr)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_hooks():
    """Patch SessionHooks so tests don't need a real ChromaDB / Ollama."""
    hooks = MagicMock()
    hooks.on_session_start = AsyncMock(return_value={"past_context": []})
    hooks.on_session_end   = AsyncMock(return_value={"saved": True})
    return hooks


# ── create_session ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_session_basic(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="Build an auth module")

    assert state.status     == "active"
    assert state.task       == "Build an auth module"
    assert state.session_id != ""
    assert isinstance(state.created_at, float)


@pytest.mark.asyncio
async def test_create_session_custom_id(sm):
    sid = "test-session-abc"
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="task", session_id=sid)
    assert state.session_id == sid


@pytest.mark.asyncio
async def test_create_session_with_models(sm, fake_redis):
    models = {"coder": "qwen2.5-coder:7b", "architect": "qwen2.5-coder:7b"}
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="task", models=models)

    assert state.models == models
    # session:models:{id} HASH key must also be written for RoutingPolicy
    stored = await fake_redis.hgetall(f"session:models:{state.session_id}")
    assert stored.get("coder") == "qwen2.5-coder:7b"


@pytest.mark.asyncio
async def test_create_session_persists_to_redis(sm, fake_redis):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="persist test")

    raw = await fake_redis.get(f"session:state:{state.session_id}")
    assert raw is not None
    loaded = SessionState.model_validate_json(raw)
    assert loaded.session_id == state.session_id
    assert loaded.status     == "active"


# ── get_session ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_session_returns_state(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        created = await sm.create_session(task="get test")
    fetched = await sm.get_session(created.session_id)
    assert fetched is not None
    assert fetched.session_id == created.session_id


@pytest.mark.asyncio
async def test_get_session_missing_returns_none(sm):
    result = await sm.get_session("does-not-exist")
    assert result is None


# ── update_session ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_session_status(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="update test")
    updated = await sm.update_session(state.session_id, status="paused")
    assert updated.status == "paused"
    # Verify persisted
    fetched = await sm.get_session(state.session_id)
    assert fetched.status == "paused"


@pytest.mark.asyncio
async def test_update_session_agent_ids_append(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="agent tracking test")
    await sm.update_session(state.session_id, agent_ids="agent-abc")
    await sm.update_session(state.session_id, agent_ids="agent-def")
    fetched = await sm.get_session(state.session_id)
    assert "agent-abc" in fetched.agent_ids
    assert "agent-def" in fetched.agent_ids


@pytest.mark.asyncio
async def test_update_session_no_duplicate_agent_ids(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="dedup test")
    await sm.update_session(state.session_id, agent_ids="agent-xyz")
    await sm.update_session(state.session_id, agent_ids="agent-xyz")   # duplicate
    fetched = await sm.get_session(state.session_id)
    assert fetched.agent_ids.count("agent-xyz") == 1


@pytest.mark.asyncio
async def test_update_session_missing_raises(sm):
    with pytest.raises(KeyError):
        await sm.update_session("nonexistent", status="paused")


# ── end_session ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_end_session(sm, mock_agent_mgr):
    hooks = _mock_hooks()
    with patch("session_manager.get_session_hooks", return_value=hooks):
        state   = await sm.create_session(task="end test")
        ended   = await sm.end_session(state.session_id, summary="all done")

    assert ended.status == "ended"
    hooks.on_session_end.assert_awaited_once()
    mock_agent_mgr.cleanup_idle_agents.assert_awaited_once()


@pytest.mark.asyncio
async def test_end_session_state_survives_in_redis(sm):
    """Session state must remain readable after end (natural TTL expiry)."""
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="survives test")
        await sm.end_session(state.session_id, summary="done")
    fetched = await sm.get_session(state.session_id)
    assert fetched is not None
    assert fetched.status == "ended"


@pytest.mark.asyncio
async def test_end_session_missing_raises(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        with pytest.raises(KeyError):
            await sm.end_session("ghost-session", summary="x")


# ── pause / resume ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pause_resume_roundtrip(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="pause test")
    paused  = await sm.pause_session(state.session_id)
    assert paused.status == "paused"
    resumed = await sm.resume_session(state.session_id)
    assert resumed.status == "active"


@pytest.mark.asyncio
async def test_resume_ended_session_raises(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="ended resume test")
        await sm.end_session(state.session_id, summary="done")
    with pytest.raises(ValueError, match="ended"):
        await sm.resume_session(state.session_id)


# ── configure_models ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_configure_models_existing_session(sm, fake_redis):
    """configure_models on existing session merges models and refreshes TTL."""
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="model config test")

    models = {"coder": "qwen2.5-coder:7b"}
    updated = await sm.configure_models(state.session_id, models)
    assert updated.models.get("coder") == "qwen2.5-coder:7b"

    # RoutingPolicy HASH key must be updated
    stored = await fake_redis.hgetall(f"session:models:{state.session_id}")
    assert stored.get("coder") == "qwen2.5-coder:7b"


@pytest.mark.asyncio
async def test_configure_models_no_session_returns_minimal_state(sm):
    """configure_models with no prior session:state returns a minimal SessionState."""
    sid   = "uncreated-session"
    state = await sm.configure_models(sid, {"reviewer": "qwen2.5-coder:7b"})
    assert state.session_id == sid
    assert state.models.get("reviewer") == "qwen2.5-coder:7b"
    # No session:state key was created — get_session returns None
    fetched = await sm.get_session(sid)
    assert fetched is None


# ── register_agent / register_task ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_agent(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="register test")
    await sm.register_agent(state.session_id, "coder-abc123")
    fetched = await sm.get_session(state.session_id)
    assert "coder-abc123" in fetched.agent_ids


@pytest.mark.asyncio
async def test_register_agent_no_session_noop(sm):
    """register_agent on a non-existent session is a silent no-op."""
    await sm.register_agent("ghost", "agent-xyz")   # must not raise


@pytest.mark.asyncio
async def test_register_task(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        state = await sm.create_session(task="task tracking")
    await sm.register_task(state.session_id, "t1")
    fetched = await sm.get_session(state.session_id)
    assert "t1" in fetched.task_ids


# ── list_sessions ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_sessions_all(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        s1 = await sm.create_session(task="session one")
        s2 = await sm.create_session(task="session two")
    sessions = await sm.list_sessions()
    ids = [s.session_id for s in sessions]
    assert s1.session_id in ids
    assert s2.session_id in ids


@pytest.mark.asyncio
async def test_list_sessions_filtered_by_status(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        active = await sm.create_session(task="active one")
        ended  = await sm.create_session(task="ended one")
        await sm.end_session(ended.session_id, summary="done")

    active_list = await sm.list_sessions(status="active")
    ended_list  = await sm.list_sessions(status="ended")

    active_ids = [s.session_id for s in active_list]
    ended_ids  = [s.session_id for s in ended_list]

    assert active.session_id in active_ids
    assert ended.session_id not in active_ids
    assert ended.session_id in ended_ids


@pytest.mark.asyncio
async def test_list_sessions_ordered_newest_first(sm):
    with patch("session_manager.get_session_hooks", return_value=_mock_hooks()):
        s1 = await sm.create_session(task="first")
        await asyncio.sleep(0.01)   # ensure different timestamps
        s2 = await sm.create_session(task="second")

    sessions = await sm.list_sessions()
    ids = [s.session_id for s in sessions]
    assert ids.index(s2.session_id) < ids.index(s1.session_id)


# ── WSEvent / models ──────────────────────────────────────────────────────────

def test_wsevent_serialisation():
    from models import WSEvent, WSEventType
    ev = WSEvent(
        type=WSEventType.WORK_COMPLETE,
        session_id="sess-123",
        agent_id="coder-abc",
        payload={"result_preview": "hello"},
    )
    j = ev.model_dump_json()
    loaded = WSEvent.model_validate_json(j)
    assert loaded.type       == WSEventType.WORK_COMPLETE
    assert loaded.session_id == "sess-123"
    assert loaded.payload["result_preview"] == "hello"


def test_agent_message_request():
    from models import AgentMessageRequest
    req = AgentMessageRequest(message="fix the auth bug", sender="user")
    assert req.message == "fix the auth bug"
    assert req.sender  == "user"


# ── send_message (AgentManager) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_to_running_agent():
    """send_message returns True for a running agent."""
    from agent_manager import AgentManager, Agent
    from memory_manager import MemoryManager

    mem = MagicMock(spec=MemoryManager)
    mgr = AgentManager(mem)

    agent            = mgr._new_agent("coder", "sess-1")
    agent.status     = "running"

    ok = await mgr.send_message(agent.agent_id, "please add tests")
    assert ok is True
    assert agent.inbox.qsize() == 1
    msg = agent.inbox.get_nowait()
    assert msg == "please add tests"


@pytest.mark.asyncio
async def test_send_message_to_terminal_agent_returns_false():
    from agent_manager import AgentManager
    from memory_manager import MemoryManager

    mem = MagicMock(spec=MemoryManager)
    mgr = AgentManager(mem)

    agent        = mgr._new_agent("coder", "sess-1")
    agent.status = "done"

    ok = await mgr.send_message(agent.agent_id, "too late")
    assert ok is False


@pytest.mark.asyncio
async def test_send_message_to_unknown_agent_returns_false():
    from agent_manager import AgentManager
    from memory_manager import MemoryManager

    mem = MagicMock(spec=MemoryManager)
    mgr = AgentManager(mem)
    ok  = await mgr.send_message("ghost-agent-xyz", "hello")
    assert ok is False


# ── subscribe_stream (AgentManager) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_stream_yields_chunks_and_terminates():
    """subscribe_stream yields outbox chunks until None sentinel."""
    from agent_manager import AgentManager
    from memory_manager import MemoryManager

    mem = MagicMock(spec=MemoryManager)
    mgr = AgentManager(mem)

    agent        = mgr._new_agent("coder", "sess-1")
    agent.status = "running"

    # Pre-fill outbox with chunks + sentinel
    await agent.outbox.put("chunk one ")
    await agent.outbox.put("chunk two")
    await agent.outbox.put(None)   # sentinel

    chunks = []
    async for chunk in mgr.subscribe_stream(agent.agent_id):
        chunks.append(chunk)

    assert chunks == ["chunk one ", "chunk two"]


@pytest.mark.asyncio
async def test_subscribe_stream_unknown_agent_returns_empty():
    from agent_manager import AgentManager
    from memory_manager import MemoryManager

    mem    = MagicMock(spec=MemoryManager)
    mgr    = AgentManager(mem)
    chunks = [c async for c in mgr.subscribe_stream("nobody")]
    assert chunks == []