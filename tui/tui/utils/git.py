"""
utils/git.py — subprocess git helpers for project workspace detection.

All functions are synchronous (called from non-async Textual event handlers).
All errors are caught and return safe defaults — git being absent or a path
not being a repo must never crash the TUI.
"""

import subprocess
import logging
from pathlib import Path

log = logging.getLogger("git")


def _run(args: list[str], cwd: str) -> tuple[int, str]:
    """Run a git command, return (returncode, stdout). Never raises."""
    try:
        r = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode, r.stdout.strip()
    except Exception as e:
        log.debug("git command failed: %s", e)
        return -1, ""


def detect_repo(path: str) -> dict:
    """
    Detect git repo info for the given workspace path.

    Returns:
        {
          "is_repo":    bool,
          "root":       str,    # absolute repo root (may differ from path)
          "branch":     str,    # current branch name
          "has_changes": bool,  # True if working tree is dirty
          "ahead":      int,    # commits ahead of upstream
          "behind":     int,    # commits behind upstream
        }
    """
    default = {
        "is_repo": False, "root": path, "branch": "",
        "has_changes": False, "ahead": 0, "behind": 0,
    }

    if not Path(path).exists():
        return default

    # Is this a git repo?
    rc, root = _run(["git", "rev-parse", "--show-toplevel"], path)
    if rc != 0:
        return default

    # Branch
    _, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)

    # Dirty check (any modifications/untracked)
    rc_status, status_out = _run(["git", "status", "--porcelain"], root)
    has_changes = bool(status_out.strip()) if rc_status == 0 else False

    # Ahead/behind upstream
    ahead = behind = 0
    _, ab = _run(
        ["git", "rev-list", "--left-right", "--count", f"{branch}@{{u}}...HEAD"],
        root,
    )
    if ab:
        parts = ab.split()
        if len(parts) == 2:
            try:
                behind, ahead = int(parts[0]), int(parts[1])
            except ValueError:
                pass

    return {
        "is_repo":    True,
        "root":       root,
        "branch":     branch or "HEAD",
        "has_changes": has_changes,
        "ahead":      ahead,
        "behind":     behind,
    }


def short_status(git_info: dict) -> str:
    """
    Return a compact status string for display in the project screen.
    E.g. "main  ·  3 changes" or "main  ↑2 ↓1"
    """
    if not git_info.get("is_repo"):
        return "not a git repo"

    branch = git_info.get("branch", "?")
    parts  = [branch]

    if git_info.get("has_changes"):
        parts.append("uncommitted changes")
    if git_info.get("ahead", 0):
        parts.append(f"↑{git_info['ahead']}")
    if git_info.get("behind", 0):
        parts.append(f"↓{git_info['behind']}")

    return "  ·  ".join(parts)