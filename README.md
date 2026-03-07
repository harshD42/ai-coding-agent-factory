# 🏭 AI Coding Agent Factory

> A fully local, Dockerized, multi-agent AI coding system powered by open-weight Qwen models.

[![CI](https://github.com/harshD42/ai-coding-agent-factory/actions/workflows/ci.yml/badge.svg)](https://github.com/harshD42/ai-coding-agent-factory/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Phase](https://img.shields.io/badge/Phase-3%20Complete-green.svg)](docs/phase-roadmap.md)
[![Release](https://img.shields.io/github/v/release/harshD42/ai-coding-agent-factory)](https://github.com/harshD42/ai-coding-agent-factory/releases)

---

## What Is This?

AI Coding Agent Factory is a self-hosted, privacy-first multi-agent coding assistant. It runs entirely on your own hardware — no API keys, no cloud, no data leaving your machine.

You interact through VS Code (Roo Code extension) or a terminal CLI. Behind the scenes, an orchestrator routes your requests to specialized AI agents (architect, coder, reviewer, tester, documenter), manages persistent memory, enforces patch-based file editing, runs tests automatically, and learns from failures over time.

**Core principle:** Agents are dumb workers. The orchestrator is the brain. Your IDE is just the UI.

---

## Features

**Multi-agent pipeline** — Architect plans, Coder implements, Reviewer critiques, Tester writes tests, Documenter writes docs. Each role has its own system prompt and isolated memory.

**Debate engine** — Architect and Reviewer debate plans for up to N rounds before execution begins.

**Dependency-aware task DAG** — Tasks execute in topological order with Redis-backed state. Independent tasks run concurrently.

**Patch-based editing** — Agents produce unified diffs, never raw files. Every patch is validated against the workspace before applying.

**Automatic test-fix loop** — After a patch applies, pytest runs automatically. If tests fail, the failure is fed back to the coder for a fix diff, up to `MAX_FIX_ATTEMPTS` times.

**AST-aware codebase indexing** — Python, JS, TS, Go, Rust, Java, C, C++ files are indexed at function/class boundaries using tree-sitter. Agents get complete, meaningful code units as context.

**Symbol search** — Ask `GET /v1/memory/symbol?name=multiply` to find any function or class by name across the entire indexed codebase.

**Failure pattern learning** — After enough similar failures accumulate, the system automatically extracts "what not to do" skills and injects them into future agent prompts as known pitfalls.

**Fine-tune data collection** — Every successful (patch applied + tests pass) session is recorded as a training example. Export via `GET /v1/finetune/export` for offline LoRA fine-tuning.

**GitHub CI/CD integration** — Connect your repo's webhook to auto-fix failing CI or decompose new issues into task DAGs.

**Persistent memory** — ChromaDB stores sessions, codebase embeddings, skills, anti-patterns, and failure history. Reranker improves search precision.

**Real-time file watcher** — watchdog monitors `/workspace` and keeps a Redis hash registry live for conflict-free patch application.

**Metrics** — Token counts and latency tracked per agent call, per session, and per role.

**Hardware adaptive** — Laptop (Ollama 7B), single GPU (24GB), multi-GPU server (3× dedicated models).

**Fully local** — All models run on your hardware via Ollama or vLLM.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    YOUR MACHINE / IDE                    │
│         Roo Code / Open WebUI / CLI agent.ps1           │
└─────────────────────┬───────────────────────────────────┘
                      │ HTTP :9000 (OpenAI-compatible)
┌─────────────────────▼───────────────────────────────────┐
│              ORCHESTRATOR (FastAPI :9000)                │
│                                                          │
│  Command Parser  →  Router  →  Agent Manager            │
│  Context Manager →  Patch Queue  →  Task DAG            │
│  AST Indexer     →  Memory Manager  →  Metrics          │
│  Webhook Handler →  Fine-tune Collector                  │
│  Skill Loader    →  Session Hooks  →  File Watcher       │
└──────┬──────────────┬───────────────────┬───────────────┘
       │              │                   │
┌──────▼──────┐ ┌─────▼──────┐ ┌─────────▼──────────────┐
│  ChromaDB   │ │   Redis    │ │     Executor :9001      │
│  :8100      │ │   :6379    │ │  (sandbox: pytest/npm)  │
│  sessions   │ │  task DAGs │ │  git apply / workspace  │
│  codebase   │ │  file hash │ └────────────────────────┘
│  skills     │ │  registry  │
│  failures   │ └────────────┘      ┌─────────────────┐
└─────────────┘                     │   GitHub.com    │
                                    │  webhooks →     │
                        ┌───────────▼─────────────────┐
                        │      MODEL SERVERS          │
                        │  Ollama :11434 (laptop)     │
                        │  vLLM :8001/8002/8003 (GPU) │
                        └─────────────────────────────┘
```

See [docs/architecture.md](docs/architecture.md) for the full design.

---

## Quick Start

### Prerequisites
- Docker + Docker Compose v2
- 8GB RAM minimum (16GB recommended for laptop profile)
- NVIDIA Container Toolkit (for GPU profiles only)

### 1. Clone

```bash
git clone https://github.com/harshD42/ai-coding-agent-factory.git
cd ai-coding-agent-factory
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — set PROFILE and PROJECT_PATH at minimum
```

### 3. Launch

```bash
# Laptop / CPU
docker compose --profile laptop up -d

# Single GPU (24GB)
docker compose --profile gpu-shared up -d

# Multi-GPU server
docker compose --profile gpu up -d
```

### 4. Point at your project

```bash
# In .env:
PROJECT_PATH=/path/to/your/project

# Restart and re-index
docker compose restart executor orchestrator
curl -X POST http://localhost:9000/v1/index
```

### 5. Connect your IDE

**Roo Code (VS Code):**
- API Provider: `OpenAI Compatible`
- Base URL: `http://localhost:9000/v1`
- API Key: `local`
- Model ID: `orchestrator`
- Mode: **Chat**

**Open WebUI:** `http://localhost:3000` (add `--profile monitor` to compose command)

### 6. Use it

```
/architect "add JWT authentication to the API"
/execute
/status
/memory "rate limiting"
/index
```

---

## Hardware Profiles

| Profile | Command | Hardware | Models |
|---------|---------|----------|--------|
| `laptop` | `--profile laptop` | 8GB RAM / any GPU | Ollama qwen2.5-coder:7b |
| `gpu-shared` | `--profile gpu-shared` | 24GB VRAM | vLLM Qwen3-Coder-Next (all roles) |
| `gpu` | `--profile gpu` | 3× GPU | vLLM dedicated per role |

---

## Available Commands

| Command | Description |
|---------|-------------|
| `/architect <task>` | Generate implementation plan |
| `/debate <topic>` | Architect vs Reviewer debate |
| `/review <text>` | Review code or plan |
| `/test <task>` | Write tests |
| `/execute` | Execute task queue (parallel) |
| `/memory <query>` | Search past sessions |
| `/learn` | Extract skill from session |
| `/status` | System health + metrics + training data |
| `/index` | Re-index codebase (AST-aware) |

---

## GitHub Webhook Setup (Optional)

Connect your repo to auto-fix failing CI and decompose issues:

1. Go to your repo → Settings → Webhooks → Add webhook
2. Payload URL: `http://your-server:9000/v1/webhook/github`
3. Content type: `application/json`
4. Secret: set a random string, copy it to `GITHUB_WEBHOOK_SECRET` in `.env`
5. Events: select `Workflow runs` and `Issues`
6. Set `GITHUB_TOKEN` in `.env` (PAT with `repo:read`, `actions:read`)
7. Set `GITHUB_REPO=owner/repo` in `.env`

When CI fails, the coder agent will automatically analyze the logs and enqueue a fix diff.

---

## Project Structure

```
ai-coding-agent-factory/
├── docker-compose.yml            # Full stack definition (3 profiles)
├── .env.example                  # Configuration template
├── Makefile                      # make up/down/test/lint
├── executor/
│   ├── Dockerfile
│   ├── requirements.txt          # fastapi, uvicorn, pytest
│   └── main.py                   # Sandboxed command runner
├── orchestrator/
│   ├── Dockerfile                # python:3.12-slim + gcc (tree-sitter)
│   ├── requirements.txt          # All deps including tree-sitter grammars
│   ├── main.py                   # FastAPI app + all endpoints
│   ├── config.py                 # All env vars
│   ├── router.py                 # Health-aware model routing
│   ├── agent_manager.py          # Spawn/track/kill agents + metrics
│   ├── context_manager.py        # 5-tier prompt building + antipatterns
│   ├── memory_manager.py         # ChromaDB + embedding + reranker + symbols
│   ├── ast_indexer.py            # Tree-sitter AST chunking
│   ├── patch_queue.py            # Diff validation + test-fix loop
│   ├── task_queue.py             # Redis DAG + parallel execution
│   ├── debate_engine.py          # Multi-round debate
│   ├── skill_loader.py           # Markdown skills → prompts
│   ├── session_hooks.py          # Lifecycle + failure pattern mining
│   ├── file_watcher.py           # watchdog workspace monitor
│   ├── webhook_handler.py        # GitHub CI/issue webhook
│   ├── fine_tune_collector.py    # Training data JSONL collection
│   ├── metrics.py                # Token + latency tracking
│   ├── executor_client.py        # HTTP client for executor
│   ├── command_parser.py         # /command detection
│   ├── models.py                 # Pydantic schemas
│   └── utils.py                  # Shared utilities
├── agents/                       # Agent system prompts (markdown)
├── skills/                       # Domain knowledge (add yours here)
├── rules/                        # Always-on coding rules
├── cli/
│   └── agent.ps1                 # PowerShell CLI
├── tests/
│   ├── unit/                     # 280+ unit tests, no Docker needed
│   └── integration/              # Smoke tests, requires running stack
└── docs/
    ├── api-reference.md
    ├── architecture.md
    ├── deployment.md
    ├── phase-roadmap.md
    └── skills-guide.md
```

---

## Roadmap

| Phase | Version | Status | Description |
|-------|---------|--------|-------------|
| **Phase 1** | v0.1.0 | ✅ Complete | Foundation: agents, memory, DAG, debate, patches |
| **Phase 2** | v0.2.0 | ✅ Complete | Auto-patching, test runner, parallel execution, metrics |
| **Phase 3** | v0.3.0 | ✅ Complete | AST indexing, CI webhook, fine-tune collection, failure learning |
| **Phase 4** | v0.4.0 | 🔲 Planned | LiteLLM gateway, Qdrant, multi-user, VS Code extension |

See [docs/phase-roadmap.md](docs/phase-roadmap.md) for full details.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache 2.0](LICENSE) — matches the Qwen model family license.