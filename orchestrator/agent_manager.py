"""
agent_manager.py — Spawn, track, kill agents + task decomposition + watchdog.

Each agent has:
  - A role (architect / coder / reviewer / tester / documenter)
  - A private conversation buffer (in memory, keyed by agent_id)
  - A watchdog timeout (MAX_AGENT_RUNTIME seconds)
  - Isolated memory: agents never see each other's reasoning

Agents never touch the filesystem directly. They produce text/diffs which
the orchestrator validates and applies via the patch queue (Step 7).
"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

import config
from context_manager import ContextManager
from memory_manager import MemoryManager
from models import ChatCompletionRequest, Message
import router

log = logging.getLogger("agent_manager")

# ── Agent state ───────────────────────────────────────────────────────────────

class Agent:
    def __init__(self, agent_id: str, role: str, session_id: str):
        self.agent_id   = agent_id
        self.role       = role
        self.session_id = session_id
        self.status     = "idle"        # idle | running | done | failed | killed
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.ended_at:   Optional[float] = None
        self.task:       Optional[str]   = None
        self.result:     Optional[str]   = None
        self.error:      Optional[str]   = None
        # Private conversation buffer — never shared with other agents
        self._history:   list[dict]      = []

    def to_dict(self) -> dict:
        return {
            "agent_id":   self.agent_id,
            "role":       self.role,
            "session_id": self.session_id,
            "status":     self.status,
            "task":       self.task,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at":   self.ended_at,
            "error":      self.error,
        }


# ── AgentManager ──────────────────────────────────────────────────────────────

class AgentManager:
    def __init__(self, mem: MemoryManager):
        self._mem:    MemoryManager  = mem
        self._ctx:    ContextManager = ContextManager(mem)
        self._agents: dict[str, Agent] = {}   # agent_id → Agent

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _new_agent(self, role: str, session_id: str) -> Agent:
        agent_id = f"{role}-{uuid.uuid4().hex[:8]}"
        agent    = Agent(agent_id, role, session_id)
        self._agents[agent_id] = agent
        log.info("spawned agent  id=%s  role=%s  session=%s", agent_id, role, session_id)
        return agent

    async def spawn_and_run(
        self,
        role: str,
        task: str,
        session_id: str = "default",
        extra_context: str = "",
    ) -> dict:
        """
        Spawn an agent for the given role, run it with the task, return result.
        Enforces MAX_AGENT_RUNTIME watchdog timeout.
        """
        agent = self._new_agent(role, session_id)
        agent.task       = task
        agent.status     = "running"
        agent.started_at = time.time()

        try:
            result = await asyncio.wait_for(
                self._run_agent(agent, task, extra_context),
                timeout=config.MAX_AGENT_RUNTIME,
            )
            agent.status   = "done"
            agent.result   = result
            agent.ended_at = time.time()
            log.info("agent done  id=%s  elapsed=%.1fs", agent.agent_id,
                     agent.ended_at - agent.started_at)
            return {"agent_id": agent.agent_id, "role": role, "result": result, "status": "done"}

        except asyncio.TimeoutError:
            agent.status   = "killed"
            agent.error    = f"Timed out after {config.MAX_AGENT_RUNTIME}s"
            agent.ended_at = time.time()
            log.error("agent timed out  id=%s", agent.agent_id)
            await self._mem.record_failure(
                session_id, agent.agent_id, task,
                error=agent.error, approach=f"role={role}"
            )
            return {"agent_id": agent.agent_id, "role": role,
                    "result": None, "status": "killed", "error": agent.error}

        except Exception as e:
            agent.status   = "failed"
            agent.error    = str(e)
            agent.ended_at = time.time()
            log.exception("agent failed  id=%s: %s", agent.agent_id, e)
            await self._mem.record_failure(
                session_id, agent.agent_id, task, error=str(e), approach=f"role={role}"
            )
            return {"agent_id": agent.agent_id, "role": role,
                    "result": None, "status": "failed", "error": str(e)}

    async def _run_agent(self, agent: Agent, task: str, extra_context: str) -> str:
        """Build context, call the model, return response text."""
        system_prompt = _load_agent_prompt(agent.role)
        if extra_context:
            system_prompt += f"\n\n## Additional Context\n{extra_context}"

        # Build token-bounded prompt using context manager
        messages = await self._ctx.build_prompt(
            task=task,
            system_prompt=system_prompt,
            conversation=agent._history,
            session_id=agent.session_id,
        )

        # Call model via router (role-aware, health-checked)
        req = ChatCompletionRequest(
            model="orchestrator",
            messages=[Message(**m) for m in messages],
            stream=False,
        )
        result = await router.dispatch(req, role=agent.role, messages=messages)

        content = ""
        if isinstance(result, dict):
            choices = result.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                content = msg.get("content", "") if isinstance(msg, dict) else ""

        # Append to agent's private history
        agent._history.append({"role": "user",      "content": task})
        agent._history.append({"role": "assistant",  "content": content})

        return content

    # ── Task decomposition ────────────────────────────────────────────────────

    async def decompose_plan_to_tasks(
        self, plan: str, session_id: str = "default"
    ) -> list[dict]:
        """
        Ask the architect model to decompose a plan into a JSON task DAG.
        Returns a list of task dicts: {id, role, desc, deps}.
        """
        prompt = (
            "Decompose the following plan into discrete implementation tasks.\n"
            "Output ONLY a JSON array. Each item must have exactly these fields:\n"
            '  {"id": "t1", "role": "coder|tester|architect|reviewer|documenter", '
            '"desc": "what to do", "deps": ["t0"]}\n'
            "Rules:\n"
            "- deps must reference ids that appear earlier in the array\n"
            "- no circular dependencies\n"
            "- tester tasks depend on the coder tasks they test\n"
            "- first tasks have deps: []\n\n"
            f"Plan:\n{plan}"
        )

        result = await self.spawn_and_run(
            role="architect", task=prompt, session_id=session_id
        )
        raw = result.get("result", "") or ""

        try:
            # Strip markdown fences if present
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            tasks = json.loads(clean.strip())
            validated = _validate_task_dag(tasks)
            log.info("decomposed plan into %d tasks", len(validated))
            return validated
        except Exception as e:
            log.warning("task decomposition parse failed: %s\nraw=%s", e, raw[:500])
            return []

    # ── Status / listing ──────────────────────────────────────────────────────

    def get_status(self) -> dict:
        agents = list(self._agents.values())
        return {
            "total":   len(agents),
            "running": sum(1 for a in agents if a.status == "running"),
            "done":    sum(1 for a in agents if a.status == "done"),
            "failed":  sum(1 for a in agents if a.status in ("failed", "killed")),
        }

    def list_agents(self) -> list[dict]:
        return [a.to_dict() for a in self._agents.values()]

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        return self._agents.get(agent_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_agent_prompt(role: str) -> str:
    """Load system prompt from agents/{role}.md, fall back to a default."""
    path = Path(f"/app/agents/{role}.md")
    if path.exists():
        return path.read_text(encoding="utf-8")
    # Minimal fallback so nothing breaks if the file is missing
    return f"You are a {role} agent. Complete the task given to you accurately and concisely."


def _validate_task_dag(tasks: list) -> list[dict]:
    """
    Validate and normalize a task list.
    Ensures all dep references exist and there are no cycles.
    """
    if not isinstance(tasks, list):
        raise ValueError("tasks must be a list")

    seen_ids = set()
    normalized = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid  = str(t.get("id", f"t{len(normalized)}"))
        role = str(t.get("role", "coder"))
        desc = str(t.get("desc", ""))
        deps = [str(d) for d in t.get("deps", []) if str(d) in seen_ids]
        if role not in ("architect", "coder", "reviewer", "tester", "documenter"):
            role = "coder"
        normalized.append({"id": tid, "role": role, "desc": desc, "deps": deps, "status": "pending"})
        seen_ids.add(tid)

    return normalized


# ── Module-level singleton (initialised in main.py lifespan) ──────────────────
_agent_manager: Optional[AgentManager] = None

def get_agent_manager() -> AgentManager:
    if _agent_manager is None:
        raise RuntimeError("AgentManager not initialised")
    return _agent_manager

def init_agent_manager(mem: MemoryManager) -> AgentManager:
    global _agent_manager
    _agent_manager = AgentManager(mem)
    return _agent_manager