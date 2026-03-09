"""
tests/unit/test_agent_bus.py — Unit tests for Phase 4B.3 AgentBus.

All tests use FakeRedis — no real Redis or model backend needed.
Covers:
  - publish() writes to in-process queue AND Redis pub/sub
  - publish() rejects TOKEN events at the gate
  - publish() handles full in-process queue gracefully (logs, doesn't crash)
  - publish() handles Redis failure gracefully (logs, doesn't crash)
  - subscribe_architect() yields only architect-relevant event types
  - subscribe_architect() exits on None sentinel
  - cleanup_session() pushes sentinel to in-process queue
  - cleanup_session() removes queue from registry
  - _get_queue() creates queue on first access, reuses on second
  - patch_queue.set_bus() wires bus; PATCH_APPLIED published on apply
  - session_manager.end_session() publishes STATUS/ended and cleans up bus
  - TOKEN event never reaches bus (guarded in publish)
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "orchestrator"))

from models import WSEvent, WSEventType


# ── Fake Redis ────────────────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self._published: list[tuple[str, str]] = []   # (channel, message)
        self._store: dict = {}
        self.fail_publish = False

    async def publish(self, channel: str, message: str):
        if self.fail_publish:
            raise ConnectionError("Redis down")
        self._published.append((channel, message))
        return 1

    def pubsub(self):
        return FakePubSub(self)

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)

    async def hset(self, key, mapping=None, **kw):
        pass

    async def expire(self, key, ttl):
        return 1

    async def scan(self, cursor=0, match="*", count=100):
        return 0, []

    def pipeline(self):
        return FakePipeline(self._store)


class FakePipeline:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def set(self, k, v, ex=None):
        self._ops.append(("set", k, v))
        return self

    def expire(self, k, ttl):
        return self

    async def execute(self):
        for op, *args in self._ops:
            if op == "set":
                self._store[args[0]] = args[1]
        return [True] * len(self._ops)

    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass


class FakePubSub:
    """Minimal pub/sub stub — subscribe_session() tests use this."""
    def __init__(self, redis: FakeRedis):
        self._redis    = redis
        self._channel  = None
        self._messages = []   # pre-loaded by tests

    async def subscribe(self, channel: str):
        self._channel = channel

    async def unsubscribe(self, channel: str):
        pass

    async def aclose(self):
        pass

    async def listen(self):
        for msg in self._messages:
            yield msg


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def bus(fake_redis):
    from agent_bus import AgentBus
    return AgentBus(fake_redis)


def _event(etype=WSEventType.WORK_COMPLETE, session="sess-1", agent="coder-abc"):
    return WSEvent(type=etype, session_id=session, agent_id=agent, payload={})


# ── _get_queue ────────────────────────────────────────────────────────────────

def test_get_queue_creates_on_first_access(bus):
    q = bus._get_queue("sess-1")
    assert q is not None
    assert isinstance(q, asyncio.Queue)


def test_get_queue_reuses_existing(bus):
    q1 = bus._get_queue("sess-1")
    q2 = bus._get_queue("sess-1")
    assert q1 is q2


def test_get_queue_separate_per_session(bus):
    q1 = bus._get_queue("sess-1")
    q2 = bus._get_queue("sess-2")
    assert q1 is not q2


# ── publish ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_publish_puts_event_on_in_process_queue(bus):
    ev = _event()
    await bus.publish("sess-1", ev)
    q = bus._get_queue("sess-1")
    assert q.qsize() == 1
    got = q.get_nowait()
    assert got.type == WSEventType.WORK_COMPLETE


@pytest.mark.asyncio
async def test_publish_sends_to_redis_pubsub(bus, fake_redis):
    ev = _event()
    await bus.publish("sess-1", ev)
    assert len(fake_redis._published) == 1
    channel, payload = fake_redis._published[0]
    assert channel == "bus:session:sess-1"
    parsed = WSEvent.model_validate_json(payload)
    assert parsed.type == WSEventType.WORK_COMPLETE


@pytest.mark.asyncio
async def test_publish_rejects_token_events(bus, fake_redis):
    ev = _event(etype=WSEventType.TOKEN)
    await bus.publish("sess-1", ev)
    # Must not appear in queue or Redis
    q = bus._get_queue("sess-1")
    assert q.empty()
    assert len(fake_redis._published) == 0


@pytest.mark.asyncio
async def test_publish_queue_full_still_publishes_to_redis(bus, fake_redis):
    """When in-process queue is full, Redis pub/sub still receives the event."""
    # Fill the queue to capacity
    q = bus._get_queue("sess-1")
    for _ in range(256):
        await q.put(_event())

    ev = _event(etype=WSEventType.WORK_FAILED)
    await bus.publish("sess-1", ev)   # must not raise

    # Redis should still have received it
    assert any(ch == "bus:session:sess-1" for ch, _ in fake_redis._published)


@pytest.mark.asyncio
async def test_publish_redis_failure_does_not_crash(bus, fake_redis):
    """Redis publish failure must be logged and swallowed, never propagated."""
    fake_redis.fail_publish = True
    ev = _event()
    await bus.publish("sess-1", ev)   # must not raise
    # In-process queue still received the event
    q = bus._get_queue("sess-1")
    assert q.qsize() == 1


# ── subscribe_architect ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_architect_yields_work_complete(bus):
    ev = _event(etype=WSEventType.WORK_COMPLETE)
    await bus.publish("sess-1", ev)
    await bus._get_queue("sess-1").put(None)   # sentinel to terminate

    received = []
    async for e in bus.subscribe_architect("sess-1"):
        received.append(e)

    assert len(received) == 1
    assert received[0].type == WSEventType.WORK_COMPLETE


@pytest.mark.asyncio
async def test_subscribe_architect_yields_patch_applied(bus):
    ev = _event(etype=WSEventType.PATCH_APPLIED)
    await bus.publish("sess-1", ev)
    await bus._get_queue("sess-1").put(None)

    received = [e async for e in bus.subscribe_architect("sess-1")]
    assert len(received) == 1
    assert received[0].type == WSEventType.PATCH_APPLIED


@pytest.mark.asyncio
async def test_subscribe_architect_skips_status_events(bus):
    """STATUS events are not in _ARCHITECT_EVENTS and must be filtered out."""
    await bus.publish("sess-1", _event(etype=WSEventType.STATUS))
    await bus._get_queue("sess-1").put(None)

    received = [e async for e in bus.subscribe_architect("sess-1")]
    assert received == []


@pytest.mark.asyncio
async def test_subscribe_architect_exits_on_sentinel(bus):
    """None sentinel must terminate the subscribe_architect generator."""
    await bus.publish("sess-1", _event(etype=WSEventType.WORK_COMPLETE))
    # sentinel already in queue via cleanup_session
    bus.cleanup_session("sess-1")

    # subscribe_architect on a cleaned-up session: queue has sentinel, exits
    q = asyncio.Queue()
    await q.put(_event(etype=WSEventType.WORK_COMPLETE))
    await q.put(None)
    bus._queues["sess-2"] = q

    received = [e async for e in bus.subscribe_architect("sess-2")]
    assert len(received) == 1


@pytest.mark.asyncio
async def test_subscribe_architect_multiple_events_in_order(bus):
    events = [
        _event(etype=WSEventType.WORK_COMPLETE),
        _event(etype=WSEventType.PATCH_APPLIED),
        _event(etype=WSEventType.WORK_FAILED),
    ]
    for ev in events:
        await bus.publish("sess-1", ev)
    await bus._get_queue("sess-1").put(None)

    received = [e async for e in bus.subscribe_architect("sess-1")]
    assert [e.type for e in received] == [
        WSEventType.WORK_COMPLETE,
        WSEventType.PATCH_APPLIED,
        WSEventType.WORK_FAILED,
    ]


# ── cleanup_session ───────────────────────────────────────────────────────────

def test_cleanup_session_removes_queue(bus):
    bus._get_queue("sess-1")
    assert "sess-1" in bus._queues
    bus.cleanup_session("sess-1")
    assert "sess-1" not in bus._queues


def test_cleanup_session_pushes_sentinel(bus):
    q = bus._get_queue("sess-1")
    bus.cleanup_session("sess-1")
    # Queue was popped from registry but sentinel was pushed before removal
    # We need to check by consuming it — re-create a reference first
    # (cleanup_session pops the queue, so grab it before calling)
    q2 = asyncio.Queue()
    bus._queues["sess-2"] = q2
    bus.cleanup_session("sess-2")
    assert q2.qsize() == 1
    assert q2.get_nowait() is None


def test_cleanup_session_noop_for_unknown_session(bus):
    """Cleaning up a session that has no queue must not raise."""
    bus.cleanup_session("never-existed")   # must not raise


# ── subscribe_session (Redis pub/sub path) ────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_session_yields_events_from_redis(bus, fake_redis):
    """
    subscribe_session() reads from FakePubSub._messages.
    We pre-load messages and verify they are yielded as WSEvents.
    """
    ev = _event(etype=WSEventType.WORK_COMPLETE)

    # Pre-load the pubsub with one event then a STATUS/ended terminator
    ended_ev = WSEvent(
        type=WSEventType.STATUS,
        session_id="sess-1",
        payload={"lifecycle": "ended"},
    )
    fake_redis.pubsub()._messages = [
        {"type": "message", "data": ev.model_dump_json()},
        {"type": "message", "data": ended_ev.model_dump_json()},
    ]

    # Patch pubsub() on the bus's redis so our pre-loaded stub is used
    fake_pubsub         = FakePubSub(fake_redis)
    fake_pubsub._messages = [
        {"type": "message", "data": ev.model_dump_json()},
        {"type": "message", "data": ended_ev.model_dump_json()},
    ]
    bus._redis = MagicMock()
    bus._redis.pubsub = MagicMock(return_value=fake_pubsub)
    bus._redis.publish = AsyncMock()

    received = [e async for e in bus.subscribe_session("sess-1")]

    # Should receive work_complete then status/ended (which triggers exit)
    assert received[0].type == WSEventType.WORK_COMPLETE
    assert received[1].type == WSEventType.STATUS
    assert received[1].payload["lifecycle"] == "ended"


@pytest.mark.asyncio
async def test_subscribe_session_skips_non_message_types(bus):
    """subscribe_session must ignore subscribe/unsubscribe confirmation messages."""
    ev = _event(etype=WSEventType.PATCH_APPLIED)
    ended = WSEvent(type=WSEventType.STATUS, session_id="s", payload={"lifecycle": "ended"})

    fake_pubsub = FakePubSub(None)
    fake_pubsub._messages = [
        {"type": "subscribe",   "data": None},       # confirmation — skip
        {"type": "unsubscribe", "data": None},       # confirmation — skip
        {"type": "message",     "data": ev.model_dump_json()},
        {"type": "message",     "data": ended.model_dump_json()},
    ]
    bus._redis = MagicMock()
    bus._redis.pubsub = MagicMock(return_value=fake_pubsub)
    bus._redis.publish = AsyncMock()

    received = [e async for e in bus.subscribe_session("s")]
    assert received[0].type == WSEventType.PATCH_APPLIED   # confirmation rows skipped


# ── patch_queue integration ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_patch_queue_set_bus_publishes_patch_applied(fake_redis):
    """set_bus() wires bus; PATCH_APPLIED is published when patch applies."""
    from patch_queue import PatchQueue
    from agent_bus import AgentBus

    bus = AgentBus(fake_redis)
    pq  = PatchQueue()
    pq.set_bus(bus)

    # Simulate a successful apply by calling _publish_patch_applied directly
    from patch_queue import Patch
    p = Patch(diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
              agent_id="coder-1", task_id="t1",
              session_id="sess-1", description="test patch")

    await pq._publish_patch_applied(p)

    assert len(fake_redis._published) == 1
    channel, payload = fake_redis._published[0]
    assert channel == "bus:session:sess-1"
    ev = WSEvent.model_validate_json(payload)
    assert ev.type       == WSEventType.PATCH_APPLIED
    assert ev.session_id == "sess-1"
    assert ev.agent_id   == "coder-1"
    assert ev.payload["patch_id"] == p.patch_id


@pytest.mark.asyncio
async def test_patch_queue_publish_failure_does_not_affect_result(fake_redis):
    """Bus failure in _publish_patch_applied must not propagate."""
    from patch_queue import PatchQueue, Patch
    from agent_bus import AgentBus

    fake_redis.fail_publish = True
    bus = AgentBus(fake_redis)
    pq  = PatchQueue()
    pq.set_bus(bus)

    p = Patch(diff="--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
              agent_id="a", task_id="t", session_id="s")
    await pq._publish_patch_applied(p)   # must not raise


# ── session_manager integration ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_end_session_publishes_status_ended_and_cleans_bus():
    """end_session() must publish STATUS/ended and call bus.cleanup_session()."""
    from session_manager import SessionManager, SessionState
    import time

    fake_redis = FakeRedis()

    # Pre-populate session state so end_session finds it
    sid   = "sess-end-test"
    state = SessionState(session_id=sid, task="test", status="active")
    fake_redis._store[f"session:state:{sid}"] = state.model_dump_json()

    mock_agent_mgr = MagicMock()
    mock_agent_mgr.cleanup_idle_agents = AsyncMock(return_value=0)

    mock_bus = MagicMock()
    mock_bus.publish       = AsyncMock()
    mock_bus.cleanup_session = MagicMock()

    sm = SessionManager(fake_redis, mock_agent_mgr, bus=mock_bus)

    with patch("session_manager.get_session_hooks") as mock_hooks_getter:
        hooks = MagicMock()
        hooks.on_session_end = AsyncMock(return_value={"saved": True})
        mock_hooks_getter.return_value = hooks
        await sm.end_session(sid, summary="done")

    # STATUS/ended event must have been published
    mock_bus.publish.assert_awaited_once()
    call_args = mock_bus.publish.call_args
    published_event = call_args[0][1]   # second positional arg
    assert published_event.type == WSEventType.STATUS
    assert published_event.payload["lifecycle"] == "ended"

    # cleanup_session must have been called
    mock_bus.cleanup_session.assert_called_once_with(sid)