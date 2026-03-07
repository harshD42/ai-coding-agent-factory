"""
skill_loader.py — Load markdown skills into agent prompts via embedding relevance.

Directory layout (all mounted read-only into /app):
    /app/agents/     — system prompts per role
    /app/skills/     — domain knowledge markdown files
    /app/rules/      — always-on rules (injected into every prompt)
    /app/commands/   — command definitions

On agent spawn:
    1. Load role system prompt from agents/{role}.md
    2. Load all rules/ files (always injected)
    3. Use embedding search to find the top-k most relevant skill files
    4. Inject rules + skills into the system prompt
"""

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("skill_loader")

AGENTS_DIR   = Path("/app/agents")
SKILLS_DIR   = Path("/app/skills")
RULES_DIR    = Path("/app/rules")
COMMANDS_DIR = Path("/app/commands")

MAX_SKILL_CHARS = 2000   # truncate individual skill files to this length
MAX_RULES_CHARS = 3000   # truncate combined rules block to this length


class SkillLoader:
    def __init__(self):
        self._skills: dict[str, str] = {}    # path → content
        self._rules:  str            = ""    # combined rules block
        self._loaded = False

    def load(self) -> None:
        """Scan all skill/rule files into memory. Call once at startup."""
        self._skills = {}

        # Load skills
        if SKILLS_DIR.exists():
            for p in SKILLS_DIR.rglob("*.md"):
                try:
                    self._skills[str(p)] = p.read_text(encoding="utf-8")
                except Exception as e:
                    log.warning("failed to load skill %s: %s", p, e)

        # Load rules (always-on, injected into every prompt)
        rules_parts = []
        if RULES_DIR.exists():
            for p in sorted(RULES_DIR.rglob("*.md")):
                try:
                    rules_parts.append(f"### {p.stem}\n{p.read_text(encoding='utf-8')}")
                except Exception as e:
                    log.warning("failed to load rule %s: %s", p, e)
        self._rules = "\n\n".join(rules_parts)[:MAX_RULES_CHARS]

        self._loaded = True
        log.info(
            "skill_loader: %d skills, rules=%d chars",
            len(self._skills), len(self._rules),
        )

    def load_agent_prompt(self, role: str) -> str:
        """Read agents/{role}.md, fall back to a minimal default."""
        path = AGENTS_DIR / f"{role}.md"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except Exception as e:
                log.warning("failed to load agent prompt %s: %s", path, e)
        return f"You are a {role} agent. Complete the task given to you accurately and concisely."

    def find_relevant_skills(self, task: str, k: int = 3) -> list[str]:
        """
        Return the top-k skill file contents most relevant to the task.
        Uses simple keyword overlap scoring (no embedding needed here —
        the memory manager handles semantic search; this is fast path).
        """
        if not self._skills:
            return []

        task_words = set(_tokenize(task))
        scored = []
        for path, content in self._skills.items():
            skill_words = set(_tokenize(content[:500]))   # score on first 500 chars
            overlap     = len(task_words & skill_words)
            if overlap > 0:
                scored.append((overlap, path, content))

        scored.sort(reverse=True)
        return [content[:MAX_SKILL_CHARS] for _, _, content in scored[:k]]

    def build_system_prompt(self, role: str, task: str, extra: str = "") -> str:
        """
        Assemble full system prompt:
            agent role definition
            + always-on rules
            + relevant skills
            + optional extra context
        """
        if not self._loaded:
            self.load()

        parts = [self.load_agent_prompt(role)]

        if self._rules:
            parts.append(f"\n\n## Rules (always apply)\n{self._rules}")

        skills = self.find_relevant_skills(task)
        if skills:
            skill_block = "\n\n---\n\n".join(skills)
            parts.append(f"\n\n## Relevant Skills\n{skill_block}")

        if extra:
            parts.append(f"\n\n## Additional Context\n{extra}")

        return "".join(parts)

    def list_skills(self) -> list[dict]:
        """Return metadata for all loaded skills."""
        return [
            {"path": p, "chars": len(c), "preview": c[:100]}
            for p, c in self._skills.items()
        ]

    def list_commands(self) -> list[str]:
        """Return names of available commands."""
        if not COMMANDS_DIR.exists():
            return []
        return [p.stem for p in sorted(COMMANDS_DIR.glob("*.md"))]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer for keyword matching."""
    import re
    return [w.lower() for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3]


# ── Singleton ─────────────────────────────────────────────────────────────────

skill_loader = SkillLoader()