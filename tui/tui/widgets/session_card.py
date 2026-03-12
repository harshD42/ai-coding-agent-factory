"""
widgets/session_card.py — Single row in the session list.
Focusable and keyboard navigable. Emits Selected on click or Enter.
"""

from rich.text import Text
from textual.message import Message
from textual.widget import Widget

import tui.theme as theme
from tui.utils.format import fmt_relative, truncate


class SessionCard(Widget):
    """One session row. Focusable. Emits Selected on click/Enter."""

    can_focus = True

    DEFAULT_CSS = """
    SessionCard {
        height: 1;
        background: #181825;
        padding: 0 1;
    }
    SessionCard:focus {
        background: #313244;
    }
    SessionCard:hover {
        background: #313244;
    }
    """

    class Selected(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, session_dict: dict, **kwargs) -> None:
        sid = session_dict.get("session_id", "")
        super().__init__(id=f"sess-{sid}", **kwargs)
        self._sess = session_dict

    def render(self) -> Text:
        s      = self._sess
        sid    = s.get("session_id", "")
        status = s.get("status", "")
        task   = truncate(s.get("task", "(no task)"), 44)
        ts     = s.get("updated_at") or s.get("created_at") or 0
        rel    = fmt_relative(ts)
        expired = s.get("expired", False)

        if expired:
            dot, col, st = "⊘", theme.SURFACE2, "expired"
        elif status == "active":
            dot, col, st = "●", theme.GREEN, "active"
        elif status == "paused":
            dot, col, st = "◐", theme.YELLOW, "paused"
        else:
            dot, col, st = "⊙", theme.OVERLAY0, "ended"

        markup = (
            f"[{col}]{dot}[/]  [{theme.TEXT}]{task:<44}[/]  "
            f"[{col}]{st:<8}[/]  [{theme.OVERLAY0}]{rel}[/]"
        )
        return Text.from_markup(markup)

    def on_click(self) -> None:
        self.post_message(self.Selected(self._sess.get("session_id", "")))

    def on_key(self, event) -> None:
        if event.key == "enter":
            self.post_message(self.Selected(self._sess.get("session_id", "")))
            event.stop()