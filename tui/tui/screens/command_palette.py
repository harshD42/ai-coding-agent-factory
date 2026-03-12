"""
screens/command_palette.py — /command palette overlay.

Triggered by:
  - Typing / in the session InputBar with unknown/partial command
  - handle_inline_command() returning "__open_command_palette__"
  - The `?` key is handled by HelpScreen, not this screen.

Features:
  - Fuzzy prefix filter on command name as user types
  - Arrow keys navigate, Enter executes selected command
  - Command history (arrow-up in filter input cycles history)
  - All commands route back to session_service.handle_inline_command()
  - Esc dismisses without executing

Design: floating overlay centered on screen, ~60 cols wide.
"""

import asyncio
from collections import deque
from typing import Optional

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Input, ListView, ListItem, Static
from textual.containers import Container

import tui.theme as theme
from tui.widgets.footer_bar import FooterBar

# ── Command registry ──────────────────────────────────────────────────────────

COMMANDS: list[dict] = [
    {"name": "architect", "args": "<task>",    "desc": "Generate implementation plan"},
    {"name": "execute",   "args": "",          "desc": "Execute the task queue (DAG)"},
    {"name": "review",    "args": "<text>",    "desc": "Review code or a plan"},
    {"name": "test",      "args": "<task>",    "desc": "Write tests for a task"},
    {"name": "debate",    "args": "<topic>",   "desc": "Architect vs Reviewer debate (opt-in)"},
    {"name": "memory",    "args": "<query>",   "desc": "Search past session memories"},
    {"name": "spawn",     "args": "<role>",    "desc": "Spawn a specific agent role"},
    {"name": "kill",      "args": "<agent_id>","desc": "Send interrupt to running agent"},
    {"name": "model",     "args": "",          "desc": "Open model configuration overlay"},
    {"name": "index",     "args": "",          "desc": "Re-index the codebase (AST-aware)"},
    {"name": "status",    "args": "",          "desc": "Show system status inline"},
    {"name": "learn",     "args": "",          "desc": "Extract reusable skill from session"},
    {"name": "end",       "args": "",          "desc": "End this session with a summary"},
    {"name": "help",      "args": "",          "desc": "Open keybinding reference"},
]

_HISTORY_MAXLEN = 20


def _fuzzy_match(query: str, name: str) -> bool:
    """Return True if query is a prefix match (case-insensitive)."""
    return name.lower().startswith(query.lower().strip())


class CommandPaletteScreen(Screen):

    def __init__(
        self,
        partial: str = "",
        on_execute=None,       # async callable(cmd_string: str) → str
        command_history: Optional[deque] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._partial  = partial.lstrip("/").strip()
        self._execute  = on_execute   # injected by session screen
        self._history: deque = command_history or deque(maxlen=_HISTORY_MAXLEN)
        self._hist_idx = -1
        self._filtered = list(COMMANDS)
        self._selected = 0

    # ── Composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield FooterBar("command_palette", id="footer")
        yield Container(
            Static(
                f"[bold {theme.BLUE}]Command Palette[/]  "
                f"[{theme.OVERLAY0}][Esc] dismiss[/]",
                id="cp-title",
            ),
            Static(f"[{theme.SURFACE1}]{'─' * 58}[/]"),
            Input(
                value=self._partial,
                placeholder="type to filter…",
                id="cp-filter",
            ),
            Static(f"[{theme.SURFACE1}]{'─' * 58}[/]"),
            Static(self._render_list(), id="cp-list"),
            Static("", id="cp-status"),
            id="command-palette",
        )

    def on_mount(self) -> None:
        try:
            self.query_one("#cp-filter", Input).focus()
        except Exception:
            pass
        self._apply_filter(self._partial)

    # ── Filtering ─────────────────────────────────────────────────────────────

    def _apply_filter(self, query: str) -> None:
        if query.strip():
            self._filtered = [
                c for c in COMMANDS if _fuzzy_match(query, c["name"])
            ]
        else:
            self._filtered = list(COMMANDS)
        self._selected = 0
        self._refresh_list()

    def _render_list(self) -> str:
        if not self._filtered:
            return f"[{theme.OVERLAY0}]No matching commands[/]"

        lines = []
        for i, cmd in enumerate(self._filtered):
            name_col = theme.BLUE if i == self._selected else theme.TEXT
            args_col = theme.OVERLAY1
            desc_col = theme.OVERLAY0
            prefix   = f"[bold {theme.MAUVE}]>[/] " if i == self._selected else "  "
            name_s   = f"/{cmd['name']}"
            args_s   = f" {cmd['args']}" if cmd["args"] else ""
            lines.append(
                f"{prefix}[bold {name_col}]{name_s}[/]"
                f"[{args_col}]{args_s:<16}[/]  "
                f"[{desc_col}]{cmd['desc']}[/]"
            )
        return "\n".join(lines)

    def _refresh_list(self) -> None:
        try:
            self.query_one("#cp-list", Static).update(self._render_list())
        except Exception:
            pass

    # ── Input events ──────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "cp-filter":
            self._hist_idx = -1
            self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cp-filter":
            self._run_selected()

    def on_key(self, event) -> None:
        k = event.key

        if k == "escape":
            self.app.pop_screen()
            return

        if k == "up":
            try:
                inp = self.query_one("#cp-filter", Input)
            except Exception:
                return

            # If filter is empty, cycle command history
            if not inp.value.strip() and self._history:
                hist_list = list(self._history)
                if self._hist_idx == -1:
                    self._hist_idx = len(hist_list) - 1
                elif self._hist_idx > 0:
                    self._hist_idx -= 1
                inp.value = hist_list[self._hist_idx]
                inp.cursor_position = len(inp.value)
                self._apply_filter(inp.value)
            else:
                # Navigate list upward
                if self._selected > 0:
                    self._selected -= 1
                    self._refresh_list()
            event.prevent_default()

        elif k == "down":
            try:
                inp = self.query_one("#cp-filter", Input)
            except Exception:
                return

            if not inp.value.strip() and self._history and self._hist_idx != -1:
                hist_list = list(self._history)
                if self._hist_idx < len(hist_list) - 1:
                    self._hist_idx += 1
                    inp.value = hist_list[self._hist_idx]
                else:
                    self._hist_idx = -1
                    inp.value = ""
                inp.cursor_position = len(inp.value)
                self._apply_filter(inp.value)
            else:
                if self._selected < len(self._filtered) - 1:
                    self._selected += 1
                    self._refresh_list()
            event.prevent_default()

        elif k == "enter":
            self._run_selected()

    # ── Execution ─────────────────────────────────────────────────────────────

    def _run_selected(self) -> None:
        if not self._filtered:
            return
        cmd = self._filtered[self._selected]

        # Build command string from filter input + selected command
        try:
            filter_val = self.query_one("#cp-filter", Input).value.strip()
        except Exception:
            filter_val = ""

        # If filter already has args (e.g. "architect add JWT"), use it
        # Otherwise just use the command name
        if " " in filter_val:
            cmd_string = filter_val
        else:
            cmd_string = cmd["name"]

        # Save to history
        if not self._history or list(self._history)[-1] != cmd_string:
            self._history.append(cmd_string)

        asyncio.create_task(self._execute_and_close(cmd_string))

    async def _execute_and_close(self, cmd_string: str) -> None:
        self.app.pop_screen()
        if self._execute is not None:
            try:
                result = await self._execute(cmd_string)
                # Special signals handled by session screen
            except Exception as e:
                pass  # session screen handles logging