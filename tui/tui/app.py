"""
app.py — AicafApp: the Textual application root.

Owns:
  - Single AicafClient + ConnectionSupervisor
  - Single AppState (shared reference passed to all screens)
  - Single ProjectStore
  - Screen registry and push/pop navigation
  - Global key bindings (Ctrl+Q)
  - Connection status updates from supervisor → header bar
"""

import asyncio
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding

from tui.client import AicafClient, ConnectionSupervisor
from tui.services.project_service import ProjectService
from tui.services.session_service import SessionService
from tui.state import AppState
from tui.store import ProjectStore

from tui.screens.launcher import LauncherScreen
from tui.screens.new_project import NewProjectScreen
from tui.screens.new_session import NewSessionScreen
from tui.screens.project import ProjectScreen
from tui.screens.session import SessionScreen
from tui.screens.chat import ChatScreen
from tui.screens.logs import LogsScreen
from tui.screens.model_config import ModelConfigScreen
from tui.screens.command_palette import CommandPaletteScreen
from tui.screens.help import HelpScreen

from importlib.resources import files

CSS_PATH = str(files("tui").joinpath("aicaf.tcss"))


class AicafApp(App):
    """Main application. One instance per `aicaf` invocation."""

    CSS_PATH = str(CSS_PATH)

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        orchestrator_url: str,
        initial_project:  str | None = None,
        initial_session:  str | None = None,
        chat_role:        str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._orch_url        = orchestrator_url.rstrip("/")
        self._initial_project = initial_project
        self._initial_session = initial_session
        self._chat_role       = chat_role

        self._state   = AppState()
        self._store   = ProjectStore()
        self._client  = AicafClient(self._orch_url)
        self._supervisor: Optional[ConnectionSupervisor] = None

        self._proj_svc: Optional[ProjectService] = None
        self._sess_svc: Optional[SessionService] = None

    # ── App lifecycle ─────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        await self._client.connect()

        self._proj_svc = ProjectService(self._client, self._store, self._state)
        self._sess_svc = SessionService(self._client, self._state)

        self._supervisor = ConnectionSupervisor(
            client=self._client,
            on_status_change=self._on_connection_change,
        )
        await self._supervisor.start()

        if self._chat_role:
            await self.push_screen(self._make_chat(self._chat_role))
        elif self._initial_project and self._initial_session:
            await self.push_screen(
                self._make_session(self._initial_project, self._initial_session)
            )
        elif self._initial_project:
            await self.push_screen(self._make_project(self._initial_project))
        else:
            await self.push_screen(self._make_launcher())

    async def on_unmount(self) -> None:
        if self._supervisor:
            await self._supervisor.stop()
        await self._client.close()

    # ── Connection status callback ─────────────────────────────────────────────

    def _on_connection_change(self, status: str) -> None:
        self._state.set_connection(status)
        try:
            from tui.widgets.header_bar import HeaderBar
            self.query_one(HeaderBar).refresh_content()
        except Exception:
            pass

    # ── Screen factories ──────────────────────────────────────────────────────

    def _make_launcher(self) -> LauncherScreen:
        return LauncherScreen(
            client=self._client,
            store=self._store,
            state=self._state,
        )

    def _make_project(self, project_id: str) -> ProjectScreen:
        return ProjectScreen(
            client=self._client,
            store=self._store,
            state=self._state,
            proj_svc=self._proj_svc,
            project_id=project_id,
        )

    def _make_new_project(self) -> NewProjectScreen:
        return NewProjectScreen(
            client=self._client,
            store=self._store,
            state=self._state,
            proj_svc=self._proj_svc,
        )

    def _make_new_session(self, project_id: str) -> NewSessionScreen:
        return NewSessionScreen(
            store=self._store,
            state=self._state,
            proj_svc=self._proj_svc,
            project_id=project_id,
        )

    def _make_session(self, project_id: str, session_id: str) -> SessionScreen:
        return SessionScreen(
            client=self._client,
            store=self._store,
            state=self._state,
            sess_svc=self._sess_svc,
            project_id=project_id,
            session_id=session_id,
        )

    def _make_chat(self, role: str = "coder") -> ChatScreen:
        return ChatScreen(
            client=self._client,
            store=self._store,
            state=self._state,
            role=role,
        )

    def _make_logs(self) -> LogsScreen:
        return LogsScreen(state=self._state)

    def _make_model_config(self, session_id: str) -> ModelConfigScreen:
        return ModelConfigScreen(
            client=self._client,
            state=self._state,
            session_id=session_id,
        )

    def _make_command_palette(
        self,
        partial: str = "",
        on_execute=None,
        command_history=None,
    ) -> CommandPaletteScreen:
        return CommandPaletteScreen(
            partial=partial,
            on_execute=on_execute,
            command_history=command_history,
        )

    def _make_help(self) -> HelpScreen:
        return HelpScreen()

    # ── Screen router ─────────────────────────────────────────────────────────

    def push_screen(self, screen_name_or_screen, **kwargs):
        """
        Accept either a Screen instance or a string name with kwargs.
        All registered screen names are handled here.
        """
        if isinstance(screen_name_or_screen, str):
            name = screen_name_or_screen

            if name == "launcher":
                return super().push_screen(self._make_launcher())
            elif name == "project":
                return super().push_screen(
                    self._make_project(kwargs["project_id"])
                )
            elif name == "new_project":
                return super().push_screen(self._make_new_project())
            elif name == "new_session":
                return super().push_screen(
                    self._make_new_session(kwargs["project_id"])
                )
            elif name == "session":
                return super().push_screen(
                    self._make_session(
                        kwargs["project_id"], kwargs["session_id"]
                    )
                )
            elif name == "chat":
                return super().push_screen(
                    self._make_chat(kwargs.get("role", "coder"))
                )
            elif name == "logs":
                return super().push_screen(self._make_logs())
            elif name == "model_config":
                return super().push_screen(
                    self._make_model_config(kwargs["session_id"])
                )
            elif name == "command_palette":
                return super().push_screen(
                    self._make_command_palette(
                        partial=kwargs.get("partial", ""),
                        on_execute=kwargs.get("on_execute"),
                        command_history=kwargs.get("command_history"),
                    )
                )
            elif name == "help":
                return super().push_screen(self._make_help())
            else:
                raise ValueError(f"Unknown screen: {name!r}")

        return super().push_screen(screen_name_or_screen)

    # ── Global action ─────────────────────────────────────────────────────────

    def action_quit(self) -> None:
        self.exit()