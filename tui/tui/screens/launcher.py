"""
screens/launcher.py — Project launcher (entry screen).

Navigation:
  - Arrow keys / j/k scroll the project list
  - Tab cycles focus: project list → role selector → Open Chat button
  - Enter opens selected project or activates focused button
  - n = new project, c = quick chat, q = quit
"""

import asyncio
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Select, Static
from textual.containers import Container, Vertical, ScrollableContainer

import tui.theme as theme
from tui.client import AicafClient, AicafConnectionError
from tui.state import AppState
from tui.store import ProjectStore
from tui.widgets.footer_bar import FooterBar
from tui.widgets.project_card import ProjectCard
from tui.utils.format import truncate

LOGO = """╔═╗╦╔═╗╔═╗╔═╗
╠═╣║║  ╠═╣╠╣ 
╩ ╩╩╚═╝╩ ╩╚  """

ALL_ROLES = ["architect", "coder", "reviewer", "tester", "documenter", "general"]


class LauncherScreen(Screen):

    DEFAULT_CSS = """
    LauncherScreen {
        align: center middle;
    }
    #launcher-inner {
        width: 80;
        height: auto;
        padding: 1 2;
    }
    #projects-list {
        height: auto;
        max-height: 10;
        border: solid #313244;
        background: #181825;
        padding: 0 1;
        margin-bottom: 1;
    }
    #chat-panel {
        border: solid #313244;
        background: #181825;
        padding: 1;
        height: auto;
        margin-top: 1;
        layout: vertical;
    }
    #open-chat-btn {
        margin-top: 1;
        width: 20;
    }
    """

    def __init__(
        self,
        client: AicafClient,
        store:  ProjectStore,
        state:  AppState,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._client       = client
        self._store        = store
        self._state        = state
        self._projects     = []
        self._selected_idx = 0

    def compose(self) -> ComposeResult:
        yield FooterBar("launcher", id="footer")
        yield Container(
            Static(f"[bold {theme.BLUE}]{LOGO}[/]", id="launcher-logo"),
            Static(
                f"[{theme.OVERLAY0}]AI Coding Agent Factory  v0.5.1[/]",
                id="launcher-subtitle",
            ),
            Static("", id="conn-status"),
            Static(
                f"\n[bold {theme.OVERLAY1}]Recent Projects[/]  "
                f"[{theme.OVERLAY0}]\\[n] new  \\[Enter] open[/]",
            ),
            Vertical(id="projects-list"),
            Static("", id="orphan-header"),
            Vertical(id="orphan-list"),
            Static(
                f"\n[bold {theme.OVERLAY1}]Quick Chat[/]  "
                f"[{theme.OVERLAY0}]Talk to any agent without a project[/]",
            ),
            Container(
                Static(
                    f"[{theme.OVERLAY0}]Select a role and press Open Chat "
                    f"(or press [bold]c[/] from anywhere)[/]",
                    id="chat-hint",
                ),
                Select(
                    options=[(r, r) for r in ALL_ROLES],
                    value="coder",
                    id="chat-role-select",
                ),
                Static("", id="chat-model-label"),
                Button("Open Chat →", id="open-chat-btn", variant="primary"),
                id="chat-panel",
            ),
            id="launcher-inner",
        )

    def on_mount(self) -> None:
        self._load_projects()
        self.call_after_refresh(self._post_mount)

    def _post_mount(self) -> None:
        asyncio.create_task(self._async_init())

    def _load_projects(self) -> None:
        self._projects = self._store.list_projects()
        self._render_projects()

    def _render_projects(self) -> None:
        try:
            pl = self.query_one("#projects-list")
            pl.remove_children()
            if not self._projects:
                pl.mount(Static(
                    f"  [{theme.OVERLAY0}]No projects yet — "
                    f"press [bold {theme.BLUE}]n[/] to create one[/]"
                ))
                return
            for i, p in enumerate(self._projects):
                pl.mount(ProjectCard(p, active=(i == self._selected_idx)))
        except Exception:
            pass

    async def _async_init(self) -> None:
        try:
            data = await self._client.health()
            version = data.get("version", "?")
            profile = data.get("profile", "?")
            self.query_one("#conn-status", Static).update(
                f"[{theme.GREEN}]●[/]  [{theme.OVERLAY0}]"
                f"{self._client.base_url}  ·  {profile}  ·  v{version}[/]"
            )
        except AicafConnectionError:
            self.query_one("#conn-status", Static).update(
                f"[{theme.RED}]✕[/]  [{theme.RED}]"
                f"orchestrator unreachable — {self._client.base_url}[/]"
            )
        except Exception:
            pass

        try:
            models = await self._client.get_models_for_role("coder")
            if models:
                self.query_one("#chat-model-label", Static).update(
                    f"[{theme.OVERLAY0}]Default model:[/] "
                    f"[{theme.OVERLAY1}]{models[0]['name']}[/]"
                )
        except Exception:
            pass

        try:
            active = await self._client.list_sessions(status="active")
            known: set[str] = set()
            for p in self._store.list_projects():
                known.update(p.session_ids)
            orphans = [s for s in active if s["session_id"] not in known]
            if orphans:
                self.query_one("#orphan-header", Static).update(
                    f"\n[bold {theme.YELLOW}]Active orphan sessions "
                    f"(not linked to a local project)[/]"
                )
                ol = self.query_one("#orphan-list")
                for sess in orphans[:5]:
                    task = truncate(sess.get("task", ""), 40)
                    ol.mount(Static(
                        f"  [{theme.YELLOW}]◐[/]  [{theme.TEXT}]{task}[/]  "
                        f"[{theme.OVERLAY0}]{sess['session_id'][:12]}…[/]"
                    ))
        except Exception:
            pass

    # ── Navigation ────────────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        k = event.key

        # Project list navigation — always available
        if k in ("up", "k"):
            if self._selected_idx > 0:
                self._selected_idx -= 1
                self._render_projects()
            event.stop()
        elif k in ("down", "j"):
            if self._selected_idx < len(self._projects) - 1:
                self._selected_idx += 1
                self._render_projects()
            event.stop()
        elif k == "enter":
            # Enter on project list OR on focused button
            focused = self.focused
            if isinstance(focused, Button):
                return  # let button handle it
            if self._projects:
                self._open_project(self._projects[self._selected_idx].id)
            event.stop()
        elif k == "n":
            self.app.push_screen("new_project")
        elif k == "c":
            self._open_chat()
        elif k in ("q", "ctrl+q"):
            self.app.exit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-chat-btn":
            self._open_chat()

    def on_project_card_selected(self, event: ProjectCard.Selected) -> None:
        self._open_project(event.project_id)

    def _open_project(self, project_id: str) -> None:
        self._store.save_ui_state(last_project_id=project_id)
        self.app.push_screen("project", project_id=project_id)

    def _open_chat(self) -> None:
        try:
            role = str(self.query_one("#chat-role-select", Select).value)
        except Exception:
            role = "coder"
        self.app.push_screen("chat", role=role)