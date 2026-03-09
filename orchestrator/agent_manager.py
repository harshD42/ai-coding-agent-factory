"""
agent_manager.py — Spawn, track, kill agents + task decomposition + watchdog.

Phase 3.5 fixes:
  - Agent history trimmed to MAX_AGENT_HISTORY after every turn
  - Agent prompt path loaded from config.AGENTS_DIR
  - cleanup_idle_agents() prunes finished agents older than AGENT_IDLE_TIMEOUT

Phase 4A.2 additions:
  - AgentManager accepts optional redis reference for session model lookups
  - Agent.model field — populated before _run_agent() via RoutingPolicy
  - _run_agent() passes session_id to router.dispatch() for per-session routing
  - _run_agent() passes model name to context_manager.build_prompt() for
    per-model token budgets (Phase 4A.1 integration)

Phase 4B.1 additions:
  - Agent gains inbox (asyncio.Queue) and outbox (asyncio.Queue) for
    persistent session messaging. These are in-process queues; Redis pub/sub
    fan-out to the WebSocket layer is wired in Phase 4B.3 (AgentBus).
  - Agent state machine gains WAITING_FOR_INPUT status:
      IDLE → ASSIGNED → RUNNING → WAITING_FOR_INPUT → RUNNING → COMPLETE
  - AgentManager.send_message(agent_id, message) — push to agent inbox
  - AgentManager.subscribe_stream(agent_id) — async-iterate agent outbox tokens
  - AgentManager gains optional bus reference (set in 4B.3 lifespan wiring)

Phase 4B.2 additions:
  - _run_agent() switches to stream=True — tokens pushed to agent.outbox
    token-by-token as they arrive from the model, not as one block at the end
  - AgentManager.has_agent(agent_id) — used by SSE endpoint poll loop
  - Non-streaming fallback retained for LiteLLM path and backends that ignore
    stream=True (dict response detected and handled gracefully)
"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

import config
from context_manager import ContextManager
from memory_manager import MemoryManager
from metrics import metrics, parse_usage
from models import ChatCompletionRequest, Message
from routing_policy import get_routing_policy
import router

log = logging.getLogger("agent_manager")

# Inbox capacity per agent — if the architect is overwhelmed, messages queue
# here rather than blocking the caller. 256 is generous for single-dev use.
_INBOX_MAXSIZE  = 256
_OUTBOX_MAXSIZE = 1024   # token chunks are small; keep buffer large


# ── Agent state ───────────────────────────────────────────────────────────────

class Agent:
    def __init__(self, agent_id: str, role: str, session_id: str):
        self.agent_id   = agent_id
        self.role       = role
        self.session_id = session_id
        self.status     = "idle"
        self.model:     Optional[str]   = None    # Phase 4A.2 — resolved model name
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.ended_at:   Optional[float] = None
        self.task:       Optional[str]   = None
        self.result:     Optional[str]   = None
        self.error:      Optional[str]   = None
        self._history:   list[dict]      = []

        # Phase 4B.1: per-agent message queues (in-process)
        # inbox  — user/architect messages waiting to be processed
        # outbox — token chunks for SSE consumers (4B.2) and bus (4B.3)
        self.inbox:  asyncio.Queue = asyncio.Queue(maxsize=_INBOX_MAXSIZE)
        self.outbox: asyncio.Queue = asyncio.Queue(maxsize=_OUTBOX_MAXSIZE)

    def to_dict(self) -> dict:
        return {
            "agent_id":        self.agent_id,
            "role":            self.role,
            "session_id":      self.session_id,
            "status":          self.status,
            "model":           self.model,
            "task":            self.task,
            "created_at":      self.created_at,
            "started_at":      self.started_at,
            "ended_at":        self.ended_at,
            "error":           self.error,
            "inbox_depth":     self.inbox.qsize(),   # 4B.1: visible in /list
            "outbox_depth":    self.outbox.qsize(),
        }


# ── AgentManager ──────────────────────────────────────────────────────────────

class AgentManager:
    def __init__(self, mem: MemoryManager, redis=None, bus=None):
        self._mem:    MemoryManager  = mem
        self._ctx:    ContextManager = ContextManager(mem)
        self._agents: dict[str, Agent] = {}
        self._redis   = redis   # Phase 4A.2 — for RoutingPolicy session lookups
        self._bus     = bus     # Phase 4B.3 — AgentBus (None until 4B.3 wiring)

    def set_bus(self, bus) -> None:
        """Wire in the AgentBus singleton. Called from main.py lifespan in 4B.3."""
        self._bus = bus

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _new_agent(self, role: str, session_id: str) -> Agent:
        agent_id = f"{role}-{uuid.uuid4().hex[:8]}"
        agent    = Agent(agent_id, role, session_id)
        self._agents[agent_id] = agent
        log.info("spawned agent  id=%s  role=%s  session=%s", agent_id, role, session_id)
        return agent

    async def spawn_and_run(
        self,
        role:          str,
        task:          str,
        session_id:    str = "default",
        extra_context: str = "",
    ) -> dict:
        """
        Spawn an agent, run it, return result dict.
        Enforces MAX_AGENT_RUNTIME watchdog timeout.

        Phase 4B.1: registers agent with SessionManager if available.
        """
        agent            = self._new_agent(role, session_id)
        agent.task       = task
        agent.status     = "running"
        agent.started_at = time.time()

        # Phase 4B.1: register agent with session so session:state tracks agent_ids
        try:
            from session_manager import get_session_manager
            await get_session_manager().register_agent(session_id, agent.agent_id)
        except RuntimeError:
            pass   # SessionManager not yet initialised (tests, early startup)
        except Exception as e:
            log.debug("register_agent failed (non-fatal): %s", e)

        try:
            result = await asyncio.wait_for(
                self._run_agent(agent, task, extra_context),
                timeout=config.MAX_AGENT_RUNTIME,
            )
            agent.status   = "done"
            agent.result   = result
            agent.ended_at = time.time()

            # Phase 4B.1: signal outbox consumers that this agent is done
            await _drain_outbox_sentinel(agent)

            log.info("agent done  id=%s  model=%s  elapsed=%.1fs",
                     agent.agent_id, agent.model, agent.ended_at - agent.started_at)
            return {
                "agent_id": agent.agent_id,
                "role":     role,
                "model":    agent.model,
                "result":   result,
                "status":   "done",
            }

        except asyncio.TimeoutError:
            agent.status   = "killed"
            agent.error    = f"Timed out after {config.MAX_AGENT_RUNTIME}s"
            agent.ended_at = time.time()
            await _drain_outbox_sentinel(agent)
            log.error("agent timed out  id=%s", agent.agent_id)
            metrics.record_request(
                agent_id=agent.agent_id, role=role,
                tokens_in=0, tokens_out=0,
                latency_ms=(agent.ended_at - agent.started_at) * 1000,
                session_id=session_id, status="killed",
            )
            await self._mem.record_failure(
                session_id, agent.agent_id, task,
                error=agent.error, approach=f"role={role}",
            )
            if self._bus is not None:
                from models import WSEvent, WSEventType
                await self._bus.publish(session_id, WSEvent(
                    type=WSEventType.WORK_FAILED,
                    session_id=session_id,
                    agent_id=agent.agent_id,
                    payload={"role": role, "error": agent.error, "reason": "timeout"},
                ))
            return {
                "agent_id": agent.agent_id, "role": role, "model": agent.model,
                "result": None, "status": "killed", "error": agent.error,
            }

        except Exception as e:
            agent.status   = "failed"
            agent.error    = str(e)
            agent.ended_at = time.time()
            await _drain_outbox_sentinel(agent)
            log.exception("agent failed  id=%s: %s", agent.agent_id, e)
            metrics.record_request(
                agent_id=agent.agent_id, role=role,
                tokens_in=0, tokens_out=0,
                latency_ms=(agent.ended_at - agent.started_at) * 1000,
                session_id=session_id, status="failed",
            )
            await self._mem.record_failure(
                session_id, agent.agent_id, task,
                error=str(e), approach=f"role={role}",
            )
            if self._bus is not None:
                from models import WSEvent, WSEventType
                await self._bus.publish(session_id, WSEvent(
                    type=WSEventType.WORK_FAILED,
                    session_id=session_id,
                    agent_id=agent.agent_id,
                    payload={"role": role, "error": str(e), "reason": "exception"},
                ))
            return {
                "agent_id": agent.agent_id, "role": role, "model": agent.model,
                "result": None, "status": "failed", "error": str(e),
            }

    async def _run_agent(self, agent: Agent, task: str, extra_context: str) -> str:
        """
        Resolve model → build context → call model → record metrics → return text.

        Phase 4B.1: token chunks are published to agent.outbox so that
        subscribe_stream() consumers (SSE endpoint, 4B.2) receive them in
        near-real-time. For non-streaming model calls the full response is
        published as a single chunk after completion. Phase 4B.2 wires true
        streaming so chunks arrive token-by-token.

        Phase 4A.2:
          - RoutingPolicy resolves (endpoint, model, btype) for this session
          - agent.model is populated before the model call
          - model name passed to build_prompt() for per-model token budget
          - session_id passed to router.dispatch() for per-session routing

        Phase 3.5:
          - History trimmed to MAX_AGENT_HISTORY after every turn
        """
        # Phase 4A.2: resolve model name for this session before building context
        try:
            _, model_name, _ = await get_routing_policy().resolve(
                agent.role, agent.session_id
            )
            agent.model = model_name
        except Exception as e:
            log.warning("routing_policy not available, model unknown: %s", e)
            agent.model = ""

        system_prompt = _load_agent_prompt(agent.role)
        if extra_context:
            system_prompt += f"\n\n## Additional Context\n{extra_context}"

        # Phase 4A.1 + 4A.2: pass model name so context_manager uses correct token budget
        messages = await self._ctx.build_prompt(
            task=task,
            system_prompt=system_prompt,
            conversation=agent._history,
            session_id=agent.session_id,
            model=agent.model,
        )

        req = ChatCompletionRequest(
            model="orchestrator",
            messages=[Message(**m) for m in messages],
            stream=False,
        )

        t0 = time.time()

        # Phase 4B.2: stream=True — tokens flow into agent.outbox as they arrive.
        # The SSE endpoint consumes agent.outbox in real time via subscribe_stream().
        # We reassemble the full response here for callers (task_queue, test_fix_loop)
        # that expect a string return value. This is the only place stream=True is set;
        # all other callers go through spawn_and_run() → _run_agent() and get the
        # assembled string back transparently.
        req_streaming = ChatCompletionRequest(
            model=req.model,
            messages=req.messages,
            stream=True,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            top_p=req.top_p,
        )

        raw = await router.dispatch(
            req_streaming,
            role=agent.role,
            messages=messages,
            session_id=agent.session_id,
        )

        latency_ms = (time.time() - t0) * 1000
        content    = ""
        tokens_in  = 0
        tokens_out = 0

        if isinstance(raw, dict):
            # Fallback: router returned a non-streaming response (e.g. LiteLLM path,
            # or backend that ignored stream=True). Treat as single chunk.
            choices = raw.get("choices", [])
            if choices:
                msg     = choices[0].get("message", {})
                content = msg.get("content", "") if isinstance(msg, dict) else ""
            tokens_in, tokens_out = parse_usage(raw)
            if content:
                try:
                    agent.outbox.put_nowait(content)
                except asyncio.QueueFull:
                    log.warning("agent %s outbox full (non-streaming fallback)", agent.agent_id)
        else:
            # Streaming path: iterate SSE chunks, parse token content, push to outbox.
            # raw is an AsyncIterator[str] of "data: {...}\n\n" lines.
            import json as _json
            async for line in raw:
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = _json.loads(data_str)
                except _json.JSONDecodeError:
                    continue

                # Extract token from both Ollama and vLLM chunk shapes
                choices = chunk.get("choices", [])
                token   = ""
                if choices:
                    delta = choices[0].get("delta", {})
                    token = delta.get("content", "") or ""
                    # Ollama non-delta shape (fallback)
                    if not token:
                        msg   = choices[0].get("message", {})
                        token = msg.get("content", "") if isinstance(msg, dict) else ""

                if token:
                    content += token
                    try:
                        agent.outbox.put_nowait(token)
                    except asyncio.QueueFull:
                        log.warning(
                            "agent %s outbox full — SSE consumer lagging, "
                            "token dropped", agent.agent_id,
                        )

                # Accumulate usage if backend provides it mid-stream
                usage = chunk.get("usage")
                if usage:
                    tokens_in  = usage.get("prompt_tokens",     tokens_in)
                    tokens_out = usage.get("completion_tokens", tokens_out)

        latency_ms = (time.time() - t0) * 1000   # recalculate after stream exhausted

        metrics.record_request(
            agent_id=agent.agent_id,
            role=agent.role,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            session_id=agent.session_id,
            status="done",
        )

        # Phase 4B.3: publish WORK_COMPLETE to AgentBus so architect loop
        # can react without polling. Bus is None until 4B.3 lifespan wiring.
        if self._bus is not None:
            from models import WSEvent, WSEventType
            await self._bus.publish(agent.session_id, WSEvent(
                type=WSEventType.WORK_COMPLETE,
                session_id=agent.session_id,
                agent_id=agent.agent_id,
                payload={
                    "role":           agent.role,
                    "model":          agent.model,
                    "result_preview": content[:200],
                    "tokens_in":      tokens_in,
                    "tokens_out":     tokens_out,
                    "latency_ms":     round(latency_ms, 1),
                },
            ))

        # Phase 3.5: trim history after appending — preserves role alternation
        agent._history.append({"role": "user",      "content": task})
        agent._history.append({"role": "assistant",  "content": content})
        if len(agent._history) > config.MAX_AGENT_HISTORY:
            excess = len(agent._history) - config.MAX_AGENT_HISTORY
            agent._history = agent._history[excess:]

        return content

    # ── Phase 4B.1: Messaging ─────────────────────────────────────────────────

    async def send_message(self, agent_id: str, message: str) -> bool:
        """
        Push a message to an agent's inbox.

        If the agent is RUNNING, the message is queued and processed when
        the current task completes. If WAITING_FOR_INPUT, the agent loop
        will pick it up on the next iteration (wired in 4B.3 architect loop).
        If the agent does not exist or has terminated, returns False.

        Returns True if the message was delivered to the queue.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            log.warning("send_message: agent %r not found", agent_id)
            return False
        if agent.status in ("done", "failed", "killed"):
            log.warning(
                "send_message: agent %r is terminal (status=%s), message dropped",
                agent_id, agent.status,
            )
            return False
        try:
            agent.inbox.put_nowait(message)
            log.debug("message queued for agent %s  inbox_depth=%d",
                      agent_id, agent.inbox.qsize())
            return True
        except asyncio.QueueFull:
            log.error(
                "send_message: agent %s inbox full (%d items) — message dropped",
                agent_id, _INBOX_MAXSIZE,
            )
            return False

    async def subscribe_stream(self, agent_id: str) -> AsyncIterator[str]:
        """
        Async-iterate token chunks from an agent's outbox.

        Yields string chunks until the sentinel value (None) is received,
        which is published by spawn_and_run() when the agent reaches a
        terminal state.

        Used by GET /v1/agents/{agent_id}/stream (SSE endpoint, 4B.2).
        Also used by the TUI agent pane to display streaming output.

        Note: only one consumer per agent is supported in 4B.1. Multi-consumer
        fan-out (broadcast to multiple TUI panes) is handled by AgentBus in 4B.3.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            log.warning("subscribe_stream: agent %r not found", agent_id)
            return

        while True:
            try:
                chunk = await asyncio.wait_for(agent.outbox.get(), timeout=30.0)
                if chunk is None:
                    # Sentinel — agent has finished
                    break
                yield chunk
            except asyncio.TimeoutError:
                # No token for 30s — check if agent has ended so we don't hang forever
                if agent.status in ("done", "failed", "killed"):
                    break
                # Still running but silent (thinking) — keep waiting
                continue

    # ── Phase 3.5: Agent registry cleanup ────────────────────────────────────

    async def cleanup_idle_agents(self) -> int:
        """Remove finished agents older than AGENT_IDLE_TIMEOUT from registry."""
        cutoff    = time.time() - config.AGENT_IDLE_TIMEOUT
        terminal  = ("done", "failed", "killed")
        to_remove = [
            aid for aid, a in self._agents.items()
            if a.status in terminal and (a.ended_at or 0) < cutoff
        ]
        for aid in to_remove:
            del self._agents[aid]
        if to_remove:
            log.info("cleanup_idle_agents: removed %d stale agents", len(to_remove))
        return len(to_remove)

    # ── Task decomposition ────────────────────────────────────────────────────

    async def decompose_plan_to_tasks(
        self, plan: str, session_id: str = "default"
    ) -> list[dict]:
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
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            tasks     = json.loads(clean.strip())
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

    def get_agents_for_session(self, session_id: str) -> list[Agent]:
        """Return all agents that ran under a given session_id."""
        return [a for a in self._agents.values() if a.session_id == session_id]

    def has_agent(self, agent_id: str) -> bool:
        """Return True if the agent is registered (may still be initialising)."""
        return agent_id in self._agents


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _drain_outbox_sentinel(agent: Agent) -> None:
    """
    Push a None sentinel to the agent outbox to signal end-of-stream
    to subscribe_stream() consumers. Non-blocking — if the outbox is full
    (no consumer connected) we log and move on.
    """
    try:
        agent.outbox.put_nowait(None)
    except asyncio.QueueFull:
        log.debug("agent %s outbox full on sentinel push — no active SSE consumer",
                  agent.agent_id)


def _load_agent_prompt(role: str) -> str:
    path = Path(config.AGENTS_DIR) / f"{role}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    log.warning("agent prompt not found: %s — using fallback", path)
    return f"You are a {role} agent. Complete the task given to you accurately and concisely."


def _validate_task_dag(tasks: list) -> list[dict]:
    if not isinstance(tasks, list):
        raise ValueError("tasks must be a list")
    seen_ids   = set()
    normalized = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid  = str(t.get("id",   f"t{len(normalized)}"))
        role = str(t.get("role", "coder"))
        desc = str(t.get("desc", ""))
        deps = [str(d) for d in t.get("deps", []) if str(d) in seen_ids]
        if role not in ("architect", "coder", "reviewer", "tester", "documenter"):
            role = "coder"
        normalized.append({"id": tid, "role": role, "desc": desc,
                           "deps": deps, "status": "pending"})
        seen_ids.add(tid)
    return normalized


# ── Singleton ─────────────────────────────────────────────────────────────────

_agent_manager: Optional[AgentManager] = None


def get_agent_manager() -> AgentManager:
    if _agent_manager is None:
        raise RuntimeError("AgentManager not initialised")
    return _agent_manager


def init_agent_manager(mem: MemoryManager, redis=None, bus=None) -> AgentManager:
    global _agent_manager
    _agent_manager = AgentManager(mem, redis=redis, bus=bus)
    return _agent_manager