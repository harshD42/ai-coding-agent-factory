"""
screens/help.py — Full keybinding reference overlay. Esc to close.
"""

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import RichLog, Static
from textual.containers import Container

import tui.theme as theme
from tui.widgets.footer_bar import FooterBar

_SECTIONS = [
    ("Global", [
        ("Ctrl+Q",  "Quit AICAF"),
        ("?",       "Open this help screen"),
    ]),
    ("Session", [
        ("Tab",     "Cycle focus between agent panes"),
        ("f",       "Follow: focus the first running agent"),
        ("d",       "Toggle DAG sidebar"),
        ("l",       "Open event log viewer"),
        ("m",       "Open model configuration overlay"),
        ("/",       "Open command palette"),
        ("Ctrl+C",  "Send interrupt to focused agent"),
        ("n",       "New session in same project"),
        ("p",       "Go to project screen"),
    ]),
    ("Chat", [
        ("r",       "Cycle agent role"),
        ("m",       "Cycle available models for current role"),
        ("p",       "Return to launcher"),
        ("/",       "Command palette"),
    ]),
    ("Launcher", [
        ("↑ / k",   "Move selection up"),
        ("↓ / j",   "Move selection down"),
        ("Enter",   "Open selected project"),
        ("n",       "Create new project"),
        ("c",       "Open quick chat"),
        ("q",       "Quit"),
    ]),
    ("Project", [
        ("Enter",   "Open selected session"),
        ("n",       "New session"),
        ("i",       "Re-index codebase"),
        ("Esc",     "Go back"),
    ]),
    ("Command Palette", [
        ("↑ / ↓",  "Navigate commands"),
        ("Enter",   "Execute selected command"),
        ("Esc",     "Dismiss"),
    ]),
    ("Input Bar", [
        ("@role",   "Route message to agent by role (e.g. @coder)"),
        ("@id",     "Route message to specific agent by ID"),
        ("↑ / ↓",  "Cycle command history"),
    ]),
]


class HelpScreen(Screen):

    def compose(self) -> ComposeResult:
        yield FooterBar("help", id="footer")
        yield Container(
            Static(
                f"[bold {theme.BLUE}]AICAF Keybindings[/]  "
                f"[{theme.OVERLAY0}][Esc] close[/]"
            ),
            Static(f"[{theme.SURFACE1}]{'─' * 70}[/]"),
            RichLog(id="help-log", highlight=False, markup=True, wrap=False),
            id="help-container",
        )

    def on_mount(self) -> None:
        self._render()

    def _render(self) -> None:
        try:
            log_w = self.query_one("#help-log", RichLog)
        except Exception:
            return

        for section, bindings in _SECTIONS:
            log_w.write(
                f"\n[bold {theme.OVERLAY1}]{section}[/]\n"
                f"[{theme.SURFACE1}]{'─' * 50}[/]"
            )
            for key, desc in bindings:
                log_w.write(
                    f"  [bold {theme.BLUE}]{key:<18}[/]  "
                    f"[{theme.TEXT}]{desc}[/]"
                )

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()