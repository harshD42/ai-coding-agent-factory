"""
utils/format.py — Display formatting helpers.
"""

import time
from datetime import datetime


def fmt_tokens(n: int) -> str:
    """Format token count compactly: 1234 → '1.2k', 1234567 → '1.2M'"""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}k"
    return f"{n/1_000_000:.1f}M"


def fmt_elapsed(start: float) -> str:
    """Format elapsed seconds as HH:MM:SS."""
    secs = int(time.time() - start)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_relative(ts: float) -> str:
    """Format a Unix timestamp as a relative time string."""
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        mins = int(delta // 60)
        return f"{mins} min ago"
    if delta < 86400:
        hrs = int(delta // 3600)
        return f"{hrs} hour{'s' if hrs > 1 else ''} ago"
    days = int(delta // 86400)
    return f"{days} day{'s' if days > 1 else ''} ago"


def fmt_datetime(ts: float) -> str:
    """Format timestamp as human date."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def truncate(s: str, max_len: int, suffix: str = "…") -> str:
    """Truncate string to max_len characters."""
    if len(s) <= max_len:
        return s
    return s[:max_len - len(suffix)] + suffix


def fmt_path(path: str, max_len: int = 40) -> str:
    """Shorten a filesystem path for display."""
    import os
    try:
        home = os.path.expanduser("~")
        if path.startswith(home):
            path = "~" + path[len(home):]
    except Exception:
        pass
    return truncate(path, max_len)


def fmt_model(model: str) -> str:
    """Shorten a model name for compact display."""
    # e.g. "Qwen/Qwen3.5-35B-A3B" → "Qwen3.5-35B"
    if "/" in model:
        model = model.split("/")[-1]
    # Drop common suffixes
    for suffix in ["-Instruct", "-A3B", "-A22B"]:
        model = model.replace(suffix, "")
    return model


def progress_bar(ratio: float, width: int = 10) -> str:
    """Return a text progress bar: ████████░░"""
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)