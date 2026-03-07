# Contributing to AI Coding Agent Factory

Thank you for your interest in contributing. This document explains the process.

## Before You Start

- Check [existing issues](../../issues) to avoid duplicating work
- For large changes, open an issue first to discuss the approach
- All contributions are licensed under Apache 2.0

## Development Setup

```bash
git clone https://github.com/harshD42/ai-coding-agent-factory.git
cd ai-coding-agent-factory
cp .env.example .env
# Edit .env — set PROFILE=laptop for local dev
docker compose --profile laptop up -d
```

Run tests before making changes to establish a baseline:
```bash
make test
```

## Making Changes

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/your-feature-name`
3. Make your changes
4. Add tests for new functionality
5. Run the full test suite: `make test`
6. Run linting: `make lint`
7. Commit using conventional commits (see below)
8. Push and open a Pull Request

## Commit Convention

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(memory): add reranker pass to recall()
fix(router): handle Ollama 400 on tool message format
docs(readme): update hardware requirements
test(patch_queue): add conflict detection tests
chore(deps): bump chromadb to 0.6.4
refactor(agent_manager): extract _load_agent_prompt to skill_loader
```

Types: `feat`, `fix`, `docs`, `test`, `chore`, `refactor`, `perf`, `ci`

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR
- Include tests for any new code
- Update relevant documentation
- Fill in the PR template completely
- PRs require passing CI before merge

## Code Style

- Python: formatted with `ruff format`, linted with `ruff check`
- Max line length: 100
- Type hints on all public functions
- Docstrings on all modules and public classes

Run formatting:
```bash
make lint   # check
make format # fix
```

## Project Structure

See [README.md](README.md#project-structure) for the full layout.

Key rules:
- New functionality goes as **methods in existing modules**, not new files
- Each module has a single coherent domain (see `docs/architecture.md`)
- The orchestrator never touches the filesystem directly — always via executor
- Agents never see each other's reasoning — all cross-agent data is passed explicitly

## Reporting Bugs

Use the [bug report template](../../issues/new?template=bug_report.md).

Include: OS, Docker version, profile (laptop/gpu-shared/gpu), and full error logs.

## Feature Requests

Use the [feature request template](../../issues/new?template=feature_request.md).

Check the [phase roadmap](docs/phase-roadmap.md) first — your idea may already be planned.