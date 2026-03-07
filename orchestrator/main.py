"""
main.py — Orchestrator entry point.

Phase 3 additions:
  3.1  GET  /v1/memory/symbol       — search codebase by symbol name
  3.2  GET  /v1/finetune/export     — download training data JSONL
       GET  /v1/finetune/stats      — training data stats
       DELETE /v1/finetune/clear    — clear training data
  3.3  POST /v1/webhook/github      — GitHub CI/issue webhook receiver
"""

import hashlib
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse

import config
import router
from agent_manager import init_agent_manager, get_agent_manager
from command_parser import parse as parse_command, help_text
from debate_engine import init_debate_engine, get_debate_engine
from file_watcher import file_watcher
from fine_tune_collector import get_stats, read_records, clear_records  # Step 3.2
from memory_manager import memory
from metrics import metrics
from models import ChatCompletionRequest
from patch_queue import patch_queue, PatchValidationError
from session_hooks import init_session_hooks, get_session_hooks
from skill_loader import skill_loader
from task_queue import task_queue
from webhook_handler import (                                            # Step 3.3
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
    task_queue.set_patch_queue(patch_queue)
    mgr = init_agent_manager(memory)
    init_debate_engine(mgr)
    init_session_hooks(memory)
    skill_loader.load()
    await file_watcher.start(task_queue._redis)
    log.info("all systems ready")
    yield
    await file_watcher.stop()
    await memory.close()
    await task_queue.close()


app = FastAPI(title="AI Coding Agent Orchestrator", version="0.3.0", lifespan=lifespan)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "profile": config.PROFILE, "version": "0.3.0"}


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


# ── Step 3.1: Symbol search ───────────────────────────────────────────────────

@app.get("/v1/memory/symbol")
async def symbol_search(name: str = Query(...), k: int = 5):
    """
    Search the indexed codebase for a function or class by name.

    Query params:
        name   required — symbol name (exact or partial)
        k      optional — max results (default 5)

    Returns chunks with symbol, symbol_type, start_line, end_line metadata.
    """
    if not name:
        raise HTTPException(400, "name is required")
    results = await memory.search_symbol(name, k=k)
    return {"query": name, "results": results, "count": len(results)}


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
        return await task_queue.load_plan(session_id, tasks)
    except ValueError as e:
        raise HTTPException(400, str(e))


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


# ── Step 3.2: Fine-tune data endpoints ───────────────────────────────────────

@app.get("/v1/finetune/stats")
def finetune_stats():
    """Return stats about collected training data."""
    return get_stats()


@app.get("/v1/finetune/export")
def finetune_export(limit: int = None):
    """
    Download training data as JSONL.
    Each line is a JSON object: {instruction, input, output, metadata}.

    Query params:
        limit   optional — cap number of records returned
    """
    import json
    records = read_records(limit=limit)
    if not records:
        return PlainTextResponse("", media_type="application/x-ndjson")
    content = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    return PlainTextResponse(
        content,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=training_data.jsonl"},
    )


@app.delete("/v1/finetune/clear")
def finetune_clear():
    """Delete all collected training records."""
    count = clear_records()
    return {"deleted": count}


# ── Step 3.3: GitHub webhook ──────────────────────────────────────────────────

@app.post("/v1/webhook/github")
async def github_webhook(request: Request):
    """
    Receive GitHub webhook events.

    Supported events:
        workflow_run  — failed CI triggers coder agent + patch enqueue
        issues        — new issue triggers architect decomposition

    Requires X-Hub-Signature-256 header signed with GITHUB_WEBHOOK_SECRET.
    """
    body      = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    event     = request.headers.get("X-GitHub-Event", "")

    try:
        verify_signature(body, signature)
    except WebhookSignatureError as e:
        log.warning("webhook: signature validation failed: %s", e)
        raise HTTPException(401, str(e))

    import json as _json
    try:
        payload = _json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    mgr = get_agent_manager()
    log.info("webhook: received event=%r", event)

    if event == "workflow_run":
        result = await handle_workflow_run(payload, mgr, patch_queue)
        return result

    elif event == "issues":
        result = await handle_issue_opened(payload, mgr, task_queue)
        return result

    else:
        return {"skipped": True, "reason": f"unsupported event: {event}"}


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
        return (
            f"**System Status**\n"
            f"- Agents: {agent_s['total']} total, {agent_s['running']} running, "
            f"{agent_s['done']} done, {agent_s['failed']} failed\n"
            f"- Patches: {patch_s['total']} total, {patch_s['pending']} pending, "
            f"{patch_s['applied']} applied, {patch_s['rejected']} rejected\n"
            f"- Metrics: {metrics_s['total_requests']} requests, "
            f"{metrics_s['total_tokens_in']+metrics_s['total_tokens_out']} total tokens, "
            f"avg latency {metrics_s['avg_latency_ms']}ms\n"
            f"- Training data: {ft_s['records']} examples collected"
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