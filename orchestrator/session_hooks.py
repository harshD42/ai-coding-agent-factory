"""
session_hooks.py — Lifecycle hooks for sessions.

Phase 3 additions:
  3.4  on_session_end() now calls _mine_failure_patterns() after saving.
       If >= N_FAILURES_THRESHOLD similar failures exist, the model is asked
       to produce an anti-pattern skill which is saved to ChromaDB with
       metadata type=antipattern.
  3.2  record_training_example() called when a patch is applied + tests pass.

Phase 4A.3 addition:
  Confidence scoring on skill and antipattern extraction. The extracting
  model is asked to self-rate confidence (0.0–1.0). This score is stored
  in ChromaDB metadata so context_manager can filter low-confidence items
  (threshold 0.6) from agent prompts. Items without a confidence field
  default to 1.0 (trusted) for backwards compatibility.
"""

import logging
import time
import uuid
from typing import Optional

import httpx

import config
from memory_manager import MemoryManager
from fine_tune_collector import record_success as ft_record

log = logging.getLogger("session_hooks")

_http = httpx.AsyncClient(timeout=120.0)


class SessionHooks:
    def __init__(self, mem: MemoryManager):
        self._mem = mem

    # ── on_session_start ──────────────────────────────────────────────────────

    async def on_session_start(self, session_id: str, task: str = "") -> dict:
        past = []
        if task:
            past = await self._mem.recall(task, k=3)
        log.info("session_start  id=%s  past_context=%d", session_id, len(past))
        return {
            "session_id":   session_id,
            "past_context": past,
            "started_at":   time.time(),
        }

    # ── on_session_end ────────────────────────────────────────────────────────

    async def on_session_end(
        self,
        session_id: str,
        summary:    str,
        transcript: list[dict] = None,
        failures:   list[dict] = None,
    ) -> dict:
        await self._mem.save_session(
            session_id=session_id,
            content=summary,
            metadata={"ts": int(time.time()), "type": "session_end"},
        )

        for f in (failures or []):
            await self._mem.record_failure(
                session_id=session_id,
                task_id=f.get("task_id", str(uuid.uuid4())),
                description=f.get("description", ""),
                error=f.get("error", ""),
                approach=f.get("approach", ""),
            )

        skill       = None
        antipattern = None

        if transcript:
            skill = await self.extract_skills(session_id, transcript)

        if failures:
            antipattern = await self._mine_failure_patterns(session_id, summary)

        log.info("session_end  id=%s  skill=%s  antipattern=%s",
                 session_id, skill, antipattern)
        return {
            "session_id":         session_id,
            "saved":              True,
            "skill_extracted":    skill is not None,
            "skill_name":         skill,
            "antipattern_mined":  antipattern is not None,
            "antipattern_name":   antipattern,
        }

    # ── on_failure ────────────────────────────────────────────────────────────

    async def on_failure(
        self,
        session_id:  str,
        task_id:     str,
        description: str,
        error:       str,
        approach:    str = "",
    ) -> None:
        await self._mem.record_failure(
            session_id=session_id,
            task_id=task_id,
            description=description,
            error=error,
            approach=approach,
        )
        log.info("on_failure recorded  session=%s  task=%s", session_id, task_id)

    # ── Step 3.2: Training data recording ─────────────────────────────────────

    async def record_training_example(
        self,
        session_id:  str,
        agent_id:    str,
        task:        str,
        diff:        str,
        context:     str = "",
        tokens_in:   int = 0,
        tokens_out:  int = 0,
    ) -> bool:
        """
        Called by patch_queue.test_fix_loop() when a patch applies AND
        tests pass — records the (task, diff) pair for fine-tuning.
        """
        return await ft_record(
            session_id=session_id,
            agent_id=agent_id,
            task=task,
            diff=diff,
            context=context,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    # ── extract_skills ────────────────────────────────────────────────────────

    async def extract_skills(
        self, session_id: str, transcript: list[dict]
    ) -> Optional[str]:
        """
        Review the transcript and extract a reusable engineering pattern if one
        emerged.

        Phase 4A.3: the model is asked to self-rate extraction confidence
        (0.0–1.0). This is stored in ChromaDB metadata so context_manager
        can filter low-confidence items (threshold 0.6) from agent prompts.
        """
        if not transcript:
            return None

        lines = []
        for t in transcript[-10:]:
            role    = t.get("role", "unknown")
            content = str(t.get("content", ""))[:300]
            lines.append(f"{role.upper()}: {content}")
        condensed = "\n".join(lines)

        prompt = (
            "Review this conversation and determine if a reusable engineering pattern "
            "or best practice emerged that would be worth remembering for future sessions.\n\n"
            "If yes, respond with:\n"
            "SKILL_NAME: <short name>\n"
            "SKILL_CONTENT: <description of the pattern, 2-5 sentences>\n"
            "CONFIDENCE: <float 0.0-1.0 — how confident you are this is genuinely reusable>\n\n"
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

            skill_name    = _parse_field(text, "SKILL_NAME")
            skill_content = _parse_field(text, "SKILL_CONTENT")
            confidence    = _parse_confidence(text)   # Phase 4A.3

            if skill_name and skill_content:
                await self._mem.save_skill(
                    skill_name,
                    skill_content,
                    metadata={"confidence": confidence},   # Phase 4A.3
                )
                log.info(
                    "skill extracted: %s  confidence=%.2f", skill_name, confidence
                )
                return skill_name

        except Exception as e:
            log.warning("skill extraction failed: %s", e)
        return None

    # ── Step 3.4: Failure pattern mining ─────────────────────────────────────

    async def _mine_failure_patterns(
        self, session_id: str, context: str = ""
    ) -> Optional[str]:
        """
        Check if enough similar failures have accumulated to extract an
        anti-pattern skill.

        Phase 4A.3: confidence scoring applied to extracted antipatterns.
        Same 0.0–1.0 scale, stored in metadata, filtered at 0.6 in
        context_manager.
        """
        threshold = config.N_FAILURES_THRESHOLD
        try:
            clusters = await self._mem.cluster_failures(
                query=context or "error failure", k=30
            )
        except Exception as e:
            log.warning("_mine_failure_patterns: cluster_failures failed: %s", e)
            return None

        for cluster in clusters:
            if len(cluster) < threshold:
                continue

            examples = "\n\n".join(
                f"Failure {i+1}:\n{f['content'][:400]}"
                for i, f in enumerate(cluster[:5])
            )

            prompt = (
                f"The following {len(cluster)} similar failures have occurred repeatedly.\n"
                "Identify the common anti-pattern and describe what to AVOID in future.\n\n"
                "Respond with:\n"
                "ANTIPATTERN_NAME: <short name>\n"
                "ANTIPATTERN_CONTENT: <what to avoid and why, 2-5 sentences>\n"
                "CONFIDENCE: <float 0.0-1.0 — how confident you are this is a real pattern>\n\n"
                "If these failures don't share a clear pattern, respond: NO_PATTERN\n\n"
                f"Examples:\n{examples}"
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

                if "NO_PATTERN" in text.upper():
                    continue

                ap_name    = _parse_field(text, "ANTIPATTERN_NAME")
                ap_content = _parse_field(text, "ANTIPATTERN_CONTENT")
                confidence = _parse_confidence(text)   # Phase 4A.3

                if ap_name and ap_content:
                    await self._mem.save_skill(
                        name=f"antipattern:{ap_name}",
                        content=ap_content,
                        metadata={
                            "type":         "antipattern",
                            "cluster_size": len(cluster),
                            "confidence":   confidence,   # Phase 4A.3
                        },
                    )
                    log.info(
                        "antipattern mined: %s  cluster_size=%d  confidence=%.2f",
                        ap_name, len(cluster), confidence,
                    )
                    return ap_name

            except Exception as e:
                log.warning("_mine_failure_patterns: model call failed: %s", e)

        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_field(text: str, field: str) -> str:
    for line in text.splitlines():
        if line.strip().upper().startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return ""


def _parse_confidence(text: str) -> float:
    """
    Phase 4A.3: parse CONFIDENCE field from model response.

    Returns a float in [0.0, 1.0]. Defaults to 0.8 if the field is
    missing or unparseable — a moderate trust level that passes the
    context_manager threshold (0.6) without claiming full certainty.
    """
    raw = _parse_field(text, "CONFIDENCE")
    if not raw:
        return 0.8   # default: moderate confidence
    try:
        val = float(raw)
        return max(0.0, min(1.0, val))   # clamp to valid range
    except (ValueError, TypeError):
        log.debug("_parse_confidence: could not parse %r, defaulting to 0.8", raw)
        return 0.8


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