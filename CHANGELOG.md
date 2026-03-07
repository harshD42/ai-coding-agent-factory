# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2025-03-07 — Phase 1 Complete

### Added

**Infrastructure**
- Docker Compose stack with 3 profiles: `laptop`, `gpu-shared`, `gpu`
- Ollama integration for laptop profile (qwen2.5-coder:7b, nomic-embed-text)
- vLLM integration for GPU profiles (Qwen3-Coder-Next, Qwen3.5, QwQ-32B)
- ChromaDB persistent memory (sessions, codebase, skills, failures collections)
- Redis-backed live session state and task DAG storage
- Sandboxed executor container with git workspace, pytest, npm, go, rust

**Orchestrator**
- OpenAI-compatible API at `:9000` — Cline/Roo Code connects here
- Health-aware model routing with fallback chain
- 5-tier priority context manager with token budgeting and CRLF normalization
- Agent manager with role-based spawning, isolated memory, and watchdog timeout
- Multi-round debate engine (architect vs reviewer, configurable max rounds)
- Redis-backed dependency-aware task DAG scheduler with topological execution
- Patch queue with unified diff validation, conflict detection, and git apply
- ChromaDB memory manager with Ollama embedding (nomic-embed-text)
- Skill loader — markdown skills injected into agent prompts via keyword matching
- Session hooks — start/end lifecycle, failure recording, skill extraction
- Command parser — `/architect`, `/debate`, `/execute`, `/review`, `/test`, `/memory`, `/learn`, `/status`, `/index`

**CLI**
- PowerShell CLI (`cli/agent.ps1`) for all orchestrator operations

**Agent System Prompts**
- architect.md, coder.md, reviewer.md, tester.md, documenter.md

### Known Issues
- Cline agent mode intercepts `/commands` — use Roo Code (Chat mode) or Open WebUI
- Skill extraction requires longer transcripts to trigger (by design)
- Agents do not automatically share results — explicit handoff required (Phase 2)

---

## [Unreleased] — Phase 2

See [docs/phase-roadmap.md](docs/phase-roadmap.md) for planned changes.