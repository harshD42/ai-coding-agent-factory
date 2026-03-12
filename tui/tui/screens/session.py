"""
screens/session.py — Main session screen with inline conversation log.

Shows a conversation-style log (user messages + agent responses) in the
main area, plus the DAG sidebar on the right. Agent pane grid is available
via `v` key toggle. This is a forward-compatible bridge to v0.6.0's full
ConversationLog widget.
"""

import asyncio
from collections import deque
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Container
from textual.screen import Screen
from textual.widgets import Input, RichLog, Static

import tui.theme as theme
from tui.client import AicafClient, AicafConnectionError
from tui.layout.session_layout import SessionLayoutManager
from tui.services.session_service import SessionService
from tui.state import AppState, AgentInfo
from tui.store import ProjectStore
from tui.widgets.dag_sidebar import DagSidebar
from tui.widgets.footer_bar import FooterBar
from tui.widgets.header_bar import HeaderBar

_CMD_HISTORY_MAXLEN = 20

# AgentPane import kept for grid view toggle (v key, future use)
try:
    from tui.widgets.agent_pane import AgentPane
    _AGENT_PANE_AVAILABLE = True
except Exception:
    _AGENT_PANE_AVAILABLE = False


class SessionScreen(Screen):

    DEFAULT_CSS = """
    SessionScreen {
        layers: base;
    }
    #sess-header {
        dock: top;
        height: 1;
    }
    #sess-footer {
        dock: bottom;
        height: 1;
    }
    #sess-input {
        dock: bottom;
        height: 3;
        background: #181825;
        border-top: solid #313244;
        border-bottom: none;
        border-left: none;
        border-right: none;
        color: #cdd6f4;
        padding: 0 2;
    }
    #sess-input:focus {
        border-top: solid #89b4fa;
        border-bottom: none;
        border-left: none;
        border-right: none;
    }
    #cmd-output {
        dock: bottom;
        height: auto;
        max-height: 6;
        background: #181825;
        border-top: solid #313244;
        padding: 0 2;
        color: #a6adc8;
        display: none;
    }
    #cmd-output.visible {
        display: block;
    }
    #session-body {
        layout: horizontal;
        height: 1fr;
    }
    #conv-log {
        background: #1e1e2e;
        padding: 0 2;
        height: 1fr;
    }
    #dag-sidebar {
        width: 34;
    }
    """

    def __init__(
        self,
        client:     AicafClient,
        store:      ProjectStore,
        state:      AppState,
        sess_svc:   SessionService,
        project_id: str,
        session_id: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._client     = client
        self._store      = store
        self._state      = state
        self._sess_svc   = sess_svc
        self._project_id = project_id
        self._session_id = session_id
        self._layout_mgr = SessionLayoutManager()
        self._cmd_history: deque = deque(maxlen=_CMD_HISTORY_MAXLEN)

        self._ws_task:      Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._ready        = False
        self._at_bottom    = True
        # Track which agents we've already streamed so we don't double-display
        self._streamed_agents: set[str] = set()

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield HeaderBar(self._state, id="sess-header")
        yield FooterBar("session", id="sess-footer")
        yield Static("", id="cmd-output")
        yield Input(
            placeholder="  Type a message or /command…",
            id="sess-input",
        )
        yield Horizontal(
            RichLog(
                id="conv-log",
                highlight=True,
                markup=True,
                wrap=True,
            ),
            DagSidebar(self._state, id="dag-sidebar"),
            id="session-body",
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.call_after_refresh(self._post_mount)

    def _post_mount(self) -> None:
        self._ready = True
        self._write_system(
            f"Session [{theme.BLUE}]{self._session_id[:16]}…[/]  "
            f"[{theme.OVERLAY0}]d=DAG  l=logs  m=models  /=commands  ?=help[/]"
        )
        try:
            self.query_one("#sess-input", Input).focus()
        except Exception:
            pass
        asyncio.create_task(self._init())

    def on_unmount(self) -> None:
        for t in [self._ws_task, self._refresh_task,
                  *self._stream_tasks.values()]:
            if t is not None and not t.done():
                t.cancel()

    async def _init(self) -> None:
        await self._sess_svc.load_session(self._session_id)
        self._refresh_header()
        if not self._state.ui.dag_sidebar_open:
            self._set_dag_visible(False)
        self._ws_task      = asyncio.create_task(self._ws_loop())
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        self._store.save_ui_state(
            last_session_id=self._session_id,
            last_screen="session",
        )

    # ── Conversation log helpers ──────────────────────────────────────────────

    def _log(self) -> Optional[RichLog]:
        try:
            return self.query_one("#conv-log", RichLog)
        except Exception:
            return None

    def _write_system(self, markup: str) -> None:
        log = self._log()
        if log:
            log.write(f"[{theme.OVERLAY0}]{markup}[/]")

    def _write_user(self, text: str) -> None:
        log = self._log()
        if not log:
            return
        log.write("")
        log.write(
            f"[bold {theme.BLUE}]You[/]  "
            f"[{theme.SURFACE1}]{'─' * 46}[/]"
        )
        log.write(f"  [{theme.TEXT}]{text}[/]")
        log.write("")
        if self._at_bottom:
            log.scroll_end(animate=False)

    def _write_agent_header(self, role: str, model: str = "") -> None:
        log = self._log()
        if not log:
            return
        rc    = theme.role_color(role)
        model_s = f"  [{theme.OVERLAY0}]{model}[/]" if model else ""
        log.write(
            f"[bold {rc}]{role.upper()}[/]{model_s}  "
            f"[{theme.SURFACE1}]{'─' * 46}[/]"
        )

    def _write_agent_token(self, token: str) -> None:
        log = self._log()
        if log:
            log.write(token, expand=True, shrink=False)
            if self._at_bottom:
                log.scroll_end(animate=False)

    def _write_agent_done(self, role: str, tokens: int, elapsed_ms: float) -> None:
        log = self._log()
        if log:
            log.write(
                f"\n  [{theme.OVERLAY0}]✓ {role} done  "
                f"{tokens} tokens  {elapsed_ms/1000:.1f}s[/]\n"
            )
            if self._at_bottom:
                log.scroll_end(animate=False)

    def _write_patch_applied(self, files: list[str], patch_id: str) -> None:
        log = self._log()
        if log:
            files_s = ", ".join(files[:3])
            log.write(
                f"  [{theme.GREEN}]✓ patch applied[/]  "
                f"[{theme.OVERLAY1}]{files_s}[/]  "
                f"[{theme.OVERLAY0}]{patch_id[:10]}[/]"
            )

    def _write_test_result(self, summary: str, passed: bool) -> None:
        log = self._log()
        if log:
            col = theme.GREEN if passed else theme.RED
            sym = "✓" if passed else "✕"
            log.write(f"  [{col}]{sym} tests: {summary}[/]")

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        while True:
            try:
                async for event in self._client.stream_session_events(
                    self._session_id
                ):
                    await self._handle_ws_event(event)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(3)

    async def _handle_ws_event(self, event: dict) -> None:
        etype    = event.get("type", "")
        payload  = event.get("payload", {})
        agent_id = event.get("agent_id", "")

        self._sess_svc.handle_ws_event(event)

        if etype == "work_complete":
            if agent_id and agent_id not in self._streamed_agents:
                # Agent completed but we missed the stream — show result
                agent = (self._state.session.agents.get(agent_id)
                         if self._state.session else None)
                if agent:
                    self._write_agent_header(agent.role, agent.model or "")
                    preview = payload.get("result_preview", "")
                    if preview:
                        self._write_agent_token(f"  {preview}")
                    self._write_agent_done(
                        agent.role,
                        payload.get("tokens_out", 0),
                        payload.get("latency_ms", 0),
                    )
            self._refresh_dag()
            self._refresh_header()

        elif etype == "work_failed":
            role = payload.get("role", "agent")
            err  = payload.get("error", "unknown error")
            log  = self._log()
            if log:
                log.write(
                    f"\n  [{theme.RED}]✕ {role} failed: {err}[/]\n"
                )
            self._refresh_dag()

        elif etype == "patch_applied":
            self._write_patch_applied(
                payload.get("files", []),
                payload.get("patch_id", ""),
            )
            self._refresh_dag()

        elif etype == "test_result":
            self._write_test_result(
                payload.get("summary", ""),
                payload.get("passed", False),
            )

        elif etype == "status":
            if payload.get("lifecycle") == "ended":
                self._write_system("Session ended.")
                self._refresh_header()

    # ── Refresh loop ──────────────────────────────────────────────────────────

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(2)
                await self._sess_svc.refresh_tasks(self._session_id)
                await self._sess_svc.refresh_agents(self._session_id)
                self._refresh_dag()
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    # ── SSE stream per agent ──────────────────────────────────────────────────

    def _start_stream(self, agent_id: str, role: str, model: str = "") -> None:
        if agent_id in self._stream_tasks and not self._stream_tasks[agent_id].done():
            return
        if agent_id in self._streamed_agents:
            return
        self._streamed_agents.add(agent_id)
        self._write_agent_header(role, model)
        self._stream_tasks[agent_id] = asyncio.create_task(
            self._stream_agent(agent_id, role)
        )

    async def _stream_agent(self, agent_id: str, role: str) -> None:
        token_count = 0
        import time
        t0 = time.time()
        try:
            async for token in self._client.stream_agent_tokens(agent_id):
                self._state.add_tokens(1)
                token_count += 1
                self._write_agent_token(token)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        elapsed = (time.time() - t0) * 1000
        self._write_agent_done(role, token_count, elapsed)
        self._refresh_header()

    # ── Widget refresh ────────────────────────────────────────────────────────

    def _refresh_header(self) -> None:
        try:
            self.query_one("#sess-header", HeaderBar).refresh_content()
        except Exception:
            pass

    def _refresh_dag(self) -> None:
        try:
            self.query_one("#dag-sidebar", DagSidebar).refresh_content()
        except Exception:
            pass

    def _set_dag_visible(self, visible: bool) -> None:
        try:
            dag = self.query_one("#dag-sidebar", DagSidebar)
            dag.remove_class("hidden") if visible else dag.add_class("hidden")
        except Exception:
            pass

    def _show_cmd_output(self, text: str) -> None:
        try:
            out = self.query_one("#cmd-output", Static)
            out.update(f"[{theme.OVERLAY0}]›[/] {text}")
            out.add_class("visible")
            asyncio.create_task(self._hide_cmd_output(8))
        except Exception:
            pass

    async def _hide_cmd_output(self, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            self.query_one("#cmd-output", Static).remove_class("visible")
        except Exception:
            pass

    # ── Scroll anchoring ──────────────────────────────────────────────────────

    def on_scroll(self, event) -> None:
        try:
            log = self._log()
            if log:
                self._at_bottom = (
                    log.scroll_y >= log.virtual_size.height - log.size.height - 2
                )
        except Exception:
            pass

    # ── Keyboard ─────────────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        k              = event.key
        typing         = isinstance(self.focused, Input)

        if k == "d" and not typing:
            open_ = self._state.toggle_dag()
            self._set_dag_visible(open_)
            self._store.save_ui_state(dag_sidebar_open=open_)

        elif k == "l" and not typing:
            self.app.push_screen("logs")

        elif k == "m" and not typing:
            asyncio.create_task(self._open_model_config())

        elif k == "question_mark" and not typing:
            self.app.push_screen("help")

        elif k == "n" and not typing:
            self.app.push_screen("new_session", project_id=self._project_id)

        elif k == "p" and not typing:
            self.app.push_screen("project", project_id=self._project_id)

        elif k == "f" and not typing:
            pass  # focus follow — no agent panes in this view, no-op

        elif k == "ctrl+c":
            asyncio.create_task(self._cancel_active_agents())

        elif k == "ctrl+q":
            self.app.exit()

        elif k == "escape" and not typing:
            self.app.push_screen("project", project_id=self._project_id)

    async def _open_model_config(self) -> None:
        self.app.push_screen("model_config", session_id=self._session_id)

    async def _cancel_active_agents(self) -> None:
        if not self._state.session:
            return
        for agent in self._state.session.running_agents():
            try:
                await self._client.send_agent_message(
                    agent.agent_id, "__interrupt__", sender="system"
                )
            except Exception:
                pass

    # ── Input ─────────────────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "sess-input":
            return
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        asyncio.create_task(self._handle_input(text))

    async def _handle_input(self, text: str) -> None:
        if text.startswith("/"):
            cmd_str = text[1:].strip()
            self._write_user(f"/{cmd_str}")
            self._show_cmd_output(f"[{theme.SKY}]◐[/] running /{cmd_str.split()[0]}…")

            result = await self._sess_svc.handle_inline_command(cmd_str)

            if result == "__open_model_config__":
                await self._open_model_config()
            elif result == "__open_command_palette__":
                self.app.push_screen(
                    "command_palette",
                    partial=cmd_str,
                    on_execute=self._run_cmd_and_show,
                    command_history=self._cmd_history,
                )
            elif result == "__open_help__":
                self.app.push_screen("help")
            else:
                if result:
                    self._show_cmd_output(result)
                    # Also write to conv log for commands that produce content
                    if len(result) > 60:
                        self._write_system(result)
                    self._state.log_event("system", f"/{cmd_str.split()[0]}", result)
                    self._refresh_dag()
                    self._refresh_header()

            if cmd_str and (
                not self._cmd_history or list(self._cmd_history)[-1] != cmd_str
            ):
                self._cmd_history.append(cmd_str)

        else:
            # Plain message → show in log + send to agent
            self._write_user(text)
            await self._sess_svc.send_message(text)

            # If /architect was previously called and spawned an agent,
            # check for any new agents to stream
            await asyncio.sleep(0.5)
            if self._state.session:
                for agent in self._state.session.active_agents():
                    if agent.agent_id not in self._streamed_agents:
                        self._start_stream(
                            agent.agent_id, agent.role, agent.model or ""
                        )

    async def _run_cmd_and_show(self, cmd_str: str) -> str:
        result = await self._sess_svc.handle_inline_command(cmd_str)
        if result and not result.startswith("__"):
            self._show_cmd_output(result)
            if len(result) > 60:
                self._write_system(result)
        return result