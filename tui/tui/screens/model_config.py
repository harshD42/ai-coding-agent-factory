"""
screens/model_config.py — Full-screen model configuration overlay.

Triggered by pressing `m` in the session screen.
Wraps ModelPanel in a focused overlay. On Apply: calls
client.configure_models(), updates AppState.session.models, pops screen.
"""

import asyncio

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Static
from textual.containers import Container

import tui.theme as theme
from tui.client import AicafClient, AicafConnectionError
from tui.state import AppState
from tui.widgets.footer_bar import FooterBar
from tui.widgets.model_panel import ModelPanel


class ModelConfigScreen(Screen):

    def __init__(
        self,
        client:     AicafClient,
        state:      AppState,
        session_id: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._client     = client
        self._state      = state
        self._session_id = session_id
        self._applying   = False

    def compose(self) -> ComposeResult:
        current = (
            self._state.session.models
            if self._state.session else {}
        )
        yield FooterBar("model_config", id="footer")
        yield Container(
            Static(
                f"[bold {theme.BLUE}]Model Configuration[/]  "
                f"[{theme.OVERLAY0}]session: {self._session_id[:12]}…[/]  "
                f"[{theme.OVERLAY0}][Esc] cancel[/]"
            ),
            Static(f"[{theme.SURFACE1}]{'─' * 60}[/]"),
            ModelPanel(
                client=self._client,
                initial_models=current,
                id="config-panel",
            ),
            Static("", id="config-status"),
            id="model-config-container",
        )

    def on_model_panel_models_configured(
        self, event: ModelPanel.ModelsConfigured
    ) -> None:
        if not self._applying:
            asyncio.create_task(self._apply(event.models))

    async def _apply(self, models: dict[str, str]) -> None:
        self._applying = True
        try:
            status_w = self.query_one("#config-status", Static)
            status_w.update(f"[{theme.SKY}]◐  Applying…[/]")

            await self._client.configure_models(self._session_id, models)

            # Update in-memory state
            if self._state.session is not None:
                self._state.session.models.update(models)

            status_w.update(f"[{theme.SUCCESS}]✓  Models updated[/]")
            await asyncio.sleep(0.6)
            self.app.pop_screen()

        except AicafConnectionError as e:
            try:
                self.query_one("#config-status", Static).update(
                    f"[{theme.ERROR}]✕  {e}[/]"
                )
            except Exception:
                pass
        finally:
            self._applying = False

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()