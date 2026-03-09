"""
session_manager.py — Long-lived session lifecycle management.

Phase 4B.1:
  Sessions are Redis-backed and survive orchestrator restarts.
  A session owns:
    - Its state (status, timestamps, model assignments, agent/task IDs)
    - The model assignments for each role (session:models:{id})
    - References to the agents and tasks that ran under it

  Key schema (canonical — matches build plan):
    session:state:{session_id}   → JSON SessionState  (TTL SESSION_TTL)
    session:models:{session_id}  → HASH role→model    (TTL SESSION_TTL, co-managed here)

  Both keys always have the same TTL. Any operation that writes or reads
  session:state also refreshes session:models TTL so they never diverge.

  SessionHooks (on_session_start / on_session_end) are called internally
  from create_session() / end_session() — they are NOT replaced.
"""

import json
import logging
import time
import uuid
from typing import Optional

import redis.asyncio as aioredis
from pydantic import BaseModel, Field

import config
from agent_manager import AgentManager
from session_hooks import get_session_hooks

log = logging.getLogger("session_manager")

_STATE_PREFIX  = "session:state:"
_MODELS_PREFIX = "session:models:"


# ── SessionState schema ───────────────────────────────────────────────────────

class SessionState(BaseModel):
    session_id: str
    status:     str = "active"    # "active" | "paused" | "ended"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    task:       str   = ""
    models:     dict[str, str] = Field(default_factory=dict)  # role → model_name
    agent_ids:  list[str]      = Field(default_factory=list)
    task_ids:   list[str]      = Field(default_factory=list)
    metadata:   dict           = Field(default_factory=dict)


# ── SessionManager ────────────────────────────────────────────────────────────

