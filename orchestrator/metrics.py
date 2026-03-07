"""
metrics.py — Token counting, request timing, agent activity tracking.

Step 2.3: wired into agent_manager._run_agent() before/after model call.
Token counts are parsed from the Ollama/vLLM response usage field.
"""

import time
import logging
from collections import defaultdict
from typing import Optional

log = logging.getLogger("metrics")


class Metrics:
    def __init__(self):
        self._requests: list[dict] = []

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_request(
        self,
        agent_id:   str,
        role:       str,
        tokens_in:  int   = 0,
        tokens_out: int   = 0,
        latency_ms: float = 0.0,
        session_id: str   = "",
        status:     str   = "done",   # done | failed | killed
    ) -> None:
        """
        Record one agent model call.

        Called by agent_manager._run_agent() after the model response arrives.
        tokens_in / tokens_out are parsed from the response usage field;
        they fall back to 0 if the backend doesn't return usage.
        """
        entry = {
            "agent_id":   agent_id,
            "role":       role,
            "tokens_in":  tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "session_id": session_id,
            "status":     status,
            "ts":         time.time(),
        }
        self._requests.append(entry)
        log.debug(
            "metrics  agent=%s  role=%s  in=%d  out=%d  latency=%.0fms  status=%s",
            agent_id, role, tokens_in, tokens_out, latency_ms, status,
        )

    # ── Summaries ─────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Return aggregate metrics across all recorded requests."""
        if not self._requests:
            return {
                "total_requests":   0,
                "total_tokens_in":  0,
                "total_tokens_out": 0,
                "avg_latency_ms":   0.0,
                "by_role":          {},
            }

        by_role: dict[str, dict] = defaultdict(
            lambda: {"requests": 0, "tokens_in": 0, "tokens_out": 0,
                     "failed": 0, "total_latency_ms": 0.0}
        )
        for r in self._requests:
            role = r["role"]
            by_role[role]["requests"]        += 1
            by_role[role]["tokens_in"]       += r["tokens_in"]
            by_role[role]["tokens_out"]      += r["tokens_out"]
            by_role[role]["total_latency_ms"] += r["latency_ms"]
            if r.get("status") in ("failed", "killed"):
                by_role[role]["failed"] += 1

        # Compute per-role avg_latency
        for role_data in by_role.values():
            n = role_data["requests"]
            role_data["avg_latency_ms"] = round(
                role_data.pop("total_latency_ms") / n, 1
            ) if n else 0.0

        total_latency = sum(r["latency_ms"] for r in self._requests)
        return {
            "total_requests":   len(self._requests),
            "total_tokens_in":  sum(r["tokens_in"]  for r in self._requests),
            "total_tokens_out": sum(r["tokens_out"] for r in self._requests),
            "avg_latency_ms":   round(total_latency / len(self._requests), 1),
            "by_role":          dict(by_role),
        }

    def get_session_summary(self, session_id: str) -> dict:
        """Return metrics for a specific session."""
        reqs = [r for r in self._requests if r["session_id"] == session_id]
        total_lat = sum(r["latency_ms"] for r in reqs)
        return {
            "session_id":   session_id,
            "requests":     len(reqs),
            "tokens_in":    sum(r["tokens_in"]  for r in reqs),
            "tokens_out":   sum(r["tokens_out"] for r in reqs),
            "avg_latency_ms": round(total_lat / len(reqs), 1) if reqs else 0.0,
        }

    def reset(self) -> None:
        """Clear all recorded metrics (useful for testing)."""
        self._requests.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_usage(response: dict) -> tuple[int, int]:
    """
    Extract (tokens_in, tokens_out) from a model response dict.

    Handles both Ollama and vLLM response shapes:
      Ollama: {"prompt_eval_count": N, "eval_count": M}
              or {"usage": {"prompt_tokens": N, "completion_tokens": M}}
      vLLM:   {"usage": {"prompt_tokens": N, "completion_tokens": M}}
    Returns (0, 0) if the backend doesn't include usage.
    """
    # OpenAI-style usage block (vLLM and newer Ollama)
    usage = response.get("usage") or {}
    if usage:
        return (
            int(usage.get("prompt_tokens",     0)),
            int(usage.get("completion_tokens", 0)),
        )

    # Ollama native flat fields
    tokens_in  = int(response.get("prompt_eval_count", 0))
    tokens_out = int(response.get("eval_count",         0))
    return tokens_in, tokens_out


# ── Module-level singleton ────────────────────────────────────────────────────

metrics = Metrics()