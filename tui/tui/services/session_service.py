"""
services/session_service.py — Session business logic.

Handles message routing, state population from orchestrator responses,
WSEvent processing, and inline /command execution.

Screens call service methods; widgets never call services directly.
"""

import asyncio
import logging
import re
from typing import Optional

from tui.client import AicafClient, AicafConnectionError
from tui.state import AppState, AgentInfo

log = logging.getLogger("session_service")

# Pattern: @role or @agent_id at start of message
_AT_PATTERN = re.compile(r"^@(\S+)\s*(.*)", re.DOTALL)

# Commands that execute directly without opening the command palette
_INLINE_COMMANDS = {
    "architect", "execute", "review", "test",
    "debate",    "memory",  "status", "index",
    "learn",     "spawn",   "kill",
}


class SessionService:

    def __init__(self, client: AicafClient, state: AppState) -> None:
        self._client = client
        self._state  = state

    # ── Load session state ────────────────────────────────────────────────────

    async def load_session(self, session_id: str) -> None:
        """
        Populate AppState.session from orchestrator.
        Called when opening a SessionScreen.
        """
        try:
            sess = await self._client.get_session(session_id)
        except AicafConnectionError as e:
            log.error("load_session failed: %s", e)
            return

        self._state.init_session(
            session_id=session_id,
            task=sess.get("task", ""),
            models=sess.get("models", {}),
        )
        if self._state.session:
            self._state.session.status = sess.get("status", "active")

        # Load agents already running under this session
        try:
            agents = await self._client.list_agents()
            for a in agents:
                if a.get("session_id") == session_id:
                    self._state.upsert_agent(a)
        except AicafConnectionError:
            pass

        await self.refresh_tasks(session_id)
        await self.refresh_patches(session_id)

    # ── Periodic refresh ──────────────────────────────────────────────────────

    async def refresh_tasks(self, session_id: str) -> None:
        try:
            data  = await self._client.get_task_status(session_id)
            for t in data.get("tasks", []):
                self._state.upsert_task(t)
        except AicafConnectionError as e:
            log.debug("refresh_tasks: %s", e)

    async def refresh_patches(self, session_id: str) -> None:
        try:
            patches = await self._client.list_patches(session_id)
            for p in patches:
                self._state.upsert_patch(p)
        except AicafConnectionError as e:
            log.debug("refresh_patches: %s", e)

    async def refresh_agents(self, session_id: str) -> None:
        try:
            agents = await self._client.list_agents()
            for a in agents:
                if a.get("session_id") == session_id:
                    self._state.upsert_agent(a)
        except AicafConnectionError as e:
            log.debug("refresh_agents: %s", e)

    # ── Message routing ───────────────────────────────────────────────────────

    async def send_message(self, raw_message: str) -> None:
        """
        Route a message from the InputBar.

        @role text      → find first agent with that role, send to inbox
        @agent_id text  → send directly to that agent's inbox
        bare text       → send to architect (if active) or first running agent
        """
        if self._state.session is None:
            return

        session_id = self._state.session.session_id
        m = _AT_PATTERN.match(raw_message.strip())

        if m:
            target_hint = m.group(1)
            message     = m.group(2).strip()
            if not message:
                return
            agent_id = self._resolve_agent(target_hint)
            if agent_id:
                try:
                    await self._client.send_agent_message(agent_id, message)
                    self._state.log_event("user", "message",
                                          f"@{target_hint}: {message}")
                except AicafConnectionError as e:
                    log.warning("send_message to %s failed: %s", target_hint, e)
            else:
                log.warning("no active agent found for target %r", target_hint)
        else:
            message  = raw_message.strip()
            agent_id = self._resolve_agent("architect")
            if not agent_id:
                running  = self._state.session.running_agents()
                agent_id = running[0].agent_id if running else None

            if agent_id:
                try:
                    await self._client.send_agent_message(agent_id, message)
                    self._state.log_event("user", "message", message)
                except AicafConnectionError as e:
                    log.warning("send_message failed: %s", e)

    def _resolve_agent(self, hint: str) -> Optional[str]:
        """Resolve a role name or agent_id hint to a concrete agent_id."""
        if self._state.session is None:
            return None
        agents = list(self._state.session.agents.values())
        # Exact agent_id match
        if hint in self._state.session.agents:
            return hint
        # Role match — prefer running over idle
        by_role = [a for a in agents if a.role == hint]
        if not by_role:
            return None
        running = [a for a in by_role if a.status == "running"]
        return (running or by_role)[0].agent_id

    # ── Inline /command execution ─────────────────────────────────────────────

    async def handle_inline_command(self, cmd_str: str) -> str:
        """
        Execute a /command typed in the session input bar.

        Returns a result string that the session screen logs as a
        system event in the conversation / event log. Never raises —
        errors are returned as strings.

        Commands that are unknown or need args not provided are returned
        as an informational string (caller opens command palette instead).
        """
        if self._state.session is None:
            return "No active session."

        session_id = self._state.session.session_id
        parts  = cmd_str.strip().split(None, 1)
        name   = parts[0].lower() if parts else ""
        args   = parts[1].strip() if len(parts) > 1 else ""

        try:
            if name == "architect":
                if not args:
                    return "Usage: /architect <task description>"
                result = await self._client._post(
                    "/v1/chat/completions",
                    {"model": "orchestrator",
                     "messages": [{"role": "user",
                                   "content": f"/architect {args}"}],
                     "stream": False},
                )
                text = (result.get("choices", [{}])[0]
                               .get("message", {})
                               .get("content", ""))
                return text or "(no response)"

            elif name == "execute":
                result = await self._client.execute_tasks(session_id)
                executed = result.get("executed", 0)
                complete = result.get("complete", 0)
                failed   = result.get("failed", 0)
                return (
                    f"Execution complete — "
                    f"{executed} tasks run, {complete} complete, {failed} failed"
                )

            elif name == "status":
                result = await self._client._post(
                    "/v1/chat/completions",
                    {"model": "orchestrator",
                     "messages": [{"role": "user", "content": "/status"}],
                     "stream": False},
                )
                text = (result.get("choices", [{}])[0]
                               .get("message", {})
                               .get("content", ""))
                return text or "(no status)"

            elif name == "index":
                result = await self._client.index_codebase()
                indexed   = result.get("files_indexed", 0)
                unchanged = result.get("files_unchanged", 0)
                chunks    = result.get("chunks", 0)
                return (
                    f"Indexed {indexed} files "
                    f"({unchanged} unchanged), {chunks} chunks"
                )

            elif name == "memory":
                if not args:
                    return "Usage: /memory <query>"
                results = await self._client.recall(args)
                if not results:
                    return f"No memories found for: {args}"
                lines = [f"Memory search: {args}"]
                for i, r in enumerate(results[:5], 1):
                    col  = r.get("collection", "?")
                    text = r.get("content", "")[:120]
                    lines.append(f"  {i}. [{col}] {text}")
                return "\n".join(lines)

            elif name == "review":
                if not args:
                    return "Usage: /review <text or task>"
                result = await self._client._post(
                    "/v1/chat/completions",
                    {"model": "orchestrator",
                     "messages": [{"role": "user",
                                   "content": f"/review {args}"}],
                     "stream": False},
                )
                text = (result.get("choices", [{}])[0]
                               .get("message", {})
                               .get("content", ""))
                return text or "(no response)"

            elif name == "test":
                if not args:
                    return "Usage: /test <task>"
                result = await self._client._post(
                    "/v1/chat/completions",
                    {"model": "orchestrator",
                     "messages": [{"role": "user",
                                   "content": f"/test {args}"}],
                     "stream": False},
                )
                text = (result.get("choices", [{}])[0]
                               .get("message", {})
                               .get("content", ""))
                return text or "(no response)"

            elif name == "debate":
                if not args:
                    return "Usage: /debate <topic>"
                result = await self._client._post(
                    "/v1/agents/debate",
                    {"topic": args, "session_id": session_id},
                )
                consensus = "✓ consensus" if result.get("consensus") else "⚠ no consensus"
                rounds    = result.get("rounds", 0)
                plan      = result.get("final_plan", "")[:300]
                return f"Debate ({rounds} rounds, {consensus})\n{plan}"

            elif name == "learn":
                result = await self._client._post(
                    "/v1/skills/learn",
                    {"session_id": session_id,
                     "transcript": self._state.session.event_log[-20:]},
                )
                skill = result.get("skill_name")
                if skill:
                    return f"Skill extracted: {skill}"
                return "No reusable skill identified in recent events."

            elif name == "spawn":
                role = args or "coder"
                if role not in ("architect", "coder", "reviewer",
                                "tester", "documenter"):
                    return f"Unknown role: {role}"
                result = await self._client.spawn_agent(
                    role=role, task="(standby)", session_id=session_id
                )
                agent_id = result.get("agent_id", "?")
                return f"Spawned {role} agent: {agent_id}"

            elif name == "kill":
                if not args:
                    return "Usage: /kill <agent_id>"
                await self._client.send_agent_message(
                    args, "__interrupt__", sender="system"
                )
                return f"Interrupt sent to agent {args}"

            elif name == "model":
                # Signal caller to open model config overlay
                return "__open_model_config__"

            elif name == "end":
                await self.end_session(summary="Session ended by user.")
                return "Session ended."

            elif name == "help":
                return "__open_help__"

            else:
                # Unknown — signal caller to open command palette
                return "__open_command_palette__"

        except AicafConnectionError as e:
            return f"Connection error: {e}"
        except Exception as e:
            log.warning("handle_inline_command error: %s", e)
            return f"Error: {e}"

    # ── WSEvent processing ────────────────────────────────────────────────────

    def handle_ws_event(self, event: dict) -> list[str]:
        """
        Process a WSEvent dict received from the WebSocket stream.
        Updates AppState and returns widget IDs that should refresh.
        """
        if self._state.session is None:
            return []

        etype    = event.get("type", "")
        payload  = event.get("payload", {})
        agent_id = event.get("agent_id")
        refresh  = []

        if etype == "work_complete":
            if agent_id and agent_id in self._state.session.agents:
                self._state.session.agents[agent_id].status = "done"
                refresh.append("dag-sidebar")
                refresh.append(f"agent-pane-{agent_id}")
            self._state.log_event(
                agent_id or "system", "work_complete",
                payload.get("result_preview", ""),
            )
            t_in  = payload.get("tokens_in", 0)
            t_out = payload.get("tokens_out", 0)
            if t_in or t_out:
                self._state.add_tokens(t_in + t_out)
                refresh.append("header-bar")

        elif etype == "work_failed":
            if agent_id and agent_id in self._state.session.agents:
                self._state.session.agents[agent_id].status = "failed"
                refresh.extend(["dag-sidebar", f"agent-pane-{agent_id}"])
            self._state.log_event(
                agent_id or "system", "work_failed",
                payload.get("error", ""),
            )

        elif etype == "patch_applied":
            patch_dict = {
                "patch_id":    payload.get("patch_id", ""),
                "agent_id":    payload.get("agent_id", agent_id or ""),
                "description": payload.get("description", ""),
                "status":      "applied",
                "files":       payload.get("files", []),
            }
            self._state.upsert_patch(patch_dict)
            refresh.append("dag-sidebar")
            self._state.log_event(
                "system", "patch_applied",
                f"patch {payload.get('patch_id')} → "
                + ", ".join(payload.get("files", [])),
            )

        elif etype == "test_result":
            self._state.log_event(
                "system", "test_result",
                str(payload.get("summary", "")),
            )
            refresh.append("dag-sidebar")

        elif etype == "status":
            lifecycle = payload.get("lifecycle", "")
            if lifecycle == "ended" and self._state.session:
                self._state.session.status = "ended"
                refresh.append("header-bar")

        elif etype == "heartbeat":
            pass  # keepalive only

        return refresh

    # ── End session ───────────────────────────────────────────────────────────

    async def end_session(self, summary: str = "") -> None:
        if self._state.session is None:
            return
        try:
            await self._client.end_session(
                self._state.session.session_id, summary
            )
        except AicafConnectionError as e:
            log.warning("end_session failed: %s", e)
        self._state.clear_session()