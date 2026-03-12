"""
screens/chat.py — Single-agent conversational chat mode.

Layout (all direct children of Screen, no Container wrapper):
  HeaderBar  — docked top
  FooterBar  — docked bottom
  Input      — docked bottom (above footer)
  RichLog    — fills remaining space

This avoids the height-calculation issues that caused the input
to be invisible when wrapped in Container/Horizontal widgets.
"""

import asyncio

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Input, RichLog, Static

import tui.theme as theme
from tui.client import AicafClient, AicafConnectionError
from tui.state import AppState
from tui.store import ProjectStore
from tui.widgets.footer_bar import FooterBar
from tui.widgets.header_bar import HeaderBar

ALL_ROLES = ["architect", "coder", "reviewer", "tester", "documenter", "general"]


class ChatScreen(Screen):

    DEFAULT_CSS = """
    ChatScreen {
        layers: base;
    }
    #chat-header-bar {
        dock: top;
        height: 1;
        background: #181825;
        padding: 0 1;
    }
    #chat-footer {
        dock: bottom;
        height: 1;
    }
    #chat-input {
        dock: bottom;
        height: 3;
        background: #181825;
        border-top: solid #313244;
        border-bottom: none;
        border-left: none;
        border-right: none;
        color: #cdd6f4;
        padding: 0 2;
    }
    #chat-input:focus {
        border-top: solid #89b4fa;
        border-bottom: none;
        border-left: none;
        border-right: none;
    }
    #chat-log {
        background: #1e1e2e;
        padding: 0 2;
    }
    """

    def __init__(
        self,
        client: AicafClient,
        store:  ProjectStore,
        state:  AppState,
        role:   str = "coder",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._client      = client
        self._store       = store
        self._state       = state
        self._role        = role
        self._model       = ""
        self._history:    list[dict] = []
        self._streaming   = False
        self._token_count = 0
        self._at_bottom   = True
        self._loading_line: int = -1  # line index of the spinner

    def compose(self) -> ComposeResult:
        yield HeaderBar(self._state, id="header-bar")
        yield FooterBar("chat", id="chat-footer")
        yield Input(
            placeholder="  Type a message and press Enter…",
            id="chat-input",
        )
        yield RichLog(
            id="chat-log",
            highlight=True,
            markup=True,
            wrap=True,
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_input)
        asyncio.create_task(self._init())

    def _focus_input(self) -> None:
        try:
            self.query_one("#chat-input", Input).focus()
        except Exception:
            pass

    async def _init(self) -> None:
        try:
            models = await self._client.get_models_for_role(self._role)
            if models:
                self._model = models[0]["name"]
        except Exception:
            self._model = "orchestrator"

        self._update_header()
        self._store.add_chat_record(
            role=self._role,
            model=self._model,
            orchestrator_url=self._client.base_url,
        )
        self._write_system(
            f"Connected · [{theme.role_color(self._role)}]{self._role}[/] · "
            f"[{theme.OVERLAY1}]{self._model or 'default'}[/]  "
            f"[{theme.OVERLAY0}]r=role  m=model  Esc=back[/]"
        )

    def _update_header(self) -> None:
        rc  = theme.role_color(self._role)
        sym = "●" if self._streaming else "◉"
        col = theme.SKY if self._streaming else theme.GREEN
        try:
            self.query_one("#header-bar", HeaderBar).refresh_content()
            # Also update the screen title area via a static in the header
        except Exception:
            pass
        # Update input placeholder to show current role/model
        try:
            inp = self.query_one("#chat-input", Input)
            inp.placeholder = (
                f"  [{self._role}:{self._model or '?'}]  Type and press Enter…"
                if not self._streaming
                else "  Waiting for response…"
            )
        except Exception:
            pass

    def _write_system(self, markup: str) -> None:
        try:
            self.query_one("#chat-log", RichLog).write(
                f"[{theme.OVERLAY0}]{markup}[/]"
            )
        except Exception:
            pass

    # ── Input handling ────────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        text = event.value.strip()
        event.input.value = ""
        if not text or self._streaming:
            return
        asyncio.create_task(self._send(text))

    async def _send(self, text: str) -> None:
        log = self.query_one("#chat-log", RichLog)

        # ── User message bubble ───────────────────────────────────────────────
        log.write("")
        log.write(
            f"[bold {theme.BLUE}]You[/]  "
            f"[{theme.SURFACE1}]{'─' * 46}[/]"
        )
        log.write(f"  [{theme.TEXT}]{text}[/]")
        log.write("")
        log.scroll_end(animate=False)

        self._history.append({"role": "user", "content": text})

        # ── Agent label + spinner ─────────────────────────────────────────────
        rc = theme.role_color(self._role)
        log.write(
            f"[bold {rc}]{self._role.upper()}[/]  "
            f"[{theme.SURFACE1}]{'─' * 46}[/]"
        )
        log.write(f"  [{theme.SKY}]◐  thinking…[/]")
        log.scroll_end(animate=False)

        self._streaming = True
        self._update_header()

        # Animate spinner while waiting for first token
        spinner_task = asyncio.create_task(self._spin())
        first_token  = True
        full_response = ""

        try:
            async for token in self._client.chat_stream(
                messages=self._history,
                model=self._model,
            ):
                if first_token:
                    spinner_task.cancel()
                    # Replace spinner line with empty line for response
                    try:
                        log.clear()
                        # Redraw history
                        self._redraw_history(log)
                        # Agent response start
                        log.write(
                            f"[bold {rc}]{self._role.upper()}[/]  "
                            f"[{theme.SURFACE1}]{'─' * 46}[/]"
                        )
                    except Exception:
                        pass
                    first_token = False

                full_response     += token
                self._token_count += len(token)
                log.write(token, expand=True, shrink=False)
                if self._at_bottom:
                    log.scroll_end(animate=False)

        except AicafConnectionError as e:
            spinner_task.cancel()
            log.write(f"\n  [{theme.ERROR}]✕ Connection error: {e}[/]")
        except Exception as e:
            spinner_task.cancel()
            log.write(f"\n  [{theme.ERROR}]✕ Error: {e}[/]")
        finally:
            spinner_task.cancel()
            self._streaming = False
            log.write("")
            log.scroll_end(animate=False)

        if full_response:
            self._history.append({"role": "assistant", "content": full_response})

        self._update_header()
        self._focus_input()

    def _redraw_history(self, log: RichLog) -> None:
        """Redraw all prior conversation turns after clearing."""
        self._write_system(
            f"[{theme.OVERLAY0}]r=role  m=model  Esc=back[/]"
        )
        for msg in self._history[:-1]:  # skip last (current user msg)
            role    = msg["role"]
            content = msg["content"]
            if role == "user":
                log.write("")
                log.write(
                    f"[bold {theme.BLUE}]You[/]  "
                    f"[{theme.SURFACE1}]{'─' * 46}[/]"
                )
                log.write(f"  [{theme.TEXT}]{content}[/]")
                log.write("")
            elif role == "assistant":
                rc = theme.role_color(self._role)
                log.write(
                    f"[bold {rc}]{self._role.upper()}[/]  "
                    f"[{theme.SURFACE1}]{'─' * 46}[/]"
                )
                log.write(f"  {content}")
                log.write("")
        # Current user message
        if self._history:
            last = self._history[-1]
            if last["role"] == "user":
                log.write("")
                log.write(
                    f"[bold {theme.BLUE}]You[/]  "
                    f"[{theme.SURFACE1}]{'─' * 46}[/]"
                )
                log.write(f"  [{theme.TEXT}]{last['content']}[/]")
                log.write("")

    async def _spin(self) -> None:
        """Animate the thinking spinner until cancelled."""
        frames = ["◐", "◓", "◑", "◒"]
        i = 0
        log = self.query_one("#chat-log", RichLog)
        rc  = theme.role_color(self._role)
        while True:
            try:
                await asyncio.sleep(0.15)
                # We can't edit a specific line in RichLog easily,
                # so just append — the spinner effect is implicit
            except asyncio.CancelledError:
                break

    # ── Scroll anchoring ──────────────────────────────────────────────────────

    def on_scroll(self, event) -> None:
        try:
            log = self.query_one("#chat-log", RichLog)
            self._at_bottom = (
                log.scroll_y >= log.virtual_size.height - log.size.height - 2
            )
        except Exception:
            pass

    # ── Key handling ──────────────────────────────────────────────────────────

    def on_key(self, event) -> None:
        k = event.key
        focused_is_input = isinstance(self.focused, Input)

        if k == "r" and not focused_is_input:
            self._cycle_role()
        elif k == "m" and not focused_is_input:
            asyncio.create_task(self._cycle_model())
        elif k == "escape":
            self.app.pop_screen()
        elif k == "ctrl+q":
            self.app.exit()

    def _cycle_role(self) -> None:
        idx = ALL_ROLES.index(self._role) if self._role in ALL_ROLES else 0
        self._role = ALL_ROLES[(idx + 1) % len(ALL_ROLES)]
        self._update_header()
        self._write_system(
            f"role → [{theme.role_color(self._role)}]{self._role}[/]"
        )

    async def _cycle_model(self) -> None:
        try:
            models = await self._client.get_models_for_role(self._role)
            if models:
                names = [m["name"] for m in models]
                idx   = names.index(self._model) if self._model in names else -1
                self._model = names[(idx + 1) % len(names)]
                self._update_header()
                self._write_system(
                    f"model → [{theme.OVERLAY1}]{self._model}[/]"
                )
        except Exception:
            pass