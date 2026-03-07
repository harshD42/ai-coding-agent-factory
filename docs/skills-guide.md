# Skills Guide

Skills are markdown files that get injected into agent system prompts. They teach agents about your specific codebase, conventions, and patterns.

## Directory Layout

```
skills/
├── coding-standards/
│   ├── python-best-practices.md
│   └── your-framework-conventions.md
├── backend-patterns/
│   ├── api-design.md
│   └── database-patterns.md
├── your-codebase/
│   ├── architecture-overview.md
│   └── gotchas-and-workarounds.md
└── continuous-learning/
    └── (auto-populated by session hooks)

rules/
├── security.md          ← always injected into every prompt
├── coding-style.md      ← always injected into every prompt
└── git-workflow.md      ← always injected into every prompt

agents/
├── architect.md         ← system prompt for architect role
├── coder.md             ← system prompt for coder role
├── reviewer.md          ← system prompt for reviewer role
├── tester.md            ← system prompt for tester role
└── documenter.md        ← system prompt for documenter role
```

## How Skills Are Selected

When an agent is spawned for a task, the skill loader:

1. Loads the agent's system prompt from `agents/{role}.md`
2. Injects all `rules/` files (always-on)
3. Scores all `skills/` files by keyword overlap with the task description
4. Injects the top 3 most relevant skill files

Selection is fast (keyword matching, no embedding call needed).

## Writing Effective Skills

Skills work best when they are **specific and actionable**. Compare:

**Bad skill (too vague):**
```markdown
# Python Best Practices
Write clean Python code. Use type hints. Handle errors properly.
```

**Good skill (specific and actionable):**
```markdown
# Python API Patterns (Our Codebase)

## FastAPI Route Convention
All routes use async functions. Input validation via Pydantic models only — never manual validation.
Error responses always use our custom `APIError` class from `src/errors.py`.

## Database Access
Never use raw SQL. All DB access goes through `src/db/repository.py`.
Use `async with db.session() as sess:` pattern for transactions.

## Logging
Use `log = logging.getLogger(__name__)` at module level.
Never use `print()` in production code.
```

## Rules vs Skills

| | Rules | Skills |
|---|---|---|
| **Always injected?** | Yes, every prompt | No, selected by relevance |
| **Purpose** | Hard requirements | Domain knowledge |
| **Examples** | "Never commit secrets", "Use LF line endings" | "Our auth uses JWT RS256", "Rate limiting is in middleware" |

## Writing Rules

Rules should be short, imperative, and non-negotiable:

```markdown
# Security Rules

- Never log passwords, tokens, or PII
- Always validate and sanitize user input before use
- Use parameterized queries — never string-format SQL
- Secrets come from environment variables only, never hardcoded
```

## Customizing Agent Roles

Edit `agents/{role}.md` to change how each role behaves.

Example — making the coder more opinionated about your stack:

```markdown
# Coder Agent

You are an expert Python engineer working on our FastAPI + PostgreSQL codebase.

## Output Format
Always output code changes as unified diffs. Never output raw files.

## Stack
- FastAPI for HTTP layer
- SQLAlchemy async for database
- Pydantic v2 for validation
- pytest + httpx for testing

## Our Conventions
- All endpoints are async
- All DB models inherit from `src/models/base.py:Base`
- Tests live in `tests/` mirroring `src/` structure
```

## Auto-Learning

After each session, `session_hooks.extract_skills()` asks the model if a reusable pattern emerged. If yes, it's saved to `skills/continuous-learning/` automatically.

To manually trigger extraction:
```
/learn
```

Or via CLI:
```powershell
.\cli\agent.ps1 learn
```