class SessionManager:
    def __init__(self, redis: aioredis.Redis, agent_mgr: AgentManager, bus=None):
        self._redis     = redis
        self._agent_mgr = agent_mgr
        self._bus       = bus   # Phase 4B.3 — AgentBus, optional

    # ── Internal Redis helpers ────────────────────────────────────────────────

    def _state_key(self, session_id: str) -> str:
        return f"{_STATE_PREFIX}{session_id}"

    def _models_key(self, session_id: str) -> str:
        return f"{_MODELS_PREFIX}{session_id}"

    async def _write_state(self, state: SessionState) -> None:
        """
        Persist SessionState to Redis and refresh session:models TTL.
        Both keys always share SESSION_TTL so they never diverge.
        """
        state.updated_at = time.time()
        pipe = self._redis.pipeline()
        pipe.set(
            self._state_key(state.session_id),
            state.model_dump_json(),
            ex=config.SESSION_TTL,
        )
        # Refresh models TTL every time state is written — keeps them in sync.
        # If no models key exists yet (no configure call made) this is a no-op.
        pipe.expire(self._models_key(state.session_id), config.SESSION_TTL)
        await pipe.execute()

    async def _read_state(self, session_id: str) -> Optional[SessionState]:
        raw = await self._redis.get(self._state_key(session_id))
        if not raw:
            return None
        try:
            return SessionState.model_validate_json(raw)
        except Exception as e:
            log.warning("corrupt session state for %s: %s", session_id, e)
            return None

    # ── create_session ────────────────────────────────────────────────────────

    async def create_session(
        self,
        task:       str,
        session_id: str | None = None,
        models:     dict[str, str] | None = None,
        metadata:   dict | None = None,
    ) -> SessionState:
        """
        Create a new session, optionally pre-configuring role→model assignments.

        Calls SessionHooks.on_session_start() for past-context recall.
        If models are provided, writes them to session:models:{id} immediately
        with SESSION_TTL so routing is available before the first agent runs.
        """
        sid   = session_id or str(uuid.uuid4())
        hooks = get_session_hooks()

        # Pull past context for this task (non-blocking — hooks handles errors)
        await hooks.on_session_start(sid, task)

        state = SessionState(
            session_id=sid,
            status="active",
            task=task,
            models=models or {},
            metadata=metadata or {},
        )

        # If models were provided, write them to the HASH key so RoutingPolicy
        # can resolve them immediately, then _write_state refreshes the TTL.
        if models:
            await self._redis.hset(self._models_key(sid), mapping=models)

        await self._write_state(state)
        log.info("session created  id=%s  task=%r  models=%s", sid, task[:80], list((models or {}).keys()))
        return state

    # ── get_session ───────────────────────────────────────────────────────────

    async def get_session(self, session_id: str) -> Optional[SessionState]:
        return await self._read_state(session_id)

    # ── update_session ────────────────────────────────────────────────────────

    async def update_session(self, session_id: str, **kwargs) -> SessionState:
        """
        Patch arbitrary fields on an existing session.

        Also refreshes session:models TTL — any update extends the session
        lifetime uniformly. Raises KeyError if the session does not exist.
        """
        state = await self._read_state(session_id)
        if state is None:
            raise KeyError(f"Session {session_id!r} not found")

        allowed = {"status", "task", "models", "agent_ids", "task_ids", "metadata"}
        for k, v in kwargs.items():
            if k not in allowed:
                log.warning("update_session: ignoring unknown field %r", k)
                continue
            # For list fields (agent_ids, task_ids) support append-style updates
            if k in ("agent_ids", "task_ids") and isinstance(v, str):
                current = getattr(state, k)
                if v not in current:
                    current.append(v)
                setattr(state, k, current)
            else:
                setattr(state, k, v)

        await self._write_state(state)
        return state

    # ── configure_models ─────────────────────────────────────────────────────

    async def configure_models(
        self, session_id: str, models: dict[str, str]
    ) -> SessionState:
        """
        Set or update role→model assignments for a session.

        If the session already exists, updates it and refreshes both TTLs.
        If not, the caller is responsible for creating the session first.
        This is called by POST /v1/session/configure in main.py.
        """
        # Write models to the HASH key first
        await self._redis.hset(self._models_key(session_id), mapping=models)

        state = await self._read_state(session_id)
        if state is not None:
            # Existing session — merge models and refresh
            state.models.update(models)
            await self._write_state(state)   # also refreshes models TTL
            log.info("session models updated  id=%s  roles=%s", session_id, list(models.keys()))
            return state
        else:
            # No session state exists yet — set just the TTL on the models key
            # and return a minimal state. The session:state key will be written
            # when create_session() is called (or on first agent spawn).
            await self._redis.expire(self._models_key(session_id), config.SESSION_TTL)
            log.info(
                "session:models written for uncreated session %s — "
                "call create_session() or POST /v1/sessions to persist full state",
                session_id,
            )
            return SessionState(session_id=session_id, models=models)

    # ── register_agent ────────────────────────────────────────────────────────

    async def register_agent(self, session_id: str, agent_id: str) -> None:
        """
        Add an agent_id to the session's agent_ids list.
        No-op if the session doesn't exist (agent spawned outside a managed session).
        """
        state = await self._read_state(session_id)
        if state is None:
            return
        if agent_id not in state.agent_ids:
            state.agent_ids.append(agent_id)
            await self._write_state(state)

    # ── register_task ─────────────────────────────────────────────────────────

    async def register_task(self, session_id: str, task_id: str) -> None:
        """Add a task_id to the session's task_ids list."""
        state = await self._read_state(session_id)
        if state is None:
            return
        if task_id not in state.task_ids:
            state.task_ids.append(task_id)
            await self._write_state(state)

    # ── end_session ───────────────────────────────────────────────────────────

    async def end_session(
        self,
        session_id: str,
        summary:    str = "",
        transcript: list[dict] | None = None,
        failures:   list[dict] | None = None,
    ) -> SessionState:
        """
        Mark session as ended, call SessionHooks.on_session_end(), trigger
        agent cleanup. The session state key is kept in Redis (it will expire
        naturally via SESSION_TTL) so history is queryable after the session ends.
        """
        state = await self._read_state(session_id)
        if state is None:
            raise KeyError(f"Session {session_id!r} not found")

        state.status = "ended"
        await self._write_state(state)

        # Delegate to hooks for memory save + skill/antipattern extraction
        hooks = get_session_hooks()
        await hooks.on_session_end(
            session_id=session_id,
            summary=summary or state.task,
            transcript=transcript or [],
            failures=failures or [],
        )

        # Prune idle agents that ran under this session
        removed = await self._agent_mgr.cleanup_idle_agents()

        # Phase 4B.3: release in-process bus queue and signal WebSocket subscriber
        if self._bus is not None:
            from models import WSEvent, WSEventType
            # Publish STATUS/ended so subscribe_session() generator exits cleanly
            try:
                await self._bus.publish(session_id, WSEvent(
                    type=WSEventType.STATUS,
                    session_id=session_id,
                    payload={"lifecycle": "ended"},
                ))
            except Exception as e:
                log.warning("bus STATUS/ended publish failed session=%s: %s", session_id, e)
            self._bus.cleanup_session(session_id)

        log.info(
            "session ended  id=%s  agents_pruned=%d", session_id, removed
        )
        return state

    # ── pause / resume ────────────────────────────────────────────────────────

    async def pause_session(self, session_id: str) -> SessionState:
        """Pause an active session. Agents keep their state; no new tasks run."""
        return await self.update_session(session_id, status="paused")

    async def resume_session(self, session_id: str) -> SessionState:
        """Resume a paused session."""
        state = await self._read_state(session_id)
        if state is None:
            raise KeyError(f"Session {session_id!r} not found")
        if state.status == "ended":
            raise ValueError(f"Session {session_id!r} has ended and cannot be resumed")
        return await self.update_session(session_id, status="active")

    # ── list_sessions ─────────────────────────────────────────────────────────

    async def list_sessions(
        self, status: str | None = None
    ) -> list[SessionState]:
        """
        Scan Redis for all session:state:* keys and return their states.

        Filtered by status if provided. Ordered by created_at descending
        (newest first). This uses SCAN — safe for production Redis, no KEYS.
        """
        states: list[SessionState] = []
        cursor = 0
        pattern = f"{_STATE_PREFIX}*"
        while True:
            cursor, keys = await self._redis.scan(
                cursor=cursor, match=pattern, count=100
            )
            for key in keys:
                raw = await self._redis.get(key)
                if not raw:
                    continue
                try:
                    s = SessionState.model_validate_json(raw)
                    states.append(s)
                except Exception as e:
                    log.warning("skipping corrupt session key %s: %s", key, e)
            if cursor == 0:
                break

        if status:
            states = [s for s in states if s.status == status]

        states.sort(key=lambda s: s.created_at, reverse=True)
        return states


# ── Singleton ─────────────────────────────────────────────────────────────────

_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    if _session_manager is None:
        raise RuntimeError(
            "SessionManager not initialised — call init_session_manager() first"
        )
    return _session_manager


def init_session_manager(
    redis: aioredis.Redis, agent_mgr: AgentManager, bus=None
) -> SessionManager:
    global _session_manager
    _session_manager = SessionManager(redis, agent_mgr, bus=bus)
    log.info("SessionManager initialised")
    return _session_manager