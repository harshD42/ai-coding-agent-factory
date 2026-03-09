"""
router.py — Health-aware routing to model endpoints with fallback chain.

Phase 3.5: non-streaming dispatch() wrapped in asyncio.wait_for(MODEL_CALL_TIMEOUT).

Phase 4A.2: resolve_endpoint() now delegates to RoutingPolicy for all
model/endpoint selection. Router stays thin — it owns HTTP dispatch logic
only, not selection logic. The RoutingPolicy singleton is set via
set_policy() called from main.py lifespan.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Optional

import httpx

import config
from models import ChatCompletionRequest

log = logging.getLogger("router")

_client = httpx.AsyncClient(timeout=300.0)
_health_cache: dict[str, tuple[bool, float]] = {}   # url → (healthy, checked_at)
HEALTH_CACHE_TTL = 30.0  # seconds

# RoutingPolicy singleton — set by main.py lifespan via set_policy()
_policy = None


def set_policy(policy) -> None:
    """Wire in the RoutingPolicy singleton. Called from main.py lifespan."""
    global _policy
    _policy = policy


# ── Health checking ───────────────────────────────────────────────────────────

async def _is_healthy(base_url: str) -> bool:
    """Check endpoint health with caching to avoid hammering."""
    now = time.time()
    if base_url in _health_cache:
        healthy, checked_at = _health_cache[base_url]
        if now - checked_at < HEALTH_CACHE_TTL:
            return healthy

    if "11434" in base_url or "ollama" in base_url.lower():
        try:
            r  = await _client.get(f"{base_url.rstrip('/')}/api/tags", timeout=5.0)
            ok = r.status_code == 200
        except Exception:
            ok = False
    else:
        try:
            r  = await _client.get(f"{base_url.rstrip('/')}/health", timeout=5.0)
            ok = r.status_code == 200
        except Exception:
            ok = False

    _health_cache[base_url] = (ok, now)
    if not ok:
        log.warning("endpoint unhealthy: %s", base_url)
    return ok


# ── Endpoint resolution ───────────────────────────────────────────────────────

async def resolve_endpoint(
    role: str = "coder",
    session_id: str = "default",
) -> tuple[str, str, str]:
    """
    Return (endpoint_url, model_name, backend_type) for the given role.

    Phase 4A.2: delegates to RoutingPolicy.resolve() for primary selection.
    Falls back through FALLBACK_ORDER if the resolved endpoint is unhealthy.
    """
    if _policy is not None:
        primary_url, primary_model, primary_btype = await _policy.resolve(role, session_id)
    else:
        # Fallback for tests or early startup where policy isn't wired yet
        from routing_policy import _profile_endpoint, _profile_model, _backend_type
        primary_url   = _profile_endpoint(role)
        primary_model = _profile_model(role)
        primary_btype = _backend_type(primary_url)

    if await _is_healthy(primary_url):
        return primary_url, primary_model, primary_btype

    # Walk fallback chain
    for fallback_url in config.FALLBACK_ORDER:
        if fallback_url == primary_url:
            continue
        if await _is_healthy(fallback_url):
            btype = "ollama" if ("11434" in fallback_url or "ollama" in fallback_url.lower()) else "vllm"
            model = config.OLLAMA_MODEL if btype == "ollama" else primary_model
            log.warning("role=%s session=%s falling back to %s", role, session_id, fallback_url)
            return fallback_url, model, btype

    # Nothing healthy — try primary anyway and let the error surface
    log.error("no healthy endpoint for role=%s session=%s, trying primary", role, session_id)
    return primary_url, primary_model, primary_btype


# ── Request builders ──────────────────────────────────────────────────────────

def _build_ollama_body(messages: list[dict], model: str, req: ChatCompletionRequest) -> dict:
    clean_messages = []
    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content", "")
        if role in ("tool", "function"):
            continue
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content
            )
        clean_messages.append({"role": role, "content": content or ""})

    body: dict = {"model": model, "messages": clean_messages, "stream": req.stream}
    options = {}
    if req.temperature is not None: options["temperature"] = req.temperature
    if req.top_p       is not None: options["top_p"]       = req.top_p
    if req.max_tokens  is not None: options["num_predict"]  = req.max_tokens
    if options:
        body["options"] = options
    return body


def _build_vllm_body(messages: list[dict], model: str, req: ChatCompletionRequest) -> dict:
    body = req.model_dump(exclude_none=True)
    body["model"]    = model
    body["messages"] = messages
    return body


# ── Streaming helpers ─────────────────────────────────────────────────────────

async def _stream_ollama(url: str, body: dict, model: str) -> AsyncIterator[str]:
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    ts  = int(time.time())
    try:
        async with _client.stream("POST", f"{url.rstrip('/')}/api/chat", json=body) as resp:
            if resp.status_code != 200:
                err = f"Ollama error {resp.status_code}"
                yield f"data: {json.dumps({'error': err})}\n\n"
                yield "data: [DONE]\n\n"
                return
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = chunk.get("message", {}).get("content", "")
                done    = chunk.get("done", False)
                sse = {
                    "id": cid, "object": "chat.completion.chunk",
                    "created": ts, "model": model,
                    "choices": [{"index": 0, "delta": {"content": content},
                                 "finish_reason": "stop" if done else None}],
                }
                yield f"data: {json.dumps(sse)}\n\n"
                if done:
                    yield "data: [DONE]\n\n"
                    return
    except Exception as e:
        log.error("stream_ollama error: %s", e)
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"


async def _stream_vllm(url: str, body: dict) -> AsyncIterator[str]:
    async with _client.stream("POST", f"{url.rstrip('/')}/chat/completions", json=body) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if line:
                yield f"{line}\n\n"


# ── Main dispatch ─────────────────────────────────────────────────────────────

async def dispatch(
    req:        ChatCompletionRequest,
    role:       str = "coder",
    messages:   list[dict] | None = None,
    session_id: str = "default",
) -> "dict | AsyncIterator[str]":
    """
    Route request to the correct model server.

    Phase 4A.4: if USE_LITELLM=true, routes through gateway.gateway_dispatch()
    instead of the direct Ollama/vLLM path. Flag is false by default — zero
    behaviour change when disabled.

    Phase 4A.2: session_id passed to resolve_endpoint() so RoutingPolicy
    can look up per-session model overrides from Redis.

    Phase 3.5: non-streaming calls wrapped in asyncio.wait_for(MODEL_CALL_TIMEOUT).
    """
    # Phase 4A.4: LiteLLM gateway (flag-gated, USE_LITELLM=false by default)
    if config.USE_LITELLM:
        from gateway import gateway_dispatch
        msgs = messages or [m.model_dump(exclude_none=True) for m in req.messages]
        _, model, _ = await resolve_endpoint(role, session_id)
        return await gateway_dispatch(
            messages=msgs,
            model=model,
            stream=req.stream,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )

    endpoint, model, btype = await resolve_endpoint(role, session_id)
    msgs = messages or [m.model_dump(exclude_none=True) for m in req.messages]
    log.info(
        "dispatch  role=%s  session=%s  backend=%s  model=%s  stream=%s",
        role, session_id, btype, model, req.stream,
    )

    if btype == "ollama":
        body = _build_ollama_body(msgs, model, req)
        if req.stream:
            return _stream_ollama(endpoint, body, model)
        resp = await asyncio.wait_for(
            _client.post(f"{endpoint.rstrip('/')}/api/chat", json=body),
            timeout=config.MODEL_CALL_TIMEOUT,
        )
        resp.raise_for_status()
        data    = resp.json()
        content = data.get("message", {}).get("content", "")
        return {
            "id":      f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object":  "chat.completion",
            "created": int(time.time()),
            "model":   model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens":     data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count",         0),
                "total_tokens":      data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
            },
        }
    else:
        body = _build_vllm_body(msgs, model, req)
        if req.stream:
            return _stream_vllm(endpoint, body)
        resp = await asyncio.wait_for(
            _client.post(f"{endpoint.rstrip('/')}/chat/completions", json=body),
            timeout=config.MODEL_CALL_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()