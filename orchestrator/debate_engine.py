"""
debate_engine.py — Multi-round architect vs reviewer debate engine.

Protocol:
    1. Architect produces initial plan
    2. Reviewer critiques it
    3. Architect revises based on critique
    4. Repeat up to MAX_DEBATE_ROUNDS
    5. Terminate early if reviewer signals APPROVE
    6. Return final plan + full transcript

Consensus detection:
    If the reviewer's response contains APPROVE (case-insensitive),
    the debate ends immediately regardless of remaining rounds.
"""

import logging
import time
from typing import AsyncIterator

import config
from agent_manager import AgentManager

log = logging.getLogger("debate")

APPROVE_SIGNALS = ["approve", "approved", "lgtm", "looks good", "no further changes"]


class DebateEngine:
    def __init__(self, agent_mgr: AgentManager):
        self._mgr = agent_mgr

    async def run(
        self,
        topic: str,
        session_id: str,
        initial_plan: str = "",
        max_rounds: int = None,
    ) -> dict:
        """
        Run a debate between architect and reviewer on the given topic.

        Args:
            topic:        The question or goal to debate
            session_id:   Session ID for memory isolation
            initial_plan: Optional pre-existing plan to start from
                          (if empty, architect generates one first)
            max_rounds:   Override MAX_DEBATE_ROUNDS from config

        Returns:
            {
                "final_plan":  str,
                "consensus":   bool,
                "rounds":      int,
                "transcript":  list[{round, role, content}],
                "session_id":  str,
            }
        """
        max_rounds = max_rounds or config.MAX_DEBATE_ROUNDS
        transcript = []
        plan       = initial_plan
        consensus  = False

        log.info("debate start  topic=%s  max_rounds=%d  session=%s",
                 topic[:60], max_rounds, session_id)

        # ── Phase 0: Generate initial plan if not provided ────────────────────
        if not plan:
            log.info("debate  phase=init  generating initial plan")
            result = await self._mgr.spawn_and_run(
                role="architect",
                task=f"Produce a detailed implementation plan for the following:\n\n{topic}",
                session_id=session_id,
            )
            plan = result.get("result", "") or ""
            transcript.append({
                "round":   0,
                "role":    "architect",
                "phase":   "initial_plan",
                "content": plan,
            })
            log.info("debate  phase=init  plan length=%d chars", len(plan))

        # ── Debate rounds ─────────────────────────────────────────────────────
        for rnd in range(1, max_rounds + 1):
            log.info("debate  round=%d/%d", rnd, max_rounds)

            # Reviewer critiques
            critique_prompt = (
                f"Review the following implementation plan critically.\n"
                f"Identify any bugs, missing edge cases, security issues, "
                f"or architectural problems.\n"
                f"If the plan is acceptable as-is, respond with APPROVE.\n\n"
                f"## Plan\n{plan}"
            )
            critique_result = await self._mgr.spawn_and_run(
                role="reviewer",
                task=critique_prompt,
                session_id=session_id,
            )
            critique = critique_result.get("result", "") or ""
            transcript.append({
                "round":   rnd,
                "role":    "reviewer",
                "phase":   "critique",
                "content": critique,
            })
            log.info("debate  round=%d  reviewer done  length=%d", rnd, len(critique))

            # Check for consensus
            if _signals_approval(critique):
                consensus = True
                log.info("debate  round=%d  consensus reached", rnd)
                break

            # Architect revises (unless this is the last round)
            if rnd < max_rounds:
                revise_prompt = (
                    f"You previously produced this plan:\n\n{plan}\n\n"
                    f"The reviewer raised the following concerns:\n\n{critique}\n\n"
                    f"Revise your plan to address these concerns. "
                    f"Be specific about what changed and why."
                )
                revise_result = await self._mgr.spawn_and_run(
                    role="architect",
                    task=revise_prompt,
                    session_id=session_id,
                )
                plan = revise_result.get("result", "") or plan
                transcript.append({
                    "round":   rnd,
                    "role":    "architect",
                    "phase":   "revision",
                    "content": plan,
                })
                log.info("debate  round=%d  architect revised  length=%d", rnd, len(plan))

        rounds_completed = len([t for t in transcript if t["role"] == "reviewer"])
        log.info("debate end  rounds=%d  consensus=%s  session=%s",
                 rounds_completed, consensus, session_id)

        return {
            "final_plan": plan,
            "consensus":  consensus,
            "rounds":     rounds_completed,
            "transcript": transcript,
            "session_id": session_id,
            "topic":      topic,
        }

    async def stream(
        self, topic: str, session_id: str, max_rounds: int = None
    ) -> AsyncIterator[str]:
        """
        Run the debate and yield transcript entries as newline-delimited JSON.
        Useful for streaming progress back to the client.
        """
        import json
        max_rounds = max_rounds or config.MAX_DEBATE_ROUNDS
        result = await self.run(topic=topic, session_id=session_id, max_rounds=max_rounds)
        for entry in result["transcript"]:
            yield json.dumps(entry) + "\n"
        yield json.dumps({
            "round":     "final",
            "consensus": result["consensus"],
            "rounds":    result["rounds"],
            "final_plan": result["final_plan"],
        }) + "\n"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _signals_approval(text: str) -> bool:
    """Return True if the reviewer's response signals approval."""
    lower = text.lower()
    return any(signal in lower for signal in APPROVE_SIGNALS)


# ── Singleton (init in main.py) ───────────────────────────────────────────────
_debate_engine = None

def get_debate_engine() -> DebateEngine:
    if _debate_engine is None:
        raise RuntimeError("DebateEngine not initialised")
    return _debate_engine

def init_debate_engine(agent_mgr: AgentManager) -> DebateEngine:
    global _debate_engine
    _debate_engine = DebateEngine(agent_mgr)
    return _debate_engine