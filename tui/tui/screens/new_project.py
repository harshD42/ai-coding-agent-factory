"""
screens/new_project.py — New project creation form.
"""

import asyncio
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Input, Static
from textual.containers import Container, Horizontal

import tui.theme as theme
from tui.client import AicafClient
from tui.services.project_service import ProjectService
from tui.state import AppState
from tui.store import ProjectStore
from tui.utils.git import detect_repo
from tui.widgets.footer_bar import FooterBar
from tui.widgets.model_panel import ModelPanel


class NewProjectScreen(Screen):

    def __init__(
        self,
        client:  AicafClient,
        store:   ProjectStore,
        state:   AppState,
        proj_svc: ProjectService,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._client   = client
        self._store    = store
        self._state    = state
        self._proj_svc = proj_svc
        self._models:  dict[str, str] = {}
        self._creating = False

    def compose(self) -> ComposeResult:
        yield FooterBar("new_project", id="footer")
        yield Container(
            Static(
                f"[bold {theme.BLUE}]New Project[/]"
                f"  [{theme.OVERLAY0}][Esc] back[/]",
                id="np-title",
            ),
            Static(f"[{theme.SURFACE1}]{'─' * 56}[/]"),

            # Name
            Horizontal(
                Static(f"[{theme.SUBTEXT0}]Name         [/]", classes="form-label"),
                Input(placeholder="my-project", id="inp-name"),
            ),
            # Workspace
            Horizontal(
                Static(f"[{theme.SUBTEXT0}]Workspace    [/]", classes="form-label"),
                Input(placeholder="/path/to/workspace", id="inp-workspace"),
            ),
            Static("", id="git-status"),
            # First task
            Horizontal(
                Static(f"[{theme.SUBTEXT0}]First task   [/]", classes="form-label"),
                Input(placeholder="What should the agents build?", id="inp-task"),
            ),

            Static(""),
            # Model config collapsible
            Collapsible(
                ModelPanel(client=self._client, id="model-panel-inner"),
                title="▸ Model configuration  (using profile defaults)",
                id="model-collapsible",
                collapsed=True,
            ),

            Static(""),
            Button("▶  Create & Start", id="create-btn", classes="primary"),
            Static("", id="create-status"),

            id="new-project-container",
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "inp-workspace":
            self._check_git(event.value)

    def _check_git(self, path: str) -> None:
        if not path:
            return
        info = detect_repo(path)
        try:
            gs = self.query_one("#git-status", Static)
            if info["is_repo"]:
                gs.update(
                    f"  [{theme.GREEN}]✓[/] [{theme.OVERLAY0}]git repo detected  "
                    f"branch: [{theme.BLUE}]{info['branch']}[/][/]"
                )
            else:
                gs.update(f"  [{theme.OVERLAY0}]not a git repo[/]")
        except Exception:
            pass

    def on_model_panel_models_configured(
        self, event: ModelPanel.ModelsConfigured
    ) -> None:
        self._models = event.models
        try:
            coll = self.query_one("#model-collapsible", Collapsible)
            coll.title = f"▾ Model configuration  ({len(self._models)} roles configured)"
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create-btn" and not self._creating:
            asyncio.create_task(self._create())

    async def _create(self) -> None:
        self._creating = True
        try:
            status_w = self.query_one("#create-status", Static)
        except Exception:
            return

        try:
            name      = self.query_one("#inp-name",      Input).value.strip()
            workspace = self.query_one("#inp-workspace",  Input).value.strip()
            task      = self.query_one("#inp-task",       Input).value.strip()
        except Exception:
            self._creating = False
            return

        if not name:
            status_w.update(f"[{theme.ERROR}]Project name is required[/]")
            self._creating = False
            return
        if not workspace:
            status_w.update(f"[{theme.ERROR}]Workspace path is required[/]")
            self._creating = False
            return
        if not task:
            status_w.update(f"[{theme.ERROR}]First task is required[/]")
            self._creating = False
            return

        status_w.update(f"[{theme.SKY}]◐  Creating project…[/]")
        try:
            project, session_id = await self._proj_svc.create_project(
                name=name,
                workspace=workspace,
                task=task,
                models=self._models or None,
            )
            status_w.update(f"[{theme.SUCCESS}]✓  Project created[/]")
            self._store.save_ui_state(
                last_project_id=project.id,
                last_session_id=session_id,
            )
            # Navigate to session screen
            self.app.push_screen(
                "session",
                project_id=project.id,
                session_id=session_id,
            )
        except Exception as e:
            status_w.update(f"[{theme.ERROR}]✕  {e}[/]")
        finally:
            self._creating = False

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()