"""
models.py — Pydantic schemas for the OpenAI-compatible API surface.

Only what's needed for Step 3 (proxy). Extended in later steps.
"""

from typing import Any, Optional
from pydantic import BaseModel


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