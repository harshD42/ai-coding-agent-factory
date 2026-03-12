"""
screens/project.py — Project detail view.

Shows session history, workspace/git info, codebase index status, stats.
"""

import asyncio
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Input, Static
from textual.containers import Container, Vertical

import tui.theme as theme
from tui.client import AicafClient
from tui.services.project_service import ProjectService
from tui.state import AppState
from tui.store import ProjectStore
from tui.utils.format import fmt_relative, fmt_path, fmt_tokens
from tui.utils.git import detect_repo, short_status
from tui.widgets.footer_bar import FooterBar
from tui.widgets.session_card import SessionCard


class ProjectScreen(Screen):

    def __init__(
        self,
        client:     AicafClient,
        store:      ProjectStore,
        state:      AppState,
        proj_svc:   ProjectService,
        project_id: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._client     = client
        self._store      = store
        self._state      = state
        self._proj_svc   = proj_svc
        self._project_id = project_id
        self._sessions:  list[dict] = []

    def compose(self) -> ComposeResult:
        yield FooterBar("project", id="footer")
        yield Container(
            Static("", id="proj-header"),
            Static(f"[{theme.SURFACE1}]{'─' * 60}[/]"),

            Static(
                f"\n[bold {theme.OVERLAY1}]Sessions[/]"
                f"  [{theme.BLUE}][n] New Session[/]",
                id="sessions-header",
            ),
            Vertical(id="sessions-list"),

            Static(f"\n[bold {theme.OVERLAY1}]Workspace[/]", id="ws-header"),
            Static("", id="ws-info"),

            Static(f"\n[bold {theme.OVERLAY1}]Session Totals[/]", id="stats-header"),
            Static("", id="stats-info"),

            Static("", id="action-status"),
            id="project-container",
        )

    def on_mount(self) -> None:
        asyncio.create_task(self._load())

    async def _load(self) -> None:
        project = await self._proj_svc.load_project(self._project_id)
        if project is None:
            self.query_one("#proj-header", Static).update(
                f"[{theme.RED}]Project not found[/]"
            )
            return

        # Header
        self.query_one("#proj-header", Static).update(
            f"[bold {theme.BLUE}]{project.name}[/]  "
            f"[{theme.OVERLAY0}]{fmt_path(project.workspace)}[/]"
            f"  [{theme.BLUE}][config][/]  [{theme.OVERLAY0}][← back][/]"
        )

        # Git / workspace info
        git = detect_repo(project.workspace)
        ws_parts = [
            f"[{theme.OVERLAY0}]Path:   [/][{theme.TEXT}]{project.workspace}[/]",
            f"[{theme.OVERLAY0}]Branch: [/][{theme.BLUE}]{short_status(git)}[/]",
        ]
        self.query_one("#ws-info", Static).update("\n".join(ws_parts))

        # Sessions list
        self._sessions = await self._proj_svc.get_project_sessions(self._project_id)
        self._render_sessions()

        # Stats (token totals across sessions — approximate from session states)
        total_sessions = len(self._sessions)
        active_count   = sum(1 for s in self._sessions if s.get("status") == "active")
        self.query_one("#stats-info", Static).update(
            f"[{theme.OVERLAY0}]Sessions: [/][{theme.TEXT}]{total_sessions}[/]  "
            f"[{theme.OVERLAY0}]Active: [/][{theme.GREEN}]{active_count}[/]"
        )

    def _render_sessions(self) -> None:
        try:
            sl = self.query_one("#sessions-list")
            sl.remove_children()
            if not self._sessions:
                sl.mount(Static(
                    f"  [{theme.OVERLAY0}]No sessions yet — press [bold]n[/] to start one[/]"
                ))
                return
            for sess in self._sessions[:10]:
                sl.mount(SessionCard(sess))
        except Exception:
            pass

    def on_session_card_selected(self, event: SessionCard.Selected) -> None:
        if event.session_id:
            self.app.push_screen(
                "session",
                project_id=self._project_id,
                session_id=event.session_id,
            )

    def on_key(self, event) -> None:
        k = event.key
        if k == "escape":
            self.app.pop_screen()
        elif k == "n":
            asyncio.create_task(self._new_session_prompt())
        elif k == "i":
            asyncio.create_task(self._index())

    async def _new_session_prompt(self) -> None:
        # Simple prompt using app notification — full modal would use a Screen
        self.app.push_screen(
            "new_session",
            project_id=self._project_id,
        )

    async def _index(self) -> None:
        status_w = self.query_one("#action-status", Static)
        status_w.update(f"[{theme.SKY}]◐  Indexing codebase…[/]")
        try:
            result = await self._client.index_codebase()
            indexed   = result.get("files_indexed", 0)
            unchanged = result.get("files_unchanged", 0)
            chunks    = result.get("chunks", 0)
            status_w.update(
                f"[{theme.SUCCESS}]✓  Indexed {indexed} files  "
                f"({unchanged} unchanged)  {chunks} chunks[/]"
            )
        except Exception as e:
            status_w.update(f"[{theme.ERROR}]✕  Index failed: {e}[/]")