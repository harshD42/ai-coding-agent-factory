"""
metrics.py — Token counting, request timing, agent activity tracking.

Phase 1: Stub — records nothing, returns empty summaries.
Phase 2: Full implementation with per-agent token counts and latency.
"""

import time
import logging
from typing import Optional

log = logging.getLogger("metrics")


class Metrics:
    def __init__(self):
        self._requests: list[dict] = []

    def record_request(
        self,
        agent_id: str,
        role: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        session_id: str = "",
    ) -> None:
        """Record a single agent model call. Phase 2 will persist to Redis."""
        self._requests.append({
            "agent_id":   agent_id,
            "role":       role,
            "tokens_in":  tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "session_id": session_id,
            "ts":         time.time(),
        })

    def get_summary(self) -> dict:
        """Return aggregate metrics across all recorded requests."""
        if not self._requests:
            return {
                "total_requests":  0,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "avg_latency_ms":  0.0,
                "by_role":         {},
            }
        by_role: dict[str, dict] = {}
        for r in self._requests:
            role = r["role"]
            if role not in by_role:
                by_role[role] = {"requests": 0, "tokens_in": 0, "tokens_out": 0}
            by_role[role]["requests"]   += 1
            by_role[role]["tokens_in"]  += r["tokens_in"]
            by_role[role]["tokens_out"] += r["tokens_out"]

        total_latency = sum(r["latency_ms"] for r in self._requests)
        return {
            "total_requests":   len(self._requests),
            "total_tokens_in":  sum(r["tokens_in"]  for r in self._requests),
            "total_tokens_out": sum(r["tokens_out"] for r in self._requests),
            "avg_latency_ms":   round(total_latency / len(self._requests), 1),
            "by_role":          by_role,
        }

    def get_session_summary(self, session_id: str) -> dict:
        """Return metrics for a specific session."""
        session_reqs = [r for r in self._requests if r["session_id"] == session_id]
        return {
            "session_id":   session_id,
            "requests":     len(session_reqs),
            "tokens_in":    sum(r["tokens_in"]  for r in session_reqs),
            "tokens_out":   sum(r["tokens_out"] for r in session_reqs),
        }

    def reset(self) -> None:
        """Clear all recorded metrics (useful for testing)."""
        self._requests.clear()


# Module-level singleton
metrics = Metrics()