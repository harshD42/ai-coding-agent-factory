"""
widgets/project_card.py — Single row in the project list.
Focusable and keyboard navigable. Emits Selected on click or Enter.
"""

from rich.text import Text
from textual.message import Message
from textual.widget import Widget

import tui.theme as theme
from tui.store import Project
from tui.utils.format import fmt_relative, fmt_path, truncate


class ProjectCard(Widget):
    """One project row. Focusable. Emits Selected on click/Enter."""

    can_focus = True

    DEFAULT_CSS = """
    ProjectCard {
        height: 1;
        background: #181825;
        padding: 0 1;
    }
    ProjectCard:focus {
        background: #313244;
    }
    ProjectCard:hover {
        background: #313244;
    }
    """

    class Selected(Message):
        def __init__(self, project_id: str) -> None:
            super().__init__()
            self.project_id = project_id

    def __init__(self, project: Project, active: bool = False, **kwargs) -> None:
        super().__init__(
            id=f"proj-{project.id}",
            **kwargs,
        )
        self._project = project
        self._active  = active

    def render(self) -> Text:
        p    = self._project
        name = truncate(p.name, 24)
        path = fmt_path(p.workspace, 28)
        rel  = fmt_relative(p.last_active)
        nsess = len(p.session_ids)

        dot = f"[{theme.GREEN}]▶[/]  " if self._active else "   "
        col = theme.BLUE if self._active else theme.TEXT

        markup = (
            f"{dot}[bold {col}]{name:<24}[/]  "
            f"[{theme.OVERLAY0}]{path:<30}[/]  "
            f"[{theme.OVERLAY0}]{nsess} session{'s' if nsess != 1 else ''}[/]  "
            f"[{theme.OVERLAY0}]{rel}[/]"
        )
        return Text.from_markup(markup)

    def on_click(self) -> None:
        self.post_message(self.Selected(self._project.id))

    def on_key(self, event) -> None:
        if event.key == "enter":
            self.post_message(self.Selected(self._project.id))
            event.stop()