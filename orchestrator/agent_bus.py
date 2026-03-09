"""
agent_bus.py — Inter-agent message bus for Phase 4B.3.

Transport split (matches build plan post-reviewer design):

  Agent → Architect (in-process):
    asyncio.Queue per session — zero infrastructure overhead, identical
    semantics to Redis pub/sub for single-node use. Architect agent loop
    calls subscribe_architect() and reacts to work_complete / work_failed
    without polling.

  Orchestrator → WebSocket → TUI (cross-process):
    Redis pub/sub on bus:session:{session_id}. This crosses the network
    boundary between the orchestrator process and the WebSocket client.
    _session_event_loop() in main.py subscribes here and forwards events
    to the connected TUI.

Token chunks are NEVER published to the bus. They flow exclusively through
agent.outbox → SSE endpoint → TUI agent pane. This keeps Redis out of the
per-token hot path entirely.

Phase 5 note: if multiple orchestrator nodes are ever needed, replace
asyncio.Queue with NATS JetStream in this file only. The AgentBus interface
(publish, subscribe_architect, subscribe_session, cleanup_session) is stable.

Redis key schema (canonical):
  bus:session:{session_id}  →  PUBSUB channel  JSON WSEvent
"""

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import redis.asyncio as aioredis

from models import WSEvent, WSEventType

log = logging.getLogger("agent_bus")

# Event types the architect cares about — everything except raw token events.
# INTERRUPT is included so user messages routed via the bus reach the architect.
_ARCHITECT_EVENTS = {
    WSEventType.WORK_COMPLETE,
    WSEventType.WORK_FAILED,
    WSEventType.PATCH_APPLIED,
    WSEventType.TEST_RESULT,
    WSEventType.DEBATE_POINT,
    WSEventType.INTERRUPT,
}

# Per-session in-process queue capacity. 256 events is generous for a
# single coding session; back-pressure here means the architect is blocked.
_QUEUE_MAXSIZE = 256


class AgentBus:
    """
    Dual-transport message bus.

    In-process queues carry agent→architect events within the orchestrator.
    Redis pub/sub carries all events to the WebSocket layer for TUI display.

    Both transports are written on every publish() call so the TUI always
    sees the same events the architect sees, with no additional wiring.
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis  = redis
        # Per-session asyncio.Queue for in-process routing
        self._queues: dict[str, asyncio.Queue] = {}

    # ── Internal queue management ─────────────────────────────────────────────

    def _get_queue(self, session_id: str) -> asyncio.Queue:
        """Get or create the in-process queue for a session."""
        if session_id not in self._queues:
            self._queues[session_id] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        return self._queues[session_id]

    # ── publish ───────────────────────────────────────────────────────────────

    async def publish(self, session_id: str, event: WSEvent) -> None:
        """
        Publish a structured event to both transports.

        1. In-process asyncio.Queue  — consumed by subscribe_architect()
        2. Redis pub/sub channel     — consumed by subscribe_session() → WebSocket

        Token events (WSEventType.TOKEN) are rejected at the gate — they must
        never enter the bus. Callers should use agent.outbox directly for tokens.

        Non-blocking: if the in-process queue is full (architect loop lagging),
        the event is logged and dropped rather than blocking the publishing agent.
        Redis pub/sub is fire-and-forget by nature.
        """
        if event.type == WSEventType.TOKEN:
            log.error(
                "BUG: TOKEN event must not be published to AgentBus "
                "(session=%s agent=%s) — use agent.outbox for tokens",
                session_id, event.agent_id,
            )
            return

        # 1. In-process queue
        q = self._get_queue(session_id)
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            log.warning(
                "agent bus queue full for session=%s — architect loop lagging, "
                "event type=%s dropped from in-process queue",
                session_id, event.type,
            )
            # Still publish to Redis so TUI sees the event even if architect missed it

        # 2. Redis pub/sub
        try:
            await self._redis.publish(
                f"bus:session:{session_id}",
                event.model_dump_json(),
            )
        except Exception as e:
            log.warning(
                "bus Redis publish failed session=%s type=%s: %s",
                session_id, event.type, e,
            )

    # ── subscribe_architect ───────────────────────────────────────────────────

    async def subscribe_architect(
        self, session_id: str
    ) -> AsyncIterator[WSEvent]:
        """
        Async generator for the architect agent loop.

        Yields only events the architect needs to act on:
          work_complete, work_failed, patch_applied, test_result,
          debate_point, interrupt

        Blocks on the in-process queue — no polling, no Redis round-trip.
        Exits when a None sentinel is received (published by cleanup_session()).
        """
        q = self._get_queue(session_id)
        while True:
            event = await q.get()
            if event is None:
                # Sentinel — session ended, architect loop should exit
                log.debug("architect subscribe: session=%s received sentinel", session_id)
                break
            if event.type in _ARCHITECT_EVENTS:
                yield event

    # ── subscribe_session ─────────────────────────────────────────────────────

    async def subscribe_session(
        self, session_id: str
    ) -> AsyncIterator[WSEvent]:
        """
        Async generator for the WebSocket handler (_session_event_loop).

        Subscribes to the Redis pub/sub channel for this session and yields
        all WSEvents as they arrive. Used exclusively by the WebSocket path —
        never by in-process agent loops.

        Exits when:
          - A STATUS event with payload {"lifecycle": "ended"} is received
          - The Redis connection drops (logs error, exits cleanly)

        The caller (ws_session in main.py) handles WebSocketDisconnect
        independently via its own try/except — this generator doesn't need to.
        """
        channel_name = f"bus:session:{session_id}"
        pubsub       = self._redis.pubsub()

        try:
            await pubsub.subscribe(channel_name)
            log.info("bus: subscribed to %s", channel_name)

            async for raw_msg in pubsub.listen():
                if raw_msg["type"] != "message":
                    continue
                try:
                    event = WSEvent.model_validate_json(raw_msg["data"])
                except Exception as e:
                    log.warning("bus: invalid WSEvent on %s: %s", channel_name, e)
                    continue

                yield event

                # Session-ended sentinel from the WebSocket side
                if (event.type == WSEventType.STATUS
                        and event.payload.get("lifecycle") == "ended"):
                    log.info("bus: session=%s ended, closing subscriber", session_id)
                    break

        except Exception as e:
            log.error("bus: subscribe_session error session=%s: %s", session_id, e)
        finally:
            try:
                await pubsub.unsubscribe(channel_name)
                await pubsub.aclose()
            except Exception:
                pass

    # ── cleanup_session ───────────────────────────────────────────────────────

    def cleanup_session(self, session_id: str) -> None:
        """
        Remove the in-process queue for a session and push a None sentinel
        so any active subscribe_architect() generator exits cleanly.

        Called by SessionManager.end_session() when a session ends.
        Also publishes a STATUS/ended event to Redis so subscribe_session()
        generators on the WebSocket side exit too.
        """
        q = self._queues.pop(session_id, None)
        if q is not None:
            try:
                q.put_nowait(None)   # sentinel for subscribe_architect()
            except asyncio.QueueFull:
                pass   # architect already exited
        log.info("bus: cleaned up session=%s", session_id)


# ── Singleton ─────────────────────────────────────────────────────────────────

_bus: Optional[AgentBus] = None


def get_agent_bus() -> AgentBus:
    if _bus is None:
        raise RuntimeError("AgentBus not initialised — call init_agent_bus() first")
    return _bus


def init_agent_bus(redis: aioredis.Redis) -> AgentBus:
    global _bus
    _bus = AgentBus(redis)
    log.info("AgentBus initialised")
    return _bus