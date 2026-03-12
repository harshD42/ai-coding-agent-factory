"""
widgets/model_panel.py — Role → model assignment panel.

Used in two contexts:
  1. NewProjectScreen (inline, inside a Collapsible)
  2. ModelConfigScreen overlay (triggered by m key in session)

Fetches available models from GET /v1/models/for-role for each role
and renders a Select per role. On submit emits ModelsConfigured with
the role→model dict.
"""

import asyncio
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Select, Static

import tui.theme as theme
from tui.client import AicafClient

ALL_ROLES = ["architect", "coder", "reviewer", "tester", "documenter"]


class ModelPanel(Widget):
    """Role → model selectors. Populated from orchestrator catalog."""

    class ModelsConfigured(Message):
        def __init__(self, models: dict[str, str]) -> None:
            super().__init__()
            self.models = models

    def __init__(
        self,
        client: AicafClient,
        initial_models: dict[str, str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._client  = client
        self._current = initial_models or {}
        self._options: dict[str, list[tuple[str, str]]] = {}
        self._loaded  = False

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold {theme.BLUE}]Model Configuration[/]\n"
            f"[{theme.OVERLAY0}]Assign a model to each agent role.[/]\n",
            id="panel-title",
        )
        yield Static(f"[{theme.OVERLAY0}]Loading models…[/]", id="panel-loading")
        yield Button("Apply", id="panel-apply", classes="primary", disabled=True)

    def on_mount(self) -> None:
        asyncio.create_task(self._load_models())

    async def _load_models(self) -> None:
        for role in ALL_ROLES:
            try:
                models = await self._client.get_models_for_role(role)
                self._options[role] = [
                    (
                        f"{m['name']}  "
                        f"{'✓' if m.get('on_disk') else '○'}  "
                        f"{m.get('context_length', 0) // 1024}k ctx",
                        m["name"],
                    )
                    for m in models
                ]
            except Exception:
                self._options[role] = []

        # Remove loading indicator
        try:
            self.query_one("#panel-loading", Static).remove()
        except Exception:
            pass

        # Mount one label + Select per role
        for role in ALL_ROLES:
            opts = self._options.get(role, [])
            cur  = self._current.get(role, "")
            rc   = theme.role_color(role)

            await self.mount(
                Static(f"[{rc}]{role:<12}[/]", classes="role-label"),
                before=self.query_one("#panel-apply"),
            )
            if opts:
                # ── Bug fix: correctly check if stored value is in option values
                option_values = [v for _, v in opts]
                init_val = cur if cur in option_values else opts[0][1]
                sel = Select(
                    options=opts,
                    value=init_val,
                    id=f"sel-{role}",
                )
                await self.mount(sel, before=self.query_one("#panel-apply"))
            else:
                await self.mount(
                    Static(f"[{theme.OVERLAY0}]no models available[/]"),
                    before=self.query_one("#panel-apply"),
                )

        self._loaded = True
        try:
            self.query_one("#panel-apply", Button).disabled = False
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "panel-apply" or not self._loaded:
            return
        models: dict[str, str] = {}
        for role in ALL_ROLES:
            try:
                sel = self.query_one(f"#sel-{role}", Select)
                if sel.value and sel.value != Select.BLANK:
                    models[role] = str(sel.value)
            except Exception:
                if role in self._current:
                    models[role] = self._current[role]
        self.post_message(self.ModelsConfigured(models=models))