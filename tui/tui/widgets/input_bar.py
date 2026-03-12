"""
widgets/input_bar.py — Message input with @agent routing, /command detection,
and command history (arrow-up/down cycles previous inputs).
"""

from collections import deque

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static

import tui.theme as theme

_HISTORY_MAXLEN = 20


class InputBar(Widget):

    DEFAULT_CSS = """
    InputBar {
        height: 3;
        background: #181825;
        border-top: solid #313244;
        dock: bottom;
        padding: 0 1;
    }
    InputBar #input-hint {
        height: 1;
        color: #6c7086;
        width: auto;
    }
    InputBar Input {
        background: #181825;
        border: none;
        color: #cdd6f4;
        height: 1;
        width: 1fr;
    }
    InputBar Input:focus {
        border: none;
        background: #181825;
    }
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CommandTriggered(Message):
        def __init__(self, partial: str = "") -> None:
            super().__init__()
            self.partial = partial

    def __init__(self, placeholder: str = "> send a message…", **kwargs) -> None:
        super().__init__(**kwargs)
        self._placeholder = placeholder
        self._history: deque[str] = deque(maxlen=_HISTORY_MAXLEN)
        self._history_idx: int = -1

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static(
                f"[{theme.OVERLAY0}]>[/]  [{theme.OVERLAY0}]@agent  /commands[/]",
                id="input-hint",
            ),
            Input(
                placeholder=self._placeholder,
                id="main-input",
            ),
        )

    def on_mount(self) -> None:
        # Focus the inner Input as soon as the bar mounts
        self.call_after_refresh(self._focus)

    def _focus(self) -> None:
        try:
            self.query_one("#main-input", Input).focus()
        except Exception:
            pass

    def focus_input(self) -> None:
        self._focus()

    def clear(self) -> None:
        try:
            self.query_one("#main-input", Input).value = ""
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "main-input":
            return
        text = event.value.strip()
        if not text:
            return
        event.stop()
        # Save to history
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_idx = -1
        self.clear()

        if text.startswith("/"):
            self.post_message(self.CommandTriggered(partial=text[1:]))
        else:
            self.post_message(self.Submitted(text=text))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "main-input":
            return
        val = event.value
        try:
            hint = self.query_one("#input-hint", Static)
        except Exception:
            return
        if val.startswith("@"):
            hint.update(
                f"[{theme.BLUE}]@[/][{theme.TEXT}]agent[/]  "
                f"[{theme.OVERLAY0}]route to specific agent[/]"
            )
        elif val.startswith("/"):
            hint.update(
                f"[{theme.MAUVE}]/[/][{theme.TEXT}]command[/]  "
                f"[{theme.OVERLAY0}]architect · execute · status · index…[/]"
            )
        else:
            hint.update(
                f"[{theme.OVERLAY0}]>[/]  [{theme.OVERLAY0}]@agent  /commands[/]"
            )

    def on_key(self, event) -> None:
        if event.key not in ("up", "down"):
            return
        if not self._history:
            return
        try:
            inp = self.query_one("#main-input", Input)
        except Exception:
            return

        history_list = list(self._history)

        if event.key == "up":
            if self._history_idx == -1:
                self._history_idx = len(history_list) - 1
            elif self._history_idx > 0:
                self._history_idx -= 1
            inp.value = history_list[self._history_idx]
            inp.cursor_position = len(inp.value)
        elif event.key == "down":
            if self._history_idx == -1:
                return
            if self._history_idx < len(history_list) - 1:
                self._history_idx += 1
                inp.value = history_list[self._history_idx]
            else:
                self._history_idx = -1
                inp.value = ""
            inp.cursor_position = len(inp.value)

        event.prevent_default()
        event.stop()