"""
theme.py — Catppuccin Mocha palette + semantic color map.

All colors are exact Catppuccin Mocha spec hex values.
Referenced by aicaf.tcss and widget Python code for Rich markup.
"""

# ── Base palette ──────────────────────────────────────────────────────────────
BASE       = "#1e1e2e"   # main background
MANTLE     = "#181825"   # darker — headers, sidebars, bars
CRUST      = "#11111b"   # darkest — very subtle depth
SURFACE0   = "#313244"   # overlay, focused borders
SURFACE1   = "#45475a"   # slightly lighter overlay
SURFACE2   = "#585b70"   # muted elements
OVERLAY0   = "#6c7086"   # muted text, hints
OVERLAY1   = "#7f849c"   # subtext
OVERLAY2   = "#9399b2"   # secondary text
SUBTEXT0   = "#a6adc8"   # secondary content
SUBTEXT1   = "#bac2de"   # near-primary
TEXT       = "#cdd6f4"   # primary text

# ── Accent colors ─────────────────────────────────────────────────────────────
ROSEWATER  = "#f5e0dc"
FLAMINGO   = "#f2cdcd"
PINK       = "#f5c2e7"
MAUVE      = "#cba6f7"
RED        = "#f38ba8"
MAROON     = "#eba0ac"
PEACH      = "#fab387"
YELLOW     = "#f9e2af"
GREEN      = "#a6e3a1"
TEAL       = "#94e2d5"
SKY        = "#89dceb"
SAPPHIRE   = "#74c7ec"
BLUE       = "#89b4fa"
LAVENDER   = "#b4befe"

# ── Semantic colors ───────────────────────────────────────────────────────────
SUCCESS    = GREEN
WARNING    = YELLOW
ERROR      = RED
STREAMING  = SKY
INFO       = BLUE

# ── Agent role colors ─────────────────────────────────────────────────────────
ROLE_COLORS = {
    "architect":  BLUE,
    "coder":      GREEN,
    "reviewer":   PEACH,
    "tester":     YELLOW,
    "documenter": MAUVE,
    "general":    LAVENDER,
}

# ── Agent indicator symbols + colors (by status) ──────────────────────────────
INDICATOR = {
    "streaming":   ("●", SKY),      # cyan — animated
    "active":      ("◉", GREEN),    # green
    "idle":        ("◉", GREEN),    # green
    "waiting":     ("○", OVERLAY0), # gray
    "done":        ("⊙", BLUE),     # blue
    "hibernating": ("⊙", BLUE),     # blue
    "failed":      ("✕", RED),      # red
    "killed":      ("✕", RED),      # red
    "running":     ("●", SKY),      # cyan (same as streaming)
}

# Streaming animation frames — cycled every 200ms
STREAMING_FRAMES = ["●", "◐", "○", "◑"]

# ── Connection status ─────────────────────────────────────────────────────────
CONNECTION_INDICATORS = {
    "connected":     ("●", GREEN),
    "reconnecting":  ("◐", YELLOW),
    "disconnected":  ("○", OVERLAY0),
    "failed":        ("✕", RED),
}

# ── Task status ───────────────────────────────────────────────────────────────
TASK_STATUS = {
    "pending":   ("○", OVERLAY0),
    "running":   ("●", SKY),
    "complete":  ("✓", GREEN),
    "failed":    ("✕", RED),
    "blocked":   ("⊘", PEACH),
    "skipped":   ("−", OVERLAY0),
}

# ── Patch status ──────────────────────────────────────────────────────────────
PATCH_STATUS = {
    "pending":    ("●", YELLOW),
    "applied":    ("✓", GREEN),
    "rejected":   ("✕", RED),
    "conflict":   ("⚠", PEACH),
    "processing": ("◐", SKY),
}

def role_color(role: str) -> str:
    """Return hex color for a role name."""
    return ROLE_COLORS.get(role, LAVENDER)

def indicator(status: str) -> tuple[str, str]:
    """Return (symbol, hex_color) for an agent status string."""
    return INDICATOR.get(status, ("?", OVERLAY0))

def task_indicator(status: str) -> tuple[str, str]:
    return TASK_STATUS.get(status, ("?", OVERLAY0))

def patch_indicator(status: str) -> tuple[str, str]:
    return PATCH_STATUS.get(status, ("?", OVERLAY0))

def markup(text: str, color: str) -> str:
    """Wrap text in Rich color markup."""
    return f"[{color}]{text}[/{color}]"

def role_markup(text: str, role: str) -> str:
    """Wrap text in the role's accent color."""
    return markup(text, role_color(role))