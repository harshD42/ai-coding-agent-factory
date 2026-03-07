"""
utils.py — Shared utilities: token counting, prompt sanitization, diff helpers.
"""

import re

# ── Token counting ────────────────────────────────────────────────────────────

def count_tokens(text: str) -> int:
    """Estimate token count from raw text (4 chars ≈ 1 token)."""
    return max(1, len(text) // 4)


def count_messages_tokens(messages: list[dict]) -> int:
    """Estimate token count for a list of {role, content} dicts."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        total += count_tokens(content) + 4
    return total


# ── Prompt injection sanitization ────────────────────────────────────────────

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"forget\s+(everything|all)\s+(you('ve)?\s+been\s+told|above)",
    r"you\s+are\s+now\s+(a\s+)?(?:DAN|jailbreak|unrestricted)",
    r"system\s*prompt\s*:\s*",
    r"<\s*system\s*>",
    r"\[INST\]|\[/INST\]",
    r"###\s*instruction",
    r"act\s+as\s+if\s+you\s+have\s+no\s+restrictions",
    r"pretend\s+(you\s+are|to\s+be)\s+(?:an?\s+)?(?:evil|unrestricted|jailbroken)",
]

_INJECTION_RE = re.compile(
    "|".join(_INJECTION_PATTERNS),
    re.IGNORECASE | re.MULTILINE,
)


def sanitize_context(text: str) -> str:
    """Strip known prompt injection patterns, replacing with [REDACTED]."""
    return _INJECTION_RE.sub("[REDACTED]", text)


# ── Diff / patch helpers ──────────────────────────────────────────────────────

def count_diff_lines(diff: str) -> int:
    """Count changed lines (+/-) in a unified diff, excluding headers."""
    return sum(
        1 for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def extract_file_paths_from_diff(diff: str) -> list[str]:
    """Return the list of files touched by a unified diff."""
    paths = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            paths.append(line[6:].strip())
    return paths


# ── Step 2.1: Diff extraction from agent output ───────────────────────────────

# Fenced blocks tagged ```diff / ```patch / ```udiff
_FENCED_DIFF_RE = re.compile(
    r"```[ \t]*(?:diff|patch|udiff)\r?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# Bare unified diffs: start with --- line, have at least one @@ hunk
_BARE_DIFF_RE = re.compile(
    r"(---[ \t]+\S[^\n]*\n\+\+\+[ \t]+\S[^\n]*(?:\n(?!```).*)*)",
    re.MULTILINE,
)


def extract_diffs_from_result(text: str) -> list[str]:
    """
    Extract unified diff blocks from agent output text.

    Priority order:
      1. Fenced ```diff / ```patch / ```udiff blocks  (most agents use these)
      2. Bare unified diffs starting with '--- '       (fallback)

    Only blocks containing at least one @@ hunk header are returned.
    Duplicates are suppressed.

    Returns a list of diff strings ready to pass to patch_queue.enqueue().
    """
    results: list[str] = []
    seen:    set[str]  = set()

    for m in _FENCED_DIFF_RE.finditer(text):
        diff = m.group(1).strip()
        if "@@" in diff and diff not in seen:
            results.append(diff)
            seen.add(diff)

    # Only run bare-diff search if fenced pass found nothing
    if not results:
        for m in _BARE_DIFF_RE.finditer(text):
            diff = m.group(1).strip()
            if "@@" in diff and diff not in seen:
                results.append(diff)
                seen.add(diff)

    return results