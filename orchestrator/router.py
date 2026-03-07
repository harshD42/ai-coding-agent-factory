"""
router.py — Health-aware routing to model endpoints with fallback chain.

Routes requests to the correct model server based on role.
Falls back to next healthy endpoint if the primary is down.
"""

import json
import logging
import time
import uuid
from typing import AsyncIterator

import httpx

import config
from models import ChatCompletionRequest

log = logging.getLogger("router")

_client = httpx.AsyncClient(timeout=300.0)
_health_cache: dict[str, tuple[bool, float]] = {}  # url → (healthy, checked_at)
HEALTH_CACHE_TTL = 30.0  # seconds


# ── Health checking ───────────────────────────────────────────────────────────

async def _is_healthy(base_url: str) -> bool:
    """Check endpoint health with caching to avoid hammering."""
    now = time.time()
    if base_url in _health_cache:
        healthy, checked_at = _health_cache[base_url]
        if now - checked_at < HEALTH_CACHE_TTL:
            return healthy

    # Ollama health check
    if "11434" in base_url or "ollama" in base_url:
        try:
            r = await _client.get(f"{base_url.rstrip('/')}/api/tags", timeout=5.0)
            ok = r.status_code == 200
        except Exception:
            ok = False
    else:
        # vLLM health check
        try:
            r = await _client.get(f"{base_url.rstrip('/')}/health", timeout=5.0)
            ok = r.status_code == 200
        except Exception:
            ok = False

    _health_cache[base_url] = (ok, now)
    if not ok:
        log.warning("endpoint unhealthy: %s", base_url)
    return ok


async def resolve_endpoint(role: str = "coder") -> tuple[str, str, str]:
    """
    Return (endpoint_url, model_name, backend_type) for the given role,
    falling back through FALLBACK_ORDER if the primary is unhealthy.
    backend_type is 'ollama' or 'vllm'.
    """
    primary_url   = config.ROLE_ENDPOINTS.get(role, config.OLLAMA_URL)
    primary_model = config.ROLE_MODELS.get(role, config.OLLAMA_MODEL)

    if await _is_healthy(primary_url):
        btype = "ollama" if ("11434" in primary_url or "ollama" in primary_url) else "vllm"
        return primary_url, primary_model, btype

    # Walk fallback chain
    for fallback_url in config.FALLBACK_ORDER:
        if fallback_url == primary_url:
            continue
        if await _is_healthy(fallback_url):
            btype = "ollama" if ("11434" in fallback_url or "ollama" in fallback_url) else "vllm"
            model = config.OLLAMA_MODEL if btype == "ollama" else primary_model
            log.warning("role=%s falling back to %s", role, fallback_url)
            return fallback_url, model, btype

    # Nothing healthy — try primary anyway and let the error surface
    log.error("no healthy endpoint found for role=%s, trying primary", role)
    btype = "ollama" if ("11434" in primary_url or "ollama" in primary_url) else "vllm"
    return primary_url, primary_model, btype


# ── Request builders ──────────────────────────────────────────────────────────

def _build_ollama_body(messages: list[dict], model: str, req: ChatCompletionRequest) -> dict:
    # Ollama only accepts: model, messages, stream, options
    # Strip tool_calls, tool definitions, and any non-text content blocks
    clean_messages = []
    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content", "")
        # Skip tool-related roles Ollama doesn't support
        if role in ("tool", "function"):
            continue
        # Flatten list content to string
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
                sse = {"id": cid, "object": "chat.completion.chunk", "created": ts,
                       "model": model,
                       "choices": [{"index": 0, "delta": {"content": content},
                                    "finish_reason": "stop" if done else None}]}
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
    req: ChatCompletionRequest,
    role: str = "coder",
    messages: list[dict] | None = None,
) -> "dict | AsyncIterator[str]":
    """
    Route request to the correct model server.
    `messages` overrides req.messages (used by agent_manager to inject
    pre-built context from context_manager).
    """
    endpoint, model, btype = await resolve_endpoint(role)
    msgs = messages or [m.model_dump(exclude_none=True) for m in req.messages]
    log.info("dispatch  role=%s  backend=%s  endpoint=%s  stream=%s",
             role, btype, endpoint, req.stream)

    if btype == "ollama":
        body = _build_ollama_body(msgs, model, req)
        if req.stream:
            return _stream_ollama(endpoint, body, model)
        resp = await _client.post(f"{endpoint.rstrip('/')}/api/chat", json=body)
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
            # Pass Ollama's native token counts through so metrics.parse_usage()
            # can find them. Ollama uses prompt_eval_count / eval_count at root.
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
        resp = await _client.post(f"{endpoint.rstrip('/')}/chat/completions", json=body)
        resp.raise_for_status()
        return resp.json()