"""
main.py — Orchestrator entry point.

Phase 3.5 wiring changes:
  - patch_queue.set_redis() called in lifespan (enables persistence)
  - task_queue._redis reused for patch persistence (same connection)
  - GET /v1/agents/cleanup — trigger idle agent pruning on demand
  - AGENTS_DIR passed via config (not hardcoded)
  - New env vars surfaced in /status: queue depth, cache size

Phase 4A.1 additions:
  - ModelRegistry initialised and detect_available() called in lifespan
  - GET /v1/models/catalog — full catalog with on_disk status
  - GET /v1/models/for-role — filtered by role tag affinity
  - POST /v1/models/pull — pull Ollama model (rejects if agents running)

Phase 4A.2 additions:
  - RoutingPolicy initialised in lifespan, wired into router
  - AgentManager receives redis reference
  - POST /v1/session/configure — per-session role→model assignment
  - GET /v1/session/models — query current session model map

Phase 4B.1 additions:
  - SessionManager initialised in lifespan
  - POST /v1/session/configure updated: uses SESSION_TTL, calls
    session_manager.configure_models() so existing sessions are updated
    atomically and both key TTLs stay in sync
  - GET  /v1/sessions              — list sessions (optional ?status= filter)
  - GET  /v1/sessions/{session_id} — get session state
  - POST /v1/sessions              — create session (replaces bare /v1/session/start)
  - POST /v1/sessions/{session_id}/end   — end session with summary
  - POST /v1/sessions/{session_id}/pause — pause active session
  - POST /v1/sessions/{session_id}/resume — resume paused session
  - POST /v1/agents/{agent_id}/message — send message to specific agent inbox

Phase 4B.2 additions:
  - GET  /v1/agents/{agent_id}/stream — SSE token stream. Polls up to 2s for
    agent registration (handles TUI race on fast clients), then streams
    agent.outbox token-by-token, closing with a "data: [DONE]" sentinel
  - WS   /ws/session/{session_id} — full-duplex session WebSocket. Yields
    structured WSEvent JSON as agents complete work. Heartbeat ping every
    WS_HEARTBEAT_INTERVAL seconds to prevent proxy idle-timeout drops.
    _session_event_loop() is a stub here — wired to AgentBus in 4B.3
  - version bumped to 0.5.0 (no change — 4B.1 and 4B.2 ship together)
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, AsyncGenerator

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse
from sse_starlette.sse import EventSourceResponse

import config
import router
from agent_bus import init_agent_bus, get_agent_bus            # Phase 4B.3
from agent_manager import init_agent_manager, get_agent_manager
from routing_policy import init_routing_policy
from command_parser import parse as parse_command, help_text
from debate_engine import init_debate_engine, get_debate_engine
from file_watcher import file_watcher
from fine_tune_collector import get_stats, read_records, clear_records
from memory_manager import memory
from metrics import metrics
from model_registry import init_model_registry, get_model_registry
from models import ChatCompletionRequest, AgentMessageRequest, WSEvent, WSEventType
from patch_queue import patch_queue, PatchValidationError
from session_hooks import init_session_hooks, get_session_hooks
from session_manager import init_session_manager, get_session_manager   # Phase 4B.1
from skill_loader import skill_loader
from task_queue import task_queue
from webhook_handler import (
    verify_signature, WebhookSignatureError,
    handle_workflow_run, handle_issue_opened,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("orchestrator")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("connecting to ChromaDB and Redis...")
    await memory.connect()
    await task_queue.connect()

    # Phase 2.1 — wire patch_queue into task_queue
    task_queue.set_patch_queue(patch_queue)

    # Phase 3.5 — wire Redis into patch_queue for crash-safe persistence
    patch_queue.set_redis(task_queue._redis)

    # Phase 4A.2 — routing policy (needs Redis from task_queue)
    policy = init_routing_policy(task_queue._redis)
    router.set_policy(policy)

    # Phase 4A.2 — agent manager gets Redis ref for session model lookups
    mgr = init_agent_manager(memory, redis=task_queue._redis)
    init_debate_engine(mgr)
    init_session_hooks(memory)
    skill_loader.load()

    # Phase 4A.1 — model registry
    registry = init_model_registry()
    await registry.detect_available()

    # Phase 4B.3 — agent bus (depends on redis; wired into agent_mgr + patch_queue)
    bus = init_agent_bus(task_queue._redis)
    mgr.set_bus(bus)
    patch_queue.set_bus(bus)

    # Phase 4B.1 — session manager (bus passed so end_session can cleanup)
    init_session_manager(task_queue._redis, mgr, bus=bus)

    # Phase 2.4 — file watcher
    await file_watcher.start(task_queue._redis)

    log.info("all systems ready  profile=%s  version=%s", config.PROFILE, app.version)
    yield

    # Shutdown
    await file_watcher.stop()
    await get_model_registry().close()
    await memory.close()
    await task_queue.close()


app = FastAPI(title="AI Coding Agent Orchestrator", version="0.5.0", lifespan=lifespan)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "profile": config.PROFILE, "version": "0.5.0"}


# ═════════════════════════════════════════════════════════════════════════════
# Phase 4B.1 — Session management endpoints
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/v1/sessions")
async def list_sessions(status: str = Query(None, description="Filter by status: active|paused|ended")):
    """
    List all sessions tracked in Redis.
    Ordered by created_at descending (newest first).
    Optional ?status= filter: active | paused | ended
    """
    valid_statuses = {None, "active", "paused", "ended"}
    if status not in valid_statuses:
        raise HTTPException(400, f"Invalid status filter {status!r}. Must be one of: active, paused, ended")
    sessions = await get_session_manager().list_sessions(status=status)
    return {
        "sessions": [s.model_dump() for s in sessions],
        "count":    len(sessions),
        "filter":   status,
    }


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str):
    """Get the current state of a session."""
    state = await get_session_manager().get_session(session_id)
    if state is None:
        raise HTTPException(404, f"Session {session_id!r} not found")
    return state.model_dump()


@app.post("/v1/sessions")
async def create_session(body: dict):
    """
    Create a new managed session.

    Body:
      task       str   — required. The high-level task for this session.
      session_id str   — optional. Auto-generated UUID if absent.
      models     dict  — optional. role→model overrides (same as /v1/session/configure).
      metadata   dict  — optional. Arbitrary key-value metadata.

    Calls SessionHooks.on_session_start() for past-context recall.
    Returns the full SessionState.
    """
    task = body.get("task", "").strip()
    if not task:
        raise HTTPException(400, "task is required")

    session_id = body.get("session_id") or str(uuid.uuid4())
    models     = body.get("models") or {}
    metadata   = body.get("metadata") or {}

    # Validate model names if provided
    if models:
        registry  = get_model_registry()
        catalog   = {m["name"] for m in registry.catalog_with_status()}
        bad = [m for m in models.values() if m not in catalog]
        if bad:
            raise HTTPException(
                400,
                f"Unknown model(s): {bad}. Check GET /v1/models/catalog for valid names.",
            )

    state = await get_session_manager().create_session(
        task=task,
        session_id=session_id,
        models=models or None,
        metadata=metadata,
    )
    log.info("POST /v1/sessions  id=%s", state.session_id)
    return state.model_dump()


@app.post("/v1/sessions/{session_id}/end")
async def end_session_managed(session_id: str, body: dict = {}):
    """
    End a managed session.

    Body (all optional):
      summary    str        — human-readable session summary for memory.
      transcript list[dict] — conversation turns for skill extraction.
      failures   list[dict] — failure records for antipattern mining.

    Triggers SessionHooks.on_session_end() and cleans up idle agents.
    Session state remains readable in Redis until SESSION_TTL expires.
    """
    try:
        state = await get_session_manager().end_session(
            session_id=session_id,
            summary=body.get("summary", ""),
            transcript=body.get("transcript", []),
            failures=body.get("failures", []),
        )
    except KeyError:
        raise HTTPException(404, f"Session {session_id!r} not found")
    return state.model_dump()


@app.post("/v1/sessions/{session_id}/pause")
async def pause_session(session_id: str):
    """Pause an active session. Queued tasks are preserved."""
    try:
        state = await get_session_manager().pause_session(session_id)
    except KeyError:
        raise HTTPException(404, f"Session {session_id!r} not found")
    return state.model_dump()


@app.post("/v1/sessions/{session_id}/resume")
async def resume_session(session_id: str):
    """Resume a paused session."""
    try:
        state = await get_session_manager().resume_session(session_id)
    except (KeyError, ValueError) as e:
        status = 404 if isinstance(e, KeyError) else 409
        raise HTTPException(status, str(e))
    return state.model_dump()


# ── Agent messaging (Phase 4B.1) ──────────────────────────────────────────────

@app.post("/v1/agents/{agent_id}/message")
async def agent_message(agent_id: str, body: AgentMessageRequest):
    """
    Send a message to a specific agent's inbox.

    The agent does not need to be in WAITING_FOR_INPUT — messages are queued
    and processed when the agent is next available. If the agent is terminal
    (done/failed/killed) or does not exist, returns 404.

    This endpoint is also used by the TUI @agent targeting syntax.
    """
    ok = await get_agent_manager().send_message(agent_id, body.message)
    if not ok:
        agent = get_agent_manager().get_agent(agent_id)
        if agent is None:
            raise HTTPException(404, f"Agent {agent_id!r} not found")
        raise HTTPException(
            409,
            f"Agent {agent_id!r} is in terminal state ({agent.status}) "
            "and cannot receive messages",
        )
    return {"delivered": True, "agent_id": agent_id}


# ── Streaming endpoints (Phase 4B.2) ──────────────────────────────────────────

@app.get("/v1/agents/{agent_id}/stream")
async def agent_stream(agent_id: str):
    """
    SSE token stream for a specific agent.

    Polls up to 2s (20 × 100ms) for the agent to be registered — handles
    the race where the TUI opens the stream immediately after receiving the
    agent_id from spawn_and_run(), before _new_agent() has written to the
    registry. After 2s with no agent, returns 404.

    Yields raw token strings as "data: <token>" SSE events. Closes with
    "data: [DONE]" so the TUI knows the stream ended intentionally rather
    than dropped. This mirrors the OpenAI/Anthropic streaming convention.

    Tokens flow: model → router (stream=True) → _run_agent() → agent.outbox
                 → subscribe_stream() → this endpoint → TUI agent pane.
    Redis is never in the token path.
    """
    mgr = get_agent_manager()

    # Poll up to 2s for agent registration (reviewer-recommended pattern)
    for _ in range(20):
        if mgr.has_agent(agent_id):
            break
        await asyncio.sleep(0.1)
    else:
        raise HTTPException(404, f"Agent {agent_id!r} not found after 2s — "
                                 "check that spawn_and_run() was called first")

    async def generate() -> AsyncGenerator[dict, None]:
        async for token in mgr.subscribe_stream(agent_id):
            yield {"data": token}
        # Explicit end-of-stream sentinel — TUI closes the EventSource on receipt
        yield {"data": "[DONE]"}

    return EventSourceResponse(generate())


@app.websocket("/ws/session/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str):
    """
    Full-duplex session WebSocket.

    Yields structured WSEvent JSON as agents complete tasks, patches are
    applied, and session state changes. The client (TUI session screen) uses
    this channel for everything except raw token streaming — tokens come
    through the per-agent SSE endpoint above.

    Heartbeat: a WebSocket ping frame is sent every WS_HEARTBEAT_INTERVAL
    seconds (default 30s) to prevent proxy/load-balancer idle timeouts
    during long agent runs where no events are generated.

    Phase 4B.2: _session_event_loop() is a minimal stub that yields nothing —
    the WebSocket is structurally complete and will carry real events once
    AgentBus is wired in Phase 4B.3. The heartbeat and disconnect handling
    are fully functional now.
    """
    await websocket.accept()
    log.info("ws connected: session=%s  client=%s", session_id, websocket.client)

    heartbeat_task = asyncio.create_task(
        _ws_heartbeat(websocket, session_id)
    )

    try:
        async for event in _session_event_loop(session_id):
            await websocket.send_json(event.model_dump())
    except WebSocketDisconnect:
        log.info("ws disconnected: session=%s", session_id)
    except Exception as e:
        log.error("ws error: session=%s  error=%s", session_id, e)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        log.info("ws closed: session=%s", session_id)


async def _ws_heartbeat(websocket: WebSocket, session_id: str) -> None:
    """
    Send a WebSocket ping frame every WS_HEARTBEAT_INTERVAL seconds.
    Prevents proxy/LB idle-timeout drops during long agent runs.
    Cancelled by ws_session() finally block on disconnect.
    """
    while True:
        await asyncio.sleep(config.WS_HEARTBEAT_INTERVAL)
        try:
            await websocket.send_json({
                "type":       "heartbeat",
                "session_id": session_id,
                "ts":         time.time(),
            })
        except Exception:
            # WebSocket already closed — heartbeat task will be cancelled shortly
            break


async def _session_event_loop(session_id: str) -> AsyncGenerator[WSEvent, None]:
    """
    Async generator of WSEvent for a session's WebSocket channel.

    Phase 4B.3: subscribes to AgentBus.subscribe_session() which reads from
    the Redis pub/sub channel bus:session:{session_id}. Yields all structured
    events (work_complete, work_failed, patch_applied, test_result, status,
    debate_point, interrupt) to the WebSocket handler for forwarding to the TUI.

    Exits when the bus generator exits (STATUS/ended event received, or Redis
    connection drops). The ws_session() caller handles WebSocketDisconnect.
    """
    try:
        async for event in get_agent_bus().subscribe_session(session_id):
            yield event
    except Exception as e:
        log.error("_session_event_loop error session=%s: %s", session_id, e)
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/v1/session/configure")
async def session_configure(body: dict):
    """
    Store per-session role→model assignments.

    Phase 4B.1 update: delegates to SessionManager.configure_models() so
    that if a session:state key already exists, both TTLs are refreshed
    atomically. SESSION_TTL (7 days) is used instead of the deprecated
    SESSION_MODELS_TTL (24h).

    Body: {"session_id": "abc123", "models": {"architect": "Qwen/...", ...}}
    """
    session_id = body.get("session_id", str(uuid.uuid4()))
    models     = body.get("models", {})
    if not models:
        raise HTTPException(400, "models dict is required")

    registry  = get_model_registry()
    catalog   = {m["name"] for m in registry.catalog_with_status()}
    bad_models = [m for m in models.values() if m not in catalog]
    if bad_models:
        raise HTTPException(
            400,
            f"Unknown model(s): {bad_models}. "
            "Check GET /v1/models/catalog for valid names.",
        )

    # Phase 4B.1: configure_models() handles both new and existing sessions,
    # refreshes both key TTLs atomically.
    state = await get_session_manager().configure_models(session_id, models)

    log.info(
        "session configured: session=%s models=%s ttl=%ds",
        session_id, models, config.SESSION_TTL,
    )
    return {
        "session_id":  session_id,
        "models":      state.models,
        "configured":  True,
        "ttl_seconds": config.SESSION_TTL,
    }


@app.get("/v1/session/models")
async def get_session_models(session_id: str):
    """Return the current role→model map for a session (empty if using profile defaults)."""
    from routing_policy import get_routing_policy
    models = await get_routing_policy().get_session_models(session_id)
    return {
        "session_id":     session_id,
        "models":         models,
        "using_defaults": len(models) == 0,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Existing endpoints (unchanged from 0.4.x — kept in full for resumability)
# ═════════════════════════════════════════════════════════════════════════════

# ── Agent endpoints ───────────────────────────────────────────────────────────

@app.post("/v1/agents/spawn")
async def spawn_agent(body: dict):
    role       = body.get("role", "coder")
    task       = body.get("task", "")
    session_id = body.get("session_id", str(uuid.uuid4()))
    if not task:
        raise HTTPException(400, "task is required")
    return await get_agent_manager().spawn_and_run(role=role, task=task, session_id=session_id)


@app.get("/v1/agents/status")
def agent_status():
    return get_agent_manager().get_status()


@app.get("/v1/agents/list")
def agent_list():
    return {"agents": get_agent_manager().list_agents()}


@app.get("/v1/agents/{agent_id}/logs")
def agent_logs(agent_id: str):
    agent = get_agent_manager().get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"Agent {agent_id!r} not found")
    return agent.to_dict()


@app.post("/v1/agents/cleanup")
async def cleanup_agents():
    removed = await get_agent_manager().cleanup_idle_agents()
    return {"removed": removed, "idle_timeout_s": config.AGENT_IDLE_TIMEOUT}


# ── Model registry ────────────────────────────────────────────────────────────

@app.get("/v1/models/catalog")
def models_catalog():
    return {
        "profile":  config.PROFILE,
        "models":   get_model_registry().catalog_with_status(),
        "roles":    list(config.ALL_ROLES),
    }


@app.get("/v1/models/for-role")
def models_for_role(role: str = Query(..., description="Agent role to filter models for")):
    models = get_model_registry().get_models_for_role(role)
    return {"role": role, "models": models, "count": len(models)}


@app.post("/v1/models/pull")
async def pull_model(body: dict):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    status = get_agent_manager().get_status()
    if status.get("running", 0) > 0:
        raise HTTPException(
            409,
            f"Cannot pull model while {status['running']} agent(s) are running.",
        )
    try:
        result = await get_model_registry().pull_model(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return result


@app.post("/v1/models/refresh")
async def refresh_models():
    available = await get_model_registry().detect_available()
    return {
        "refreshed": True,
        "available": {backend: len(models) for backend, models in available.items()},
    }


# ── Skills ────────────────────────────────────────────────────────────────────

@app.get("/v1/skills/list")
def list_skills():
    return {"skills": skill_loader.list_skills(), "commands": skill_loader.list_commands()}


@app.post("/v1/skills/learn")
async def learn_skill(body: dict):
    session_id = body.get("session_id", "default")
    transcript = body.get("transcript", [])
    if not transcript:
        raise HTTPException(400, "transcript is required")
    skill_name = await get_session_hooks().extract_skills(session_id, transcript)
    return {"skill_extracted": skill_name is not None, "skill_name": skill_name}


# ── Session hooks (legacy — kept for backwards compat with existing callers) ──

@app.post("/v1/session/start")
async def session_start(body: dict):
    """
    Legacy endpoint. For new code, prefer POST /v1/sessions which returns
    full SessionState and is managed by SessionManager.
    """
    session_id = body.get("session_id", str(uuid.uuid4()))
    task       = body.get("task", "")
    return await get_session_hooks().on_session_start(session_id, task)


@app.post("/v1/session/end")
async def session_end(body: dict):
    """
    Legacy endpoint. For new code, prefer POST /v1/sessions/{id}/end.
    """
    session_id = body.get("session_id", "default")
    summary    = body.get("summary", "")
    transcript = body.get("transcript", [])
    failures   = body.get("failures", [])
    if not summary:
        raise HTTPException(400, "summary is required")
    result = await get_session_hooks().on_session_end(
        session_id, summary, transcript, failures
    )
    await get_agent_manager().cleanup_idle_agents()
    return result


# ── Memory & Indexing ─────────────────────────────────────────────────────────

@app.post("/v1/index")
async def index_codebase():
    log.info("indexing codebase at /workspace")
    return await memory.index_codebase("/workspace")


@app.get("/v1/memory/recall")
async def recall(q: str = Query(...)):
    results = await memory.recall(q)
    return {"query": q, "results": results}


@app.get("/v1/memory/symbol")
async def symbol_search(name: str = Query(...), k: int = 5):
    if not name:
        raise HTTPException(400, "name is required")
    results = await memory.search_symbol(name, k=k)
    return {"query": name, "results": results, "count": len(results)}


@app.post("/v1/memory/save")
async def save_memory(body: dict):
    session_id = body.get("session_id", str(uuid.uuid4()))
    content    = body.get("content", "")
    if not content:
        raise HTTPException(400, "content is required")
    await memory.save_session(session_id, content, body.get("metadata", {}))
    return {"saved": True, "session_id": session_id}


# ── Patch Queue ───────────────────────────────────────────────────────────────

@app.post("/v1/patches/submit")
async def submit_patch(body: dict):
    diff        = body.get("diff", "")
    agent_id    = body.get("agent_id", "manual")
    task_id     = body.get("task_id", str(uuid.uuid4()))
    session_id  = body.get("session_id", "default")
    description = body.get("description", "")
    if not diff:
        raise HTTPException(400, "diff is required")
    try:
        patch = await patch_queue.enqueue(diff, agent_id, task_id, session_id, description)
        return patch.to_dict()
    except PatchValidationError as e:
        raise HTTPException(400, str(e))


@app.post("/v1/patches/process")
async def process_patches():
    results = await patch_queue.process_all()
    return {"processed": len(results), "results": results}


@app.get("/v1/patches/status")
def patches_status():
    return patch_queue.queue_depth()


@app.get("/v1/patches/list")
def patches_list(session_id: str = None):
    return {"patches": patch_queue.list_patches(session_id)}


@app.post("/v1/patches/test")
async def patch_and_test(body: dict):
    diff         = body.get("diff", "")
    agent_id     = body.get("agent_id", "manual")
    task_id      = body.get("task_id", str(uuid.uuid4()))
    session_id   = body.get("session_id", "default")
    description  = body.get("description", "")
    test_pattern = body.get("test_pattern", "tests/")
    if not diff:
        raise HTTPException(400, "diff is required")
    try:
        patch = await patch_queue.enqueue(diff, agent_id, task_id, session_id, description)
    except PatchValidationError as e:
        raise HTTPException(400, str(e))
    return await patch_queue.test_fix_loop(
        patch=patch, agent_mgr=get_agent_manager(), test_pattern=test_pattern
    )


# ── Task Queue ────────────────────────────────────────────────────────────────

@app.post("/v1/tasks/load")
async def load_tasks(body: dict):
    session_id = body.get("session_id", str(uuid.uuid4()))
    tasks      = body.get("tasks", [])
    if not tasks:
        raise HTTPException(400, "tasks list is required")
    try:
        result = await task_queue.load_plan(session_id, tasks)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Phase 4B.1: register task IDs with SessionManager if session exists
    try:
        sm = get_session_manager()
        for t in tasks:
            await sm.register_task(session_id, t["id"])
    except Exception:
        pass   # non-fatal if session not managed

    return result


@app.post("/v1/tasks/execute")
async def execute_tasks(body: dict):
    session_id = body.get("session_id", "default")
    return await task_queue.execute_plan(session_id, get_agent_manager())


@app.get("/v1/tasks/status")
async def task_status(session_id: str = "default"):
    return await task_queue.get_session_status(session_id)


# ── Debate Engine ─────────────────────────────────────────────────────────────

@app.post("/v1/agents/debate")
async def debate(body: dict):
    topic      = body.get("topic", "")
    session_id = body.get("session_id", str(uuid.uuid4()))
    max_rounds = body.get("max_rounds", config.MAX_DEBATE_ROUNDS)
    plan       = body.get("plan", "")
    if not topic:
        raise HTTPException(400, "topic is required")
    return await get_debate_engine().run(
        topic=topic, session_id=session_id, initial_plan=plan, max_rounds=max_rounds,
    )


# ── Metrics ───────────────────────────────────────────────────────────────────

@app.get("/v1/metrics")
def get_metrics(session_id: str = None):
    if session_id:
        return metrics.get_session_summary(session_id)
    return metrics.get_summary()


# ── Fine-tune ─────────────────────────────────────────────────────────────────

@app.get("/v1/finetune/stats")
def finetune_stats():
    return get_stats()


@app.get("/v1/finetune/export")
def finetune_export(limit: int = None):
    import json as _json
    records = read_records(limit=limit)
    if not records:
        return PlainTextResponse("", media_type="application/x-ndjson")
    content = "\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    return PlainTextResponse(
        content,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=training_data.jsonl"},
    )


@app.delete("/v1/finetune/clear")
def finetune_clear():
    return {"deleted": clear_records()}


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/v1/webhook/github")
async def github_webhook(request: Request):
    body      = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    event     = request.headers.get("X-GitHub-Event", "")
    try:
        verify_signature(body, signature)
    except WebhookSignatureError as e:
        log.warning("webhook: signature failed: %s", e)
        raise HTTPException(401, str(e))
    import json as _json
    try:
        payload = _json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")
    mgr = get_agent_manager()
    if event == "workflow_run":
        return await handle_workflow_run(payload, mgr, patch_queue)
    elif event == "issues":
        return await handle_issue_opened(payload, mgr, task_queue)
    else:
        return {"skipped": True, "reason": f"unsupported event: {event}"}


# ── Model list (OpenAI-compatible) ────────────────────────────────────────────

@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [{"id": "orchestrator", "object": "model",
                  "created": int(time.time()), "owned_by": "local"}],
    }


# ── Chat completions ──────────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    messages = []
    for m in req.messages:
        d = m.model_dump(exclude_none=True)
        if isinstance(d.get("content"), list):
            d["content"] = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in d["content"]
            )
        messages.append(d)

    cmd = parse_command(messages)
    if cmd:
        response_text = await _handle_command(cmd, req)
        return JSONResponse(_make_response(response_text, "orchestrator"))

    log.info("chat  messages=%d  stream=%s  profile=%s",
             len(req.messages), req.stream, config.PROFILE)
    try:
        result = await router.dispatch(req)
    except Exception as e:
        log.exception("dispatch failed: %s", e)
        raise HTTPException(502, f"Model backend error: {e}")

    if isinstance(result, AsyncIterator):
        return StreamingResponse(result, media_type="text/event-stream")
    return JSONResponse(content=result)


async def _handle_command(cmd, req: ChatCompletionRequest) -> str:
    sid = str(uuid.uuid4())
    mgr = get_agent_manager()

    if cmd.name == "architect":
        r = await mgr.spawn_and_run(role="architect", task=cmd.args, session_id=sid)
        return f"**Architect Plan**\n\n{r.get('result','')}"

    elif cmd.name == "debate":
        r         = await get_debate_engine().run(topic=cmd.args, session_id=sid)
        consensus = "✅ Consensus reached" if r.get("consensus") else "⚠️ No consensus"
        return f"**Debate Result** ({r.get('rounds',0)} round(s), {consensus})\n\n{r.get('final_plan','')}"

    elif cmd.name == "review":
        r = await mgr.spawn_and_run(role="reviewer", task=cmd.args, session_id=sid)
        return f"**Review**\n\n{r.get('result','')}"

    elif cmd.name == "test":
        r = await mgr.spawn_and_run(role="tester", task=cmd.args, session_id=sid)
        return f"**Tests**\n\n{r.get('result','')}"

    elif cmd.name == "execute":
        r = await task_queue.execute_plan(sid, mgr)
        return (f"**Execution complete**\n"
                f"- Tasks executed: {r.get('executed',0)}\n"
                f"- Complete: {r.get('complete',0)}\n"
                f"- Failed: {r.get('failed',0)}")

    elif cmd.name == "memory":
        results = await memory.recall(cmd.args, k=5)
        if not results:
            return f"No memories found for: *{cmd.args}*"
        lines = [f"**Memory search:** {cmd.args}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. [{r['collection']}] {r['content'][:200]}")
        return "\n".join(lines)

    elif cmd.name == "learn":
        msgs  = [m.model_dump(exclude_none=True) for m in req.messages]
        skill = await get_session_hooks().extract_skills(sid, msgs)
        return f"✅ Skill extracted: **{skill}**" if skill else "No reusable skill identified."

    elif cmd.name == "status":
        agent_s   = get_agent_manager().get_status()
        patch_s   = patch_queue.queue_depth()
        metrics_s = metrics.get_summary()
        ft_s      = get_stats()
        reg       = get_model_registry()
        catalog   = reg.catalog_with_status()
        on_disk   = sum(1 for m in catalog if m["on_disk"])
        try:
            active_sessions = await get_session_manager().list_sessions(status="active")
            session_line = f"- Sessions: {len(active_sessions)} active\n"
        except Exception:
            session_line = ""
        return (
            f"**System Status** (v{app.version}  profile={config.PROFILE})\n"
            f"- Agents: {agent_s['total']} total, {agent_s['running']} running, "
            f"{agent_s['done']} done, {agent_s['failed']} failed\n"
            f"{session_line}"
            f"- Patches: {patch_s['total']} total ({patch_s['pending']} pending), "
            f"depth limit {config.MAX_PATCH_QUEUE_DEPTH}\n"
            f"- Metrics: {metrics_s['total_requests']} requests, "
            f"{metrics_s['total_tokens_in']+metrics_s['total_tokens_out']} tokens, "
            f"avg {metrics_s['avg_latency_ms']}ms\n"
            f"- Training data: {ft_s['records']} examples\n"
            f"- Models: {on_disk}/{len(catalog)} on disk\n"
            f"- Embed cache: {config.EMBED_CACHE_MAX_SIZE} max entries (LRU)\n"
            f"- Executor slots: {config.MAX_EXECUTOR_CONCURRENCY}"
        )

    elif cmd.name == "index":
        r = await memory.index_codebase("/workspace")
        return (f"✅ Codebase indexed\n"
                f"- Files indexed: {r['files_indexed']}\n"
                f"- Files unchanged: {r.get('files_unchanged', 0)}\n"
                f"- Chunks: {r['chunks']}\n"
                f"- Skipped: {r['skipped']}")

    else:
        return f"Unknown command: `{cmd.args}`\n\n{help_text()}"


def _make_response(content: str, model: str) -> dict:
    return {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage":   {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }