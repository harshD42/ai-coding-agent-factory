"""
models.py — Pydantic schemas for the OpenAI-compatible API surface.

Phase 4B.1 additions:
  - AgentMessageRequest  — body for POST /v1/agents/{agent_id}/message
  - SessionConfigRequest — body for POST /v1/sessions (create)

Phase 4B.2 additions (forward-declared here so models.py is the single
source of truth for all wire types):
  - WSEventType  — enum of structured event types on the agent bus / WebSocket
  - WSEvent      — the envelope for all structured bus events

These are used by:
  - agent_bus.py (4B.3) — publishes WSEvent
  - main.py (4B.2) — WebSocket handler yields WSEvent
  - TUI client (4B.4) — deserializes WSEvent from WebSocket
"""

import time
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


# ── Request ───────────────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str                       # "system" | "user" | "assistant" | "tool"
    content: str | list | None = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = "orchestrator"
    messages: list[Message]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    # Accept (and forward) any extra fields Cline may send
    model_config = {"extra": "allow"}


# ── Response ──────────────────────────────────────────────────────────────────

class ChoiceDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class Choice(BaseModel):
    index: int = 0
    message: Optional[Message] = None
    delta: Optional[ChoiceDelta] = None
    finish_reason: Optional[str] = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Optional[Usage] = None


# ── Phase 4B.1: Agent messaging ───────────────────────────────────────────────

class AgentMessageRequest(BaseModel):
    """Body for POST /v1/agents/{agent_id}/message."""
    message: str
    sender:  str = "user"   # "user" | "architect" | system role


class SessionConfigRequest(BaseModel):
    """Body for POST /v1/sessions (create managed session)."""
    task:       str
    session_id: Optional[str] = None      # auto-generated if absent
    models:     Optional[dict[str, str]] = None   # role → model_name overrides
    metadata:   Optional[dict[str, Any]] = None


# ── Phase 4B.2: WebSocket / bus event types ───────────────────────────────────

class WSEventType(str, Enum):
    """
    Structured event types that flow over the agent bus and WebSocket.

    Token chunks are NOT events — they flow directly through the SSE
    endpoint (GET /v1/agents/{id}/stream) to avoid overhead on every token.
    Only coarse-grained state-change events appear here.
    """
    TOKEN         = "token"           # reserved — not used on bus, SSE only
    WORK_COMPLETE = "work_complete"   # agent finished a task successfully
    WORK_FAILED   = "work_failed"     # agent task failed
    PATCH_APPLIED = "patch_applied"   # patch successfully committed to workspace
    TEST_RESULT   = "test_result"     # pytest run complete (pass/fail summary)
    INTERRUPT     = "interrupt"       # user message routed to specific agent
    STATUS        = "status"          # generic status update (session lifecycle)
    DEBATE_POINT  = "debate_point"    # reviewer critique → architect


class WSEvent(BaseModel):
    """
    Envelope for all structured events on the agent bus and WebSocket.

    Published by: agent_manager._run_agent() (work_complete/failed),
                  patch_queue._apply_patch() (patch_applied — 4B.3),
                  debate_engine (debate_point — 4B.3),
                  session_manager (status — lifecycle transitions)

    Consumed by:  AgentBus.subscribe_architect() (architect agent loop)
                  AgentBus.subscribe_session() (WebSocket → TUI)
    """
    type:       WSEventType
    session_id: str
    agent_id:   Optional[str] = None
    payload:    dict[str, Any] = Field(default_factory=dict)
    ts:         float = Field(default_factory=time.time)