"""
session_hooks.py — Lifecycle hooks for sessions.

Hooks:
    on_session_start  — load relevant past context from ChromaDB
    on_session_end    — save session, extract skills, record any failures
    on_failure        — record a failure to the failures collection
    extract_skills    — ask the model what reusable patterns emerged
"""

import logging
import time
import uuid
from typing import Optional

import httpx

import config
from memory_manager import MemoryManager

log = logging.getLogger("session_hooks")

_http = httpx.AsyncClient(timeout=120.0)


class SessionHooks:
    def __init__(self, mem: MemoryManager):
        self._mem = mem

    # ── on_session_start ──────────────────────────────────────────────────────

    async def on_session_start(self, session_id: str, task: str = "") -> dict:
        """
        Called when a new session begins.
        Returns relevant past context to seed the session.
        """
        past = []
        if task:
            past = await self._mem.recall(task, k=3)

        log.info("session_start  id=%s  past_context=%d items", session_id, len(past))
        return {
            "session_id":   session_id,
            "past_context": past,
            "started_at":   time.time(),
        }

    # ── on_session_end ────────────────────────────────────────────────────────

    async def on_session_end(
        self,
        session_id: str,
        summary: str,
        transcript: list[dict] = None,
        failures: list[dict]   = None,
    ) -> dict:
        """
        Called when a session ends.
        Saves the session summary, records failures, and attempts skill extraction.
        """
        # Save session summary
        await self._mem.save_session(
            session_id=session_id,
            content=summary,
            metadata={"ts": int(time.time()), "type": "session_end"},
        )

        # Record any failures
        for f in (failures or []):
            await self._mem.record_failure(
                session_id=session_id,
                task_id=f.get("task_id", str(uuid.uuid4())),
                description=f.get("description", ""),
                error=f.get("error", ""),
                approach=f.get("approach", ""),
            )

        # Extract skills from the transcript
        skill = None
        if transcript:
            skill = await self.extract_skills(session_id, transcript)

        log.info("session_end  id=%s  skill_extracted=%s", session_id, skill is not None)
        return {
            "session_id":      session_id,
            "saved":           True,
            "skill_extracted": skill is not None,
            "skill_name":      skill,
        }

    # ── on_failure ────────────────────────────────────────────────────────────

    async def on_failure(
        self,
        session_id: str,
        task_id: str,
        description: str,
        error: str,
        approach: str = "",
    ) -> None:
        """Record a failure immediately (not waiting for session end)."""
        await self._mem.record_failure(
            session_id=session_id,
            task_id=task_id,
            description=description,
            error=error,
            approach=approach,
        )
        log.info("on_failure recorded  session=%s  task=%s", session_id, task_id)

    # ── extract_skills ────────────────────────────────────────────────────────

    async def extract_skills(
        self, session_id: str, transcript: list[dict]
    ) -> Optional[str]:
        """
        Ask the model if any reusable patterns emerged from this session.
        If yes, saves to the skills collection and returns the skill name.
        Returns None if no skill was identified.
        """
        if not transcript:
            return None

        # Build a condensed transcript for the model
        lines = []
        for t in transcript[-10:]:   # last 10 entries to stay within context
            role    = t.get("role", "unknown")
            content = str(t.get("content", ""))[:300]
            lines.append(f"{role.upper()}: {content}")
        condensed = "\n".join(lines)

        prompt = (
            "Review this conversation and determine if a reusable engineering pattern "
            "or best practice emerged that would be worth remembering for future sessions.\n\n"
            "If yes, respond with:\n"
            "SKILL_NAME: <short name>\n"
            "SKILL_CONTENT: <description of the pattern, 2-5 sentences>\n\n"
            "If no clear reusable pattern emerged, respond with: NO_SKILL\n\n"
            f"Conversation:\n{condensed}"
        )

        try:
            resp = await _http.post(
                f"{config.OLLAMA_URL}/api/chat",
                json={
                    "model":    config.OLLAMA_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream":   False,
                    "options":  {"num_predict": 256},
                },
            )
            resp.raise_for_status()
            text = resp.json().get("message", {}).get("content", "")

            if "NO_SKILL" in text.upper():
                return None

            # Parse skill name and content
            skill_name    = _parse_field(text, "SKILL_NAME")
            skill_content = _parse_field(text, "SKILL_CONTENT")

            if skill_name and skill_content:
                await self._mem.save_skill(skill_name, skill_content)
                log.info("skill extracted: %s", skill_name)
                return skill_name

        except Exception as e:
            log.warning("skill extraction failed: %s", e)

        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_field(text: str, field: str) -> str:
    """Extract 'FIELD_NAME: value' from model output."""
    for line in text.splitlines():
        if line.strip().upper().startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return ""


# ── Singleton ─────────────────────────────────────────────────────────────────

_session_hooks: Optional[SessionHooks] = None


def get_session_hooks() -> SessionHooks:
    if _session_hooks is None:
        raise RuntimeError("SessionHooks not initialised")
    return _session_hooks


def init_session_hooks(mem: MemoryManager) -> SessionHooks:
    global _session_hooks
    _session_hooks = SessionHooks(mem)
    return _session_hooks