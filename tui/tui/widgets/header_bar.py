"""
widgets/header_bar.py — Top status bar. Textual 8.x compatible.

render() returns rich.text.Text — required by Textual 8.x Visual API.
refresh_content() calls self.refresh() to trigger a re-render.
"""

from rich.text import Text
from textual.widget import Widget

import tui.theme as theme
from tui.state import AppState
from tui.utils.format import fmt_tokens, fmt_elapsed, fmt_model, truncate


class HeaderBar(Widget):
    DEFAULT_CSS = """
    HeaderBar {
        height: 1;
        background: #181825;
        color: #a6adc8;
        dock: top;
        padding: 0 1;
    }
    """

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state = state

    def render(self) -> Text:
        return Text.from_markup(self._build())

    def refresh_content(self) -> None:
        self.refresh()

    def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh)

    def _build(self) -> str:
        s = self._state
        parts = []

        parts.append(f"[bold {theme.BLUE}]AICAF[/]")

        if s.project:
            proj_name = truncate(s.project.name, 20)
            parts.append(f"  [{theme.SUBTEXT0}]{proj_name}[/]")
            if s.session:
                task = truncate(s.session.task, 30)
                parts.append(f"  [{theme.OVERLAY0}]›[/]  [{theme.TEXT}]{task}[/]")

        if s.session and s.session.models:
            coder_model = s.session.models.get("coder", "")
            if coder_model:
                parts.append(
                    f"  [{theme.OVERLAY0}]·[/]  "
                    f"[{theme.OVERLAY1}]{fmt_model(coder_model)}[/]"
                )

        right = []
        if s.session:
            right.append(
                f"[{theme.OVERLAY1}]tokens:[/] "
                f"[{theme.TEXT}]{fmt_tokens(s.session.token_count)}[/]"
            )
            if s.session.patch_count:
                right.append(
                    f"[{theme.OVERLAY1}]patches:[/] "
                    f"[{theme.SUCCESS}]{s.session.patch_count}[/]"
                )
            right.append(
                f"[{theme.OVERLAY0}]{fmt_elapsed(s.session.elapsed_start)}[/]"
            )

        conn_sym, conn_col = theme.CONNECTION_INDICATORS.get(
            s.ui.connection_status, ("?", theme.OVERLAY0)
        )
        right.append(f"[{conn_col}]{conn_sym}[/]")

        left_str  = "  ".join(parts)
        right_str = "  ".join(right)
        return f"{left_str}   [{theme.OVERLAY0}]|[/]   {right_str}"