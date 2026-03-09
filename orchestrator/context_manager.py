"""
context_manager.py — 5-tier priority context builder with token budgeting.

Phase 3.4 addition:
    P2.5 (between codebase and memories): anti-pattern warnings from the
    skills collection tagged type=antipattern. Injected as a
    "## Known Pitfalls" section so agents know what NOT to do.

Phase 4A.1 change:
    build_prompt() accepts an optional `model` parameter. When provided,
    the token budget ceiling is set from model_registry.get_context_length(model)
    instead of the global MAX_CONTEXT_TOKENS constant. This prevents context
    overflow when a 32k-context model is assigned to an agent while the global
    constant is set higher. Backwards-compatible: callers that omit `model`
    continue to use MAX_CONTEXT_TOKENS.

Priority tiers (never cut → cut first):
    P1   Current task + agent system prompt           [never cut]
    P2   Relevant codebase chunks (AST-aware)         [never cut]
    P2.5 Anti-pattern warnings                        [never cut]
    P3   Recent conversation messages (last N)        [cut last]
    P4   Past session memories + failure records      [cut second]
    P5   Older conversation turns (summarized)        [cut first]
"""

import logging
from typing import Optional

import httpx

import config
from memory_manager import MemoryManager
from utils import count_tokens, count_messages_tokens, sanitize_context

log = logging.getLogger("context")

RESPONSE_BUDGET  = 2048
RECENT_MSG_KEEP  = 6
MEMORY_RESULTS_K = 3
CODEBASE_K       = 4
ANTIPATTERN_K    = 2    # max anti-pattern warnings to inject

_http = httpx.AsyncClient(timeout=120.0)


class ContextManager:
    def __init__(self, mem: MemoryManager):
        self._mem = mem

    async def build_prompt(
        self,
        *,
        task:                 str,
        system_prompt:        str,
        conversation:         list[dict],
        session_id:           str  = "default",
        include_codebase:     bool = True,
        include_memories:     bool = True,
        include_antipatterns: bool = True,
        model:                str  = "",    # Phase 4A.1 — per-model context budget
    ) -> list[dict]:
        """
        Build a token-bounded message list ready to send to a model.

        Phase 4A.1: when `model` is provided, the token budget is derived from
        that model's context_length in the ModelRegistry rather than the global
        MAX_CONTEXT_TOKENS constant. Falls back to MAX_CONTEXT_TOKENS if the
        registry is not yet initialised or the model is unknown.
        """
        budget = _resolve_token_budget(model)

        # P1: system prompt + task
        p1        = _build_system_block(system_prompt, task)
        p1_tokens = count_tokens(p1)

        # P2: codebase context (AST-aware via memory_manager.search_codebase)
        p2 = ""
        if include_codebase and task:
            chunks = await self._mem.search_codebase(task, k=CODEBASE_K)
            if chunks:
                p2 = _format_codebase_context(chunks)

        # P2.5: anti-pattern warnings (Phase 3.4)
        # Only inject antipatterns with confidence >= 0.6 (Phase 4A.3 addition)
        p2_5 = ""
        if include_antipatterns and task:
            antipatterns = await self._mem.search_antipatterns(task, k=ANTIPATTERN_K)
            # Filter by confidence score written by session_hooks (Phase 4A.3)
            antipatterns = [
                ap for ap in antipatterns
                if ap.get("metadata", {}).get("confidence", 1.0) >= 0.6
            ]
            if antipatterns:
                p2_5 = _format_antipattern_context(antipatterns)

        # P4: memory context
        p4 = ""
        if include_memories and task:
            memories = await self._mem.recall(task, k=MEMORY_RESULTS_K)
            if memories:
                p4 = _format_memory_context(memories)

        # Assemble system message
        system_content = p1
        if p2:
            system_content += f"\n\n{p2}"
        if p2_5:
            system_content += f"\n\n{p2_5}"
        if p4:
            system_content += f"\n\n{p4}"
        system_content = sanitize_context(system_content)
        system_tokens  = count_tokens(system_content)

        remaining = budget - system_tokens
        if remaining < 512:
            log.warning(
                "system context too large (%d tokens, budget %d), dropping memories",
                system_tokens, budget,
            )
            system_content = sanitize_context(
                p1
                + (f"\n\n{p2}"   if p2   else "")
                + (f"\n\n{p2_5}" if p2_5 else "")
            )
            system_tokens = count_tokens(system_content)
            remaining     = budget - system_tokens

        messages = _trim_conversation(conversation, remaining)

        total = count_tokens(system_content) + count_messages_tokens(messages)
        log.debug(
            "build_prompt: model=%s budget=%d system=%d conv_msgs=%d total_est=%d",
            model or "global", budget, system_tokens, len(messages), total,
        )
        return [{"role": "system", "content": system_content}] + messages

    async def summarize(self, messages: list[dict], hint: str = "") -> str:
        if not messages:
            return ""
        text = "\n".join(
            f"{m['role'].upper()}: {m.get('content','')}"
            for m in messages
        )
        prompt = (
            f"Summarize the following conversation concisely, "
            f"preserving all technical decisions and code changes made.\n\n{text}"
        )
        if hint:
            prompt = f"Context: {hint}\n\n{prompt}"
        try:
            resp = await _http.post(
                f"{config.OLLAMA_URL}/api/chat",
                json={
                    "model":    config.OLLAMA_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream":   False,
                    "options":  {"num_predict": 512},
                },
            )
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")
        except Exception as e:
            log.warning("summarize failed: %s", e)
            return text[:2000] + "... [truncated]"


