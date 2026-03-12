"""
widgets/dag_sidebar.py — Collapsible right sidebar.

Textual 8.x: render() must return a Rich renderable, not a plain string.
We return rich.text.Text built from markup so Textual's Visual.to_strips()
receives an object that implements render_strips().

Sections: TASKS · AGENTS · PATCHES · MODELS
Toggled by `d` key (adds/removes .hidden CSS class via session screen).
"""

from rich.text import Text

from textual.widget import Widget

import tui.theme as theme
from tui.state import AppState
from tui.utils.format import fmt_model, truncate, progress_bar


class DagSidebar(Widget):
    """Right sidebar rendered via Rich Text markup."""

    DEFAULT_CSS = """
    DagSidebar {
        width: 34;
        background: #181825;
        border-left: solid #313244;
        overflow-y: auto;
        padding: 0 1;
        height: 1fr;
    }
    DagSidebar.hidden {
        display: none;
    }
    """

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state = state

    def render(self) -> Text:
        """Return a Rich Text object — required by Textual 8.x render path."""
        return Text.from_markup(self._build_markup())

    def refresh_content(self) -> None:
        """Force a re-render. Called by session screen on WSEvents."""
        self.refresh()

    def _build_markup(self) -> str:
        lines: list[str] = []
        sess = self._state.session

        if sess is None:
            return f"[{theme.OVERLAY0}]No active session[/]"

        # ── TASKS ─────────────────────────────────────────────────────────────
        tasks = sess.tasks_list()
        if tasks:
            lines.append(f"[bold {theme.OVERLAY0}]TASKS[/]")
            lines.append(f"[{theme.SURFACE1}]{'─' * 30}[/]")
            for t in tasks:
                sym, col = theme.task_indicator(t.status)
                role_col = theme.role_color(t.role)
                tid      = truncate(t.id, 4)
                desc     = truncate(t.desc, 18)
                st       = truncate(t.status, 8)
                lines.append(
                    f"[{col}]{sym}[/]  [{theme.OVERLAY0}]{tid}[/]  "
                    f"[{role_col}]{desc}[/]  [{col}]{st}[/]"
                )
            lines.append("")

        # ── AGENTS ────────────────────────────────────────────────────────────
        agents = sess.active_agents()
        if agents:
            lines.append(f"[bold {theme.OVERLAY0}]AGENTS[/]")
            lines.append(f"[{theme.SURFACE1}]{'─' * 30}[/]")
            for a in agents:
                sym, col = theme.indicator(a.status)
                role_col = theme.role_color(a.role)
                role_s   = truncate(a.role, 10)
                ratio = (
                    1.0 if a.status == "done"
                    else 0.5 if a.status == "running"
                    else 0.0
                )
                bar = progress_bar(ratio, 8)
                lines.append(
                    f"[{col}]{sym}[/] [{role_col}]{role_s:<10}[/]  "
                    f"[{theme.SURFACE2}]{bar}[/]  [{col}]{a.status}[/]"
                )
            lines.append("")

        # ── PATCHES ───────────────────────────────────────────────────────────
        patches = sess.patches_list()[:6]
        if patches:
            lines.append(f"[bold {theme.OVERLAY0}]PATCHES[/]")
            lines.append(f"[{theme.SURFACE1}]{'─' * 30}[/]")
            for p in patches:
                sym, col = theme.patch_indicator(p.status)
                pid   = truncate(p.patch_id, 10)
                files = truncate(", ".join(p.files[:2]), 14) if p.files else ""
                lines.append(
                    f"[{col}]{sym}[/] [{theme.OVERLAY1}]{pid}[/]  "
                    f"[{theme.OVERLAY0}]{files}[/]  [{col}]{p.status}[/]"
                )
            lines.append("")

        # ── MODELS ────────────────────────────────────────────────────────────
        if sess.models:
            lines.append(
                f"[bold {theme.OVERLAY0}]MODELS[/]  "
                f"[{theme.BLUE}]\\[m] reconfigure[/]"
            )
            lines.append(f"[{theme.SURFACE1}]{'─' * 30}[/]")
            for role, model in sess.models.items():
                rc = theme.role_color(role)
                lines.append(
                    f"[{rc}]{role:<10}[/]  "
                    f"[{theme.OVERLAY1}]{fmt_model(model)}[/]"
                )

        return "\n".join(lines) if lines else f"[{theme.OVERLAY0}]No data yet[/]"