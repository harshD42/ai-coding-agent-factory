"""
utils.py — Shared utilities: token counting, prompt sanitization, diff helpers.
"""

import re

# ── Token counting ────────────────────────────────────────────────────────────
# Approximation: average English token ≈ 4 chars.
# Good enough for budget enforcement; avoids a tiktoken dependency.

def count_tokens(text: str) -> int:
    """Estimate token count from raw text."""
    return max(1, len(text) // 4)


def count_messages_tokens(messages: list[dict]) -> int:
    """Estimate token count for a list of {role, content} dicts."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            # multi-part content (images etc.)
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        total += count_tokens(content) + 4  # 4 overhead per message
    return total


# ── Prompt injection sanitization ─────────────────────────────────────────────

# Patterns that indicate prompt injection attempts
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"forget\s+(everything|all)\s+(you('ve)?\s+been\s+told|above)",
    r"you\s+are\s+now\s+(a\s+)?(?:DAN|jailbreak|unrestricted)",
    r"system\s*prompt\s*:\s*",
    r"<\s*system\s*>",
    r"\[INST\]|\[/INST\]",          # Llama instruction tokens
    r"###\s*instruction",
    r"act\s+as\s+if\s+you\s+have\s+no\s+restrictions",
    r"pretend\s+(you\s+are|to\s+be)\s+(?:an?\s+)?(?:evil|unrestricted|jailbroken)",
]

_INJECTION_RE = re.compile(
    "|".join(_INJECTION_PATTERNS),
    re.IGNORECASE | re.MULTILINE,
)


def sanitize_context(text: str) -> str:
    """
    Strip known prompt injection patterns from text before it enters a prompt.
    Replaces matched spans with [REDACTED] so token counts stay stable.
    """
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