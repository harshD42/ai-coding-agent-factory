"""
context_manager.py — 5-tier priority context builder with token budgeting.

Priority tiers (never cut → cut first):
    P1  Current task description + agent system prompt       [never cut]
    P2  Relevant codebase files (via embedding search)       [never cut]
    P3  Recent conversation messages (last N verbatim)       [cut last]
    P4  Past session memories + failure records              [cut second]
    P5  Older conversation turns (summarized)                [cut first]

Token budget:
    MAX_CONTEXT_TOKENS - RESPONSE_BUDGET = available for prompt
    If prompt exceeds budget: trim P5 → P4 → P3 until it fits.
    P1 and P2 are never trimmed.
"""

import logging
from typing import Optional

import httpx

import config
from memory_manager import MemoryManager
from utils import count_tokens, count_messages_tokens, sanitize_context

log = logging.getLogger("context")

# Reserve this many tokens for the model's response
RESPONSE_BUDGET    = 2048
# How many recent messages to always keep verbatim (P3)
RECENT_MSG_KEEP    = 6
# Max memory results to include (P4)
MEMORY_RESULTS_K   = 3
# Max codebase chunks to include (P2)
CODEBASE_K         = 4

_http = httpx.AsyncClient(timeout=120.0)


class ContextManager:
    def __init__(self, mem: MemoryManager):
        self._mem = mem

    # ── Public API ────────────────────────────────────────────────────────────

    async def build_prompt(
        self,
        *,
        task: str,
        system_prompt: str,
        conversation: list[dict],
        session_id: str = "default",
        include_codebase: bool = True,
        include_memories: bool = True,
    ) -> list[dict]:
        """
        Build a token-bounded message list ready to send to a model.

        Returns a list of {role, content} dicts with:
          - system message (P1: system_prompt + task)
          - optional codebase context injected into system (P2)
          - optional memory context injected into system (P4)
          - trimmed conversation history (P3 + P5 summary)
        """
        budget = config.MAX_CONTEXT_TOKENS - RESPONSE_BUDGET

        # ── P1: system prompt + task (never cut) ──────────────────────────────
        p1 = _build_system_block(system_prompt, task)
        p1_tokens = count_tokens(p1)

        # ── P2: codebase context (never cut) ──────────────────────────────────
        p2 = ""
        if include_codebase and task:
            chunks = await self._mem.search_codebase(task, k=CODEBASE_K)
            if chunks:
                p2 = _format_codebase_context(chunks)

        # ── P4: memory context ────────────────────────────────────────────────
        p4 = ""
        if include_memories and task:
            memories = await self._mem.recall(task, k=MEMORY_RESULTS_K)
            if memories:
                p4 = _format_memory_context(memories)

        # ── Assemble system message ───────────────────────────────────────────
        system_content = p1
        if p2:
            system_content += f"\n\n{p2}"
        if p4:
            system_content += f"\n\n{p4}"
        system_content = sanitize_context(system_content)
        system_tokens  = count_tokens(system_content)

        remaining = budget - system_tokens
        if remaining < 512:
            # System alone is too large — drop P4 then P2 if needed
            log.warning("System context too large (%d tokens), dropping memories", system_tokens)
            system_content = sanitize_context(p1 + (f"\n\n{p2}" if p2 else ""))
            system_tokens  = count_tokens(system_content)
            remaining      = budget - system_tokens

        # ── P3 + P5: conversation history ─────────────────────────────────────
        messages = _trim_conversation(conversation, remaining)

        result = [{"role": "system", "content": system_content}] + messages
        total  = count_tokens(system_content) + count_messages_tokens(messages)
        log.debug(
            "build_prompt: system=%d conv_msgs=%d total_est=%d budget=%d",
            system_tokens, len(messages), total, budget,
        )
        return result

    async def summarize(self, messages: list[dict], hint: str = "") -> str:
        """
        Ask the model to summarize a list of conversation messages.
        Used for P5 compression of older turns.
        """
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
            # Fallback: just truncate
            return text[:2000] + "... [truncated]"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_system_block(system_prompt: str, task: str) -> str:
    parts = [system_prompt.strip()]
    if task:
        parts.append(f"\n\n## Current Task\n{task.strip()}")
    return "\n".join(parts)


def _format_codebase_context(chunks: list[dict]) -> str:
    lines = ["## Relevant Codebase Context"]
    for c in chunks:
        f = c.get("metadata", {}).get("file", "unknown")
        lines.append(f"\n### {f}\n```\n{c['content'].strip()}\n```")
    return "\n".join(lines)


def _format_memory_context(memories: list[dict]) -> str:
    lines = ["## Relevant Past Context"]
    for m in memories:
        col = m.get("collection", "memory")
        lines.append(f"\n[{col}] {m['content'].strip()}")
    return "\n".join(lines)


def _trim_conversation(conversation: list[dict], token_budget: int) -> list[dict]:
    """
    Fit conversation history into token_budget.

    Strategy:
      1. Always keep the last RECENT_MSG_KEEP messages verbatim (P3).
      2. If older messages exist (P5), include them only if budget allows.
      3. If still over budget, drop older messages one by one from the front.
    """
    if not conversation:
        return []

    # Sanitize all messages
    clean = [
        {**m, "content": sanitize_context(m.get("content") or "")}
        for m in conversation
    ]

    recent = clean[-RECENT_MSG_KEEP:]
    older  = clean[:-RECENT_MSG_KEEP] if len(clean) > RECENT_MSG_KEEP else []

    recent_tokens = count_messages_tokens(recent)

    if recent_tokens >= token_budget:
        # Even recent messages are too large — keep as many as fit from the end
        kept = []
        used = 0
        for msg in reversed(recent):
            t = count_tokens(msg.get("content", "")) + 4
            if used + t > token_budget:
                break
            kept.insert(0, msg)
            used += t
        return kept

    # Try to prepend older messages within remaining budget
    remaining   = token_budget - recent_tokens
    older_kept  = []
    older_tokens = 0
    for msg in reversed(older):
        t = count_tokens(msg.get("content", "")) + 4
        if older_tokens + t > remaining:
            break
        older_kept.insert(0, msg)
        older_tokens += t

    return older_kept + recent