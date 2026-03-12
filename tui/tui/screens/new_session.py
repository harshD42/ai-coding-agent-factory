"""
screens/new_session.py — Lightweight "start a new session" dialog.

Triggered by pressing n from the session or project screen.
Just asks for a task description; model config is inherited from project defaults.
"""

import asyncio

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Input, Static
from textual.containers import Container

import tui.theme as theme
from tui.services.project_service import ProjectService
from tui.state import AppState
from tui.store import ProjectStore


class NewSessionScreen(Screen):

    def __init__(
        self,
        store:      ProjectStore,
        state:      AppState,
        proj_svc:   ProjectService,
        project_id: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._store      = store
        self._state      = state
        self._proj_svc   = proj_svc
        self._project_id = project_id
        self._working    = False

    def compose(self) -> ComposeResult:
        project = self._store.get_project(self._project_id)
        name    = project.name if project else self._project_id
        yield Container(
            Static(
                f"[bold {theme.BLUE}]New Session[/]  "
                f"[{theme.OVERLAY0}]{name}[/]  "
                f"[{theme.OVERLAY0}][Esc] cancel[/]"
            ),
            Static(f"[{theme.SURFACE1}]{'─' * 50}[/]"),
            Static(f"\n[{theme.OVERLAY0}]Task[/]"),
            Input(
                placeholder="What should the agents work on?",
                id="ns-task-input",
            ),
            Static(""),
            Button("▶  Start Session", id="ns-start-btn", classes="primary"),
            Static("", id="ns-status"),
            id="new-session-container",
        )

    def on_mount(self) -> None:
        try:
            self.query_one("#ns-task-input", Input).focus()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ns-start-btn" and not self._working:
            asyncio.create_task(self._start())

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()
        elif event.key == "enter":
            if not self._working:
                asyncio.create_task(self._start())

    async def _start(self) -> None:
        self._working = True
        try:
            task = self.query_one("#ns-task-input", Input).value.strip()
            status_w = self.query_one("#ns-status", Static)
            if not task:
                status_w.update(f"[{theme.ERROR}]Task is required[/]")
                return

            status_w.update(f"[{theme.SKY}]◐  Creating session…[/]")
            session_id = await self._proj_svc.new_session(
                self._project_id, task
            )
            self._store.save_ui_state(last_session_id=session_id)
            # Replace this dialog and navigate to session
            self.app.pop_screen()
            self.app.push_screen(
                "session",
                project_id=self._project_id,
                session_id=session_id,
            )
        except Exception as e:
            try:
                self.query_one("#ns-status", Static).update(
                    f"[{theme.ERROR}]✕  {e}[/]"
                )
            except Exception:
                pass
        finally:
            self._working = False