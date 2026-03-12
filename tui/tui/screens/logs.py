"""
screens/logs.py — Full-screen scrollable event log viewer.
"""

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import RichLog, Static

import tui.theme as theme
from tui.state import AppState
from tui.utils.format import fmt_datetime


class LogsScreen(Screen):

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state = state

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold {theme.BLUE}]Event Log[/]  "
            f"[{theme.OVERLAY0}][Esc] back  [/] search[/]",
            id="logs-header",
        )
        yield Static(f"[{theme.SURFACE1}]{'─' * 80}[/]")
        yield RichLog(id="logs-content", highlight=True, markup=True, wrap=True)

    def on_mount(self) -> None:
        self._render()

    def _render(self) -> None:
        try:
            log_w = self.query_one("#logs-content", RichLog)
            if not self._state.session or not self._state.session.event_log:
                log_w.write(f"[{theme.OVERLAY0}]No events yet in this session.[/]")
                return
            for ev in self._state.session.event_log:
                ts      = fmt_datetime(ev.get("ts", 0))
                source  = ev.get("source", "?")
                etype   = ev.get("event_type") or ev.get("type", "?")
                content = ev.get("content", "")

                src_col = theme.role_color(source) if source in [
                    "architect", "coder", "reviewer", "tester", "documenter"
                ] else theme.OVERLAY0

                log_w.write(
                    f"[{theme.OVERLAY0}]{ts}[/]  [{src_col}]{source:<12}[/]  "
                    f"[{theme.BLUE}]{etype:<16}[/]  [{theme.TEXT}]{content[:120]}[/]"
                )
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()