"""
widgets/footer_bar.py — Bottom keybinding hint bar. Textual 8.x compatible.

render() returns rich.text.Text — required by Textual 8.x Visual API.
"""

from rich.text import Text
from textual.widget import Widget

import tui.theme as theme


HINTS = {
    "launcher": [
        ("↑↓", "navigate"), ("Enter", "open"), ("n", "new project"),
        ("c", "chat"), ("q", "quit"),
    ],
    "project": [
        ("Enter", "open session"), ("n", "new session"), ("i", "index"),
        ("Esc", "back"),
    ],
    "new_project": [
        ("Tab", "next field"), ("Enter", "create"), ("Esc", "back"),
    ],
    "new_session": [
        ("Tab", "next field"), ("Enter", "start"), ("Esc", "back"),
    ],
    "session": [
        ("Tab", "pane"), ("f", "follow"), ("d", "DAG"), ("l", "logs"),
        ("m", "models"), ("/", "commands"), ("Ctrl+C", "cancel"),
        ("?", "help"), ("Ctrl+Q", "quit"),
    ],
    "chat": [
        ("r", "role"), ("m", "model"), ("/", "commands"),
        ("p", "projects"), ("Ctrl+Q", "quit"),
    ],
    "logs": [
        ("/", "search"), ("Esc", "back"),
    ],
    "help": [
        ("Esc", "close"),
    ],
    "model_config": [
        ("Tab", "next role"), ("Enter", "apply"), ("Esc", "cancel"),
    ],
    "command_palette": [
        ("↑↓", "navigate"), ("Enter", "run"), ("Esc", "dismiss"),
    ],
}


def _build_markup(hints: list[tuple[str, str]]) -> str:
    parts = []
    for key, desc in hints:
        # Escape the key text so Rich doesn't parse e.g. "[Enter]" as a style tag
        escaped_key = key.replace("[", "\\[")
        parts.append(
            f"[bold {theme.OVERLAY1}]{escaped_key}[/] [{theme.OVERLAY0}]{desc}[/]"
        )
    return "  ".join(parts)


class FooterBar(Widget):
    DEFAULT_CSS = """
    FooterBar {
        height: 1;
        background: #181825;
        color: #6c7086;
        dock: bottom;
        padding: 0 1;
    }
    """

    def __init__(self, screen_name: str = "launcher", **kwargs) -> None:
        super().__init__(**kwargs)
        self._screen = screen_name

    def render(self) -> Text:
        markup = _build_markup(HINTS.get(self._screen, []))
        return Text.from_markup(markup) if markup else Text("")

    def set_screen(self, screen_name: str) -> None:
        self._screen = screen_name
        self.refresh()