"""
main.py — Orchestrator entry point.

Step 3: OpenAI-compatible proxy.
  POST /v1/chat/completions  — Cline connects here
  GET  /v1/models            — Cline queries this on startup
  GET  /health               — Docker health check
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
from memory_manager import memory
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
    log.info("connecting to ChromaDB...")
    await memory.connect()
    await task_queue.connect()
    mgr = init_agent_manager(memory)
    init_debate_engine(mgr)
    init_session_hooks(memory)
    skill_loader.load()
    log.info("all systems ready")
    yield
    await memory.close()
    await task_queue.close()


app = FastAPI(title="AI Coding Agent Orchestrator", version="0.6.0", lifespan=lifespan)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "profile": config.PROFILE, "version": "0.6.0"}


# ── Agent endpoints ───────────────────────────────────────────────────────────

@app.post("/v1/agents/spawn")
async def spawn_agent(body: dict):
    """Spawn an agent by role and run it with a task."""
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
    """Extract and save a skill from a session transcript."""
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
    result = await memory.index_codebase("/workspace")
    return result


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
    """Submit a unified diff to the patch queue."""
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
    """Process all pending patches in the queue."""
    results = await patch_queue.process_all()
    return {"processed": len(results), "results": results}


@app.get("/v1/patches/status")
def patches_status():
    return patch_queue.queue_depth()


@app.get("/v1/patches/list")
def patches_list(session_id: str = None):
    return {"patches": patch_queue.list_patches(session_id)}


# ── Task Queue (DAG) ──────────────────────────────────────────────────────────

@app.post("/v1/tasks/load")
async def load_tasks(body: dict):
    """Load a task DAG for a session."""
    session_id = body.get("session_id", str(uuid.uuid4()))
    tasks      = body.get("tasks", [])
    if not tasks:
        raise HTTPException(400, "tasks list is required")
    try:
        result = await task_queue.load_plan(session_id, tasks)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/v1/tasks/execute")
async def execute_tasks(body: dict):
    """Execute all ready tasks in the DAG for a session."""
    session_id = body.get("session_id", "default")
    mgr        = get_agent_manager()
    result     = await task_queue.execute_plan(session_id, mgr)
    return result


@app.get("/v1/tasks/status")
async def task_status(session_id: str = "default"):
    return await task_queue.get_session_status(session_id)


# ── Debate Engine ─────────────────────────────────────────────────────────────

@app.post("/v1/agents/debate")
async def debate(body: dict):
    """Run a multi-round architect vs reviewer debate."""
    topic      = body.get("topic", "")
    session_id = body.get("session_id", str(uuid.uuid4()))
    max_rounds = body.get("max_rounds", config.MAX_DEBATE_ROUNDS)
    plan       = body.get("plan", "")
    if not topic:
        raise HTTPException(400, "topic is required")
    result = await get_debate_engine().run(
        topic=topic,
        session_id=session_id,
        initial_plan=plan,
        max_rounds=max_rounds,
    )
    return result


# ── Model list (Cline calls this on connect) ──────────────────────────────────

@app.get("/v1/models")
def list_models():
    """Return a minimal model list so Cline's model picker works."""
    return {
        "object": "list",
        "data": [
            {
                "id":       "orchestrator",
                "object":   "model",
                "created":  int(time.time()),
                "owned_by": "local",
            }
        ],
    }


# ── Chat completions ──────────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    # Normalize Cline's list-format content blocks to plain strings
    messages = []
    for m in req.messages:
        d = m.model_dump(exclude_none=True)
        if isinstance(d.get("content"), list):
            d["content"] = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in d["content"]
            )
        messages.append(d)

    # ── Command interception ──────────────────────────────────────────────────
    cmd = parse_command(messages)
    if cmd:
        response_text = await _handle_command(cmd, req)
        return JSONResponse(_make_response(response_text, "orchestrator"))

    # ── Normal chat → model ───────────────────────────────────────────────────
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
    """Dispatch a parsed /command and return a text response."""
    sid = str(uuid.uuid4())   # session ID for this command invocation
    mgr = get_agent_manager()

    if cmd.name == "architect":
        r = await mgr.spawn_and_run(role="architect", task=cmd.args, session_id=sid)
        return f"**Architect Plan**\n\n{r.get('result','')}"

    elif cmd.name == "debate":
        r = await get_debate_engine().run(topic=cmd.args, session_id=sid)
        rounds    = r.get("rounds", 0)
        consensus = "✅ Consensus reached" if r.get("consensus") else "⚠️ No consensus"
        return f"**Debate Result** ({rounds} round(s), {consensus})\n\n{r.get('final_plan','')}"

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
        return f"✅ Skill extracted: **{skill}**" if skill else "No reusable skill identified in this session."

    elif cmd.name == "status":
        agent_s = get_agent_manager().get_status()
        patch_s = patch_queue.queue_depth()
        return (f"**System Status**\n"
                f"- Agents: {agent_s['total']} total, {agent_s['running']} running, "
                f"{agent_s['done']} done, {agent_s['failed']} failed\n"
                f"- Patches: {patch_s['total']} total, {patch_s['pending']} pending, "
                f"{patch_s['applied']} applied, {patch_s['rejected']} rejected")

    elif cmd.name == "index":
        r = await memory.index_codebase("/workspace")
        return (f"✅ Codebase indexed\n"
                f"- Files: {r['files_indexed']}\n"
                f"- Chunks: {r['chunks']}\n"
                f"- Skipped: {r['skipped']}")

    else:  # unknown
        return f"Unknown command: `{cmd.args}`\n\n{help_text()}"


def _make_response(content: str, model: str) -> dict:
    """Wrap a text string in an OpenAI-compatible chat completion response."""
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