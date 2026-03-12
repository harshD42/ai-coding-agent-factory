"""
widgets/agent_pane.py — Per-agent token stream pane.

Each live agent gets one AgentPane. The pane:
  - Shows a colored header: role indicator, model, status
  - Streams tokens via AgentStreamBuffer (50ms flush) into a RichLog
  - Animates the streaming indicator (● ◐ ○ ◑) while tokens flow
  - Dims when agent hibernates (done but session still open)
  - Scroll anchoring: auto-scroll only when viewport is at the bottom;
    new tokens append silently if the user has scrolled up

AgentStreamBuffer:
  Collects tokens and flushes every FLUSH_MS to prevent per-token widget
  updates which cause visible stutter during fast model output.
"""

import asyncio
import time
from typing import Callable, Optional

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

import tui.theme as theme
from tui.state import AgentInfo
from tui.utils.format import fmt_model, truncate

FLUSH_MS   = 50     # token buffer flush interval (ms)
ANIMATE_MS = 200    # streaming indicator animation interval (ms)


# ── Token stream buffer ───────────────────────────────────────────────────────

class AgentStreamBuffer:
    """
    Buffers incoming tokens and flushes to the pane every FLUSH_MS.
    Prevents per-token Textual widget updates (which cause stutter).
    """

    def __init__(self, on_flush: Callable[[str], None]) -> None:
        self._buf: list[str] = []
        self._on_flush       = on_flush
        self._task: Optional[asyncio.Task] = None

    def push(self, token: str) -> None:
        self._buf.append(token)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        if self._buf:
            self._on_flush("".join(self._buf))
            self._buf.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_MS / 1000)
                if self._buf:
                    chunk = "".join(self._buf)
                    self._buf.clear()
                    self._on_flush(chunk)
            except asyncio.CancelledError:
                break


# ── AgentPane ─────────────────────────────────────────────────────────────────

class AgentPane(Widget):
    """Visual pane for one agent. Receives tokens via push_token()."""

    def __init__(self, agent: AgentInfo, **kwargs) -> None:
        super().__init__(id=f"agent-pane-{agent.agent_id}", **kwargs)
        self._agent       = agent
        self._buffer      = AgentStreamBuffer(self._on_flush)
        self._frame_idx   = 0
        self._anim_task:  Optional[asyncio.Task] = None
        self._token_count = 0
        # Scroll anchoring — True means we auto-scroll on new tokens
        self._at_bottom   = True

    # ── Composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(
            self._header_text(),
            id=f"pane-hdr-{self._agent.agent_id}",
            classes="pane-header",
        )
        yield RichLog(
            id=f"pane-log-{self._agent.agent_id}",
            highlight=True,
            markup=True,
            wrap=True,
            classes="pane-log",
        )

    def on_mount(self) -> None:
        self._buffer.start()
        self._update_css_class()

    def on_unmount(self) -> None:
        self._buffer.stop()
        if self._anim_task and not self._anim_task.done():
            self._anim_task.cancel()

    # ── Scroll anchoring ──────────────────────────────────────────────────────

    def on_scroll(self, event) -> None:
        """Track whether the user is at the bottom of the log."""
        try:
            log_w = self.query_one(f"#pane-log-{self._agent.agent_id}", RichLog)
            # RichLog exposes scroll_y and virtual_size.height
            at_bottom = (
                log_w.scroll_y >= log_w.virtual_size.height - log_w.size.height - 2
            )
            self._at_bottom = at_bottom
        except Exception:
            pass

    # ── Header rendering ──────────────────────────────────────────────────────

    def _header_text(self) -> str:
        a        = self._agent
        sym, col = theme.indicator(a.status)

        if a.status in ("running", "streaming"):
            sym = theme.STREAMING_FRAMES[self._frame_idx % len(theme.STREAMING_FRAMES)]
            col = theme.SKY

        role_col  = theme.role_color(a.role)
        role_str  = f"[bold {role_col}]{a.role.upper()}[/]"
        ind_str   = f"[{col}]{sym}[/]"
        model_str = (
            f"[{theme.OVERLAY0}]{fmt_model(a.model)}[/]" if a.model else ""
        )

        status_label = {
            "running":  f"[{theme.SKY}]streaming[/]",
            "idle":     f"[{theme.OVERLAY0}]idle[/]",
            "done":     f"[{theme.BLUE}]done[/]",
            "failed":   f"[{theme.RED}]failed[/]",
            "killed":   f"[{theme.RED}]killed[/]",
            "waiting":  f"[{theme.OVERLAY0}]waiting[/]",
        }.get(a.status, f"[{theme.OVERLAY0}]{a.status}[/]")

        tok_str = (
            f"[{theme.OVERLAY0}]{self._token_count} tok[/]"
            if self._token_count else ""
        )

        left  = f" {ind_str} {role_str}  {model_str}"
        right = f"{tok_str}  {status_label} "
        return f"{left}  [{theme.OVERLAY0}]│[/]  {right}"

    def _refresh_header(self) -> None:
        try:
            self.query_one(
                f"#pane-hdr-{self._agent.agent_id}", Static
            ).update(self._header_text())
        except Exception:
            pass

    def _update_css_class(self) -> None:
        self.remove_class("streaming", "failed", "hibernating")
        if self._agent.status in ("running",):
            self.add_class("streaming")
        elif self._agent.status in ("failed", "killed"):
            self.add_class("failed")
        elif self._agent.status in ("done",):
            self.add_class("hibernating")

    # ── Token ingestion ───────────────────────────────────────────────────────

    def push_token(self, token: str) -> None:
        """Called by session screen for each incoming SSE token."""
        self._buffer.push(token)
        self._token_count += len(token)
        if self._anim_task is None or self._anim_task.done():
            self._anim_task = asyncio.create_task(self._animate())

    def _on_flush(self, chunk: str) -> None:
        """Called by buffer on each 50ms flush — writes to RichLog."""
        try:
            log_w = self.query_one(
                f"#pane-log-{self._agent.agent_id}", RichLog
            )
            log_w.write(chunk, expand=True, shrink=False)
            # Only auto-scroll when the user hasn't scrolled up
            if self._at_bottom:
                log_w.scroll_end(animate=False)
        except Exception:
            pass

    async def _animate(self) -> None:
        """Cycle streaming indicator frames while agent is running."""
        while self._agent.status in ("running",):
            self._frame_idx = (self._frame_idx + 1) % 4
            self._refresh_header()
            await asyncio.sleep(ANIMATE_MS / 1000)

    # ── Status updates ────────────────────────────────────────────────────────

    def update_agent(self, agent: AgentInfo) -> None:
        """Called when agent state changes (WSEvent or polling)."""
        self._agent = agent
        self._update_css_class()
        self._refresh_header()
        if agent.status not in ("running",) and self._anim_task:
            if not self._anim_task.done():
                self._anim_task.cancel()