"""
command_parser.py — Parse /commands from incoming chat messages and route
them to the appropriate orchestrator endpoint instead of the model.

Supported commands:
    /architect <task>     — spawn architect agent
    /debate <topic>       — run architect vs reviewer debate
    /execute              — execute current task queue
    /review <text>        — spawn reviewer agent
    /test <task>          — spawn tester agent
    /learn                — extract skill from current session
    /memory <query>       — search past sessions
    /status               — show agent + queue status
    /index                — re-index the codebase

Commands are detected in the last user message.
If a command is found, it is handled directly and a synthetic assistant
response is returned — the message never reaches the model.
"""

import logging
import re
import uuid
from typing import Optional

log = logging.getLogger("command_parser")

# Map command name → (pattern, needs_args)
_COMMANDS: dict[str, tuple[re.Pattern, bool]] = {
    "architect": (re.compile(r"^/architect\s+(.+)$",  re.DOTALL | re.IGNORECASE), True),
    "debate":    (re.compile(r"^/debate\s+(.+)$",     re.DOTALL | re.IGNORECASE), True),
    "review":    (re.compile(r"^/review\s+(.+)$",     re.DOTALL | re.IGNORECASE), True),
    "test":      (re.compile(r"^/test\s+(.+)$",       re.DOTALL | re.IGNORECASE), True),
    "memory":    (re.compile(r"^/memory\s+(.+)$",     re.DOTALL | re.IGNORECASE), True),
    "execute":   (re.compile(r"^/execute\s*$",        re.IGNORECASE),             False),
    "learn":     (re.compile(r"^/learn\s*$",          re.IGNORECASE),             False),
    "status":    (re.compile(r"^/status\s*$",         re.IGNORECASE),             False),
    "index":     (re.compile(r"^/index\s*$",          re.IGNORECASE),             False),
}


class ParsedCommand:
    def __init__(self, name: str, args: str = "", session_id: str = ""):
        self.name       = name
        self.args       = args.strip()
        self.session_id = session_id or str(uuid.uuid4())

    def __repr__(self):
        return f"ParsedCommand(name={self.name!r}, args={self.args[:40]!r})"


def _extract_text(content) -> str:
    """Extract plain text from a string or Cline-style list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                # {"type": "text", "text": "..."} format
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return str(content) if content else ""


def parse(messages: list[dict]) -> Optional[ParsedCommand]:
    """
    Scan the last user message for a /command.
    Returns a ParsedCommand if found, None otherwise.
    """
    if not messages:
        return None

    # Find the last user message
    last_user = None
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m
            break

    if not last_user:
        return None

    content = _extract_text(last_user.get("content") or "").strip()
    if not content.startswith("/"):
        return None

    for name, (pattern, needs_args) in _COMMANDS.items():
        m = pattern.match(content)
        if m:
            args = m.group(1).strip() if needs_args else ""
            log.info("parsed command: /%s  args=%s", name, args[:60])
            return ParsedCommand(name=name, args=args)

    # Unknown /command — tell the user
    cmd_word = content.split()[0]
    log.info("unknown command: %s", cmd_word)
    return ParsedCommand(name="unknown", args=cmd_word)


def help_text() -> str:
    return (
        "**Available commands:**\n"
        "- `/architect <task>` — generate an implementation plan\n"
        "- `/debate <topic>` — run architect vs reviewer debate\n"
        "- `/review <text>` — review code or a plan\n"
        "- `/test <task>` — write tests for a task\n"
        "- `/execute` — execute the current task queue\n"
        "- `/memory <query>` — search past sessions\n"
        "- `/learn` — extract a skill from this session\n"
        "- `/status` — show system status\n"
        "- `/index` — re-index the codebase\n"
    )