# ── Token budget resolution ───────────────────────────────────────────────────

def _resolve_token_budget(model: str) -> int:
    """
    Phase 4A.1: derive token budget from model's context_length in the registry
    when a model name is provided. Falls back to config.MAX_CONTEXT_TOKENS if:
      - model is empty string (legacy callers)
      - registry is not yet initialised
      - model is not in the catalog (logs a warning)
    """
    if not model:
        return config.MAX_CONTEXT_TOKENS - RESPONSE_BUDGET

    try:
        from model_registry import get_model_registry
        ctx_len = get_model_registry().get_context_length(model)
        return ctx_len - RESPONSE_BUDGET
    except RuntimeError:
        # Registry not initialised yet — safe fallback during tests or early startup
        log.debug("model_registry not initialised, using MAX_CONTEXT_TOKENS for budget")
        return config.MAX_CONTEXT_TOKENS - RESPONSE_BUDGET
    except Exception as e:
        log.warning("failed to resolve context length for model %r: %s", model, e)
        return config.MAX_CONTEXT_TOKENS - RESPONSE_BUDGET


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_system_block(system_prompt: str, task: str) -> str:
    parts = [system_prompt.strip()]
    if task:
        parts.append(f"\n\n## Current Task\n{task.strip()}")
    return "\n".join(parts)


def _format_codebase_context(chunks: list[dict]) -> str:
    lines = ["## Relevant Codebase Context"]
    for c in chunks:
        meta   = c.get("metadata", {})
        f      = meta.get("file", "unknown")
        symbol = meta.get("symbol", "")
        stype  = meta.get("symbol_type", "")
        header = f"### {f}"
        if symbol:
            header += f" — {stype} `{symbol}`"
        lines.append(f"\n{header}\n```\n{c['content'].strip()}\n```")
    return "\n".join(lines)


def _format_antipattern_context(antipatterns: list[dict]) -> str:
    """Phase 3.4: inject anti-pattern warnings into the system prompt."""
    lines = ["## Known Pitfalls — Avoid These"]
    for ap in antipatterns:
        meta = ap.get("metadata", {})
        name = meta.get("name", "antipattern")
        conf = meta.get("confidence", 1.0)
        conf_str = f" (confidence: {conf:.0%})" if conf < 1.0 else ""
        lines.append(f"\n⚠️  **{name}**{conf_str}\n{ap['content'].strip()}")
    return "\n".join(lines)


def _format_memory_context(memories: list[dict]) -> str:
    lines = ["## Relevant Past Context"]
    for m in memories:
        col = m.get("collection", "memory")
        lines.append(f"\n[{col}] {m['content'].strip()}")
    return "\n".join(lines)


def _trim_conversation(conversation: list[dict], token_budget: int) -> list[dict]:
    if not conversation:
        return []
    clean  = [
        {**m, "content": sanitize_context(m.get("content") or "")}
        for m in conversation
    ]
    recent = clean[-RECENT_MSG_KEEP:]
    older  = clean[:-RECENT_MSG_KEEP] if len(clean) > RECENT_MSG_KEEP else []

    recent_tokens = count_messages_tokens(recent)
    if recent_tokens >= token_budget:
        kept = []
        used = 0
        for msg in reversed(recent):
            t = count_tokens(msg.get("content", "")) + 4
            if used + t > token_budget:
                break
            kept.insert(0, msg)
            used += t
        return kept

    remaining    = token_budget - recent_tokens
    older_kept   = []
    older_tokens = 0
    for msg in reversed(older):
        t = count_tokens(msg.get("content", "")) + 4
        if older_tokens + t > remaining:
            break
        older_kept.insert(0, msg)
        older_tokens += t

    return older_kept + recent