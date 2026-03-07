"""
main.py — Orchestrator entry point.

Phase 2 additions:
  Step 2.1  task_queue.set_patch_queue() wired in lifespan
  Step 2.2  POST /v1/patches/test   — apply patch + run test-fix loop
  Step 2.3  GET  /v1/metrics        — aggregate token / latency metrics
            /status command updated to include metrics summary
  Step 2.4  file_watcher.start()    — real-time workspace hash registry
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

import config
import router
from agent_manager import init_agent_manager, get_agent_manager
from command_parser import parse as parse_command, help_text
from debate_engine import init_debate_engine, get_debate_engine
from file_watcher import file_watcher                          # Step 2.4
from memory_manager import memory
from metrics import metrics                                    # Step 2.3
from models import ChatCompletionRequest
from patch_queue import patch_queue, PatchValidationError
from session_hooks import init_session_hooks, get_session_hooks
from skill_loader import skill_loader
from task_queue import task_queue

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

    # Step 2.1 — wire patch_queue into task_queue so diffs are auto-applied
    task_queue.set_patch_queue(patch_queue)

    mgr = init_agent_manager(memory)
    init_debate_engine(mgr)
    init_session_hooks(memory)
    skill_loader.load()

    # Step 2.4 — start file watcher (uses task_queue's Redis connection)
    await file_watcher.start(task_queue._redis)

    log.info("all systems ready")
    yield

    # Shutdown
    await file_watcher.stop()                                  # Step 2.4
    await memory.close()
    await task_queue.close()


app = FastAPI(title="AI Coding Agent Orchestrator", version="0.2.0", lifespan=lifespan)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "profile": config.PROFILE, "version": "0.2.0"}


# ── Agent endpoints ───────────────────────────────────────────────────────────

@app.post("/v1/agents/spawn")
async def spawn_agent(body: dict):
    role       = body.get("role", "coder")
    task       = body.get("task", "")
    session_id = body.get("session_id", str(uuid.uuid4()))
    if not task:
        raise HTTPException(400, "task is required")
    mgr    = get_agent_manager()
    result = await mgr.spawn_and_run(role=role, task=task, session_id=session_id)
    return result


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


# ── Session hooks ─────────────────────────────────────────────────────────────

@app.post("/v1/session/start")
async def session_start(body: dict):
    session_id = body.get("session_id", str(uuid.uuid4()))
    task       = body.get("task", "")
    return await get_session_hooks().on_session_start(session_id, task)


@app.post("/v1/session/end")
async def session_end(body: dict):
    session_id = body.get("session_id", "default")
    summary    = body.get("summary", "")
    transcript = body.get("transcript", [])
    failures   = body.get("failures", [])
    if not summary:
        raise HTTPException(400, "summary is required")
    return await get_session_hooks().on_session_end(
        session_id, summary, transcript, failures
    )


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


# ── Memory & Indexing ─────────────────────────────────────────────────────────

@app.post("/v1/index")
async def index_codebase():
    log.info("indexing codebase at /workspace")
    return await memory.index_codebase("/workspace")


@app.get("/v1/memory/recall")
async def recall(q: str = Query(...)):
    results = await memory.recall(q)
    return {"query": q, "results": results}


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


# ── Step 2.2: Patch + test endpoint ──────────────────────────────────────────

@app.post("/v1/patches/test")
async def patch_and_test(body: dict):
    """
    Submit a diff, apply it, run pytest, and auto-fix failures up to
    MAX_FIX_ATTEMPTS times.

    Request body:
        diff         string   required — unified diff
        agent_id     string   optional
        task_id      string   optional
        session_id   string   optional
        description  string   optional
        test_pattern string   optional  default "tests/"

    Response includes test_passed, attempts, test_summary in addition to
    the standard patch fields.
    """
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

    mgr    = get_agent_manager()
    result = await patch_queue.test_fix_loop(
        patch=patch,
        agent_mgr=mgr,
        test_pattern=test_pattern,
    )
    return result


# ── Task Queue (DAG) ──────────────────────────────────────────────────────────

@app.post("/v1/tasks/load")
async def load_tasks(body: dict):
    session_id = body.get("session_id", str(uuid.uuid4()))
    tasks      = body.get("tasks", [])
    if not tasks:
        raise HTTPException(400, "tasks list is required")
    try:
        return await task_queue.load_plan(session_id, tasks)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/v1/tasks/execute")
async def execute_tasks(body: dict):
    session_id = body.get("session_id", "default")
    mgr        = get_agent_manager()
    return await task_queue.execute_plan(session_id, mgr)


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
        topic=topic,
        session_id=session_id,
        initial_plan=plan,
        max_rounds=max_rounds,
    )


# ── Step 2.3: Metrics endpoint ────────────────────────────────────────────────

@app.get("/v1/metrics")
def get_metrics(session_id: str = None):
    """
    Return aggregate metrics, optionally filtered to a single session.

    Query params:
        session_id   optional — if provided, returns only that session's metrics
    """
    if session_id:
        return metrics.get_session_summary(session_id)
    return metrics.get_summary()


# ── Model list ────────────────────────────────────────────────────────────────

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
        r = await get_debate_engine().run(topic=cmd.args, session_id=sid)
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
        metrics_s = metrics.get_summary()          # Step 2.3
        return (
            f"**System Status**\n"
            f"- Agents: {agent_s['total']} total, {agent_s['running']} running, "
            f"{agent_s['done']} done, {agent_s['failed']} failed\n"
            f"- Patches: {patch_s['total']} total, {patch_s['pending']} pending, "
            f"{patch_s['applied']} applied, {patch_s['rejected']} rejected\n"
            f"- Metrics: {metrics_s['total_requests']} requests, "
            f"{metrics_s['total_tokens_in']+metrics_s['total_tokens_out']} total tokens, "
            f"avg latency {metrics_s['avg_latency_ms']}ms"
        )

    elif cmd.name == "index":
        r = await memory.index_codebase("/workspace")
        return (f"✅ Codebase indexed\n"
                f"- Files: {r['files_indexed']}\n"
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