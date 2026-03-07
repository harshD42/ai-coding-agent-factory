# 🏭 AI Coding Agent Factory

> A fully local, Dockerized, multi-agent AI coding system powered by open-weight Qwen models.

[![CI](https://github.com/harshD42/ai-coding-agent-factory/actions/workflows/ci.yml/badge.svg)](https://github.com/harshD42/ai-coding-agent-factory/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Phase](https://img.shields.io/badge/Phase-1%20Complete-green.svg)](docs/phase-roadmap.md)

---

## What Is This?

AI Coding Agent Factory is a self-hosted, privacy-first multi-agent coding assistant. It runs entirely on your own hardware — no API keys, no cloud, no data leaving your machine.

You interact through VS Code (Roo Code extension) or a terminal CLI. Behind the scenes, an orchestrator routes your requests to specialized AI agents (architect, coder, reviewer, tester, documenter), manages persistent memory, enforces patch-based file editing, and runs commands in a sandboxed executor.

**Core principle:** Agents are dumb workers. The orchestrator is the brain. Your IDE is just the UI.

---

## Features

- **Multi-agent pipeline** — Architect plans, Coder implements, Reviewer critiques, Tester writes tests, Documenter writes docs
- **Debate engine** — Architect and Reviewer debate plans for up to N rounds before execution
- **Dependency-aware task DAG** — Tasks execute in topological order with Redis-backed state
- **Patch-based editing** — Agents produce unified diffs, never raw files. Patches are validated before applying
- **Persistent memory** — ChromaDB stores sessions, codebase embeddings, skills, and failure history
- **Failure learning** — Failed approaches are recorded and surfaced on similar future tasks
- **Skill extraction** — Reusable patterns are extracted from sessions and injected into future prompts
- **Sandboxed execution** — Commands run in a resource-limited container with no network egress
- **Hardware adaptive** — Laptop (Ollama 7B), single GPU (24GB), multi-GPU server (3× models)
- **Fully local** — All models run on your hardware via Ollama or vLLM

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
│  Command Parser → Router → Agent Manager → Debate Engine│
│  Context Manager → Patch Queue → Task DAG → Session Hooks│
│  Skill Loader → Memory Manager → Metrics                │
└──────┬──────────────┬───────────────────┬───────────────┘
       │              │                   │
┌──────▼──────┐ ┌─────▼──────┐ ┌─────────▼──────────────┐
│  ChromaDB   │ │   Redis    │ │     Executor :9001      │
│  :8100      │ │   :6379    │ │  (sandbox: pytest/npm)  │
│  sessions   │ │  task DAGs │ │  git apply / workspace  │
│  codebase   │ │  live state│ └────────────────────────┘
│  skills     │ └────────────┘
│  failures   │         ┌─────────────────────────────┐
└─────────────┘         │      MODEL SERVERS          │
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
# Edit .env — set PROFILE, PROJECT_PATH
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

# Re-index after pointing at a real project
docker compose restart executor orchestrator
```

### 5. Connect your IDE

**Roo Code (VS Code):**
- API Provider: `OpenAI Compatible`
- Base URL: `http://localhost:9000/v1`
- API Key: `local`
- Model ID: `orchestrator`
- Mode: **Chat**

**Open WebUI:** `http://localhost:3000`

### 6. Use it

```
/architect "add JWT authentication to the API"
/debate
/execute
/status
/memory "rate limiting"
```

Or via CLI:
```powershell
.\cli\agent.ps1 architect "add JWT authentication"
.\cli\agent.ps1 status
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
| `/execute` | Execute task queue |
| `/memory <query>` | Search past sessions |
| `/learn` | Extract skill from session |
| `/status` | System health |
| `/index` | Re-index codebase |

---

## Project Structure

```
ai-coding-agent-factory/
├── docker-compose.yml        # Full stack definition
├── .env.example              # Configuration template
├── Makefile                  # make up/down/test/lint
├── pyproject.toml            # Python project config
├── executor/                 # Sandboxed command runner
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py
├── orchestrator/             # The brain
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py               # FastAPI app + endpoints
│   ├── config.py             # Environment config
│   ├── router.py             # Health-aware model routing
│   ├── agent_manager.py      # Spawn/track/kill agents
│   ├── context_manager.py    # 5-tier prompt compression
│   ├── memory_manager.py     # ChromaDB + embeddings
│   ├── patch_queue.py        # Diff validation + application
│   ├── task_queue.py         # Redis DAG scheduler
│   ├── debate_engine.py      # Multi-round debate
│   ├── skill_loader.py       # Markdown skills → prompts
│   ├── session_hooks.py      # Session lifecycle
│   ├── command_parser.py     # /command detection
│   ├── executor_client.py    # HTTP client for executor
│   ├── models.py             # Pydantic schemas
│   └── utils.py              # Shared utilities
├── agents/                   # Agent system prompts
├── skills/                   # Domain knowledge (add yours)
├── rules/                    # Always-on coding rules
├── commands/                 # Command definitions
├── cli/
│   └── agent.ps1             # PowerShell CLI
├── tests/                    # Test suite
└── docs/                     # Full documentation
```

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| **Phase 1** | ✅ Complete | Foundation: agents, memory, DAG, debate, patches |
| **Phase 2** | 🔲 Planned | Auto-patching, test runner, parallel agents, metrics |
| **Phase 3** | 🔲 Future | AST indexing, fine-tuning, CI/CD integration |
| **Phase 4** | 🔲 Future | LiteLLM, Qdrant, multi-user, VS Code extension |

See [docs/phase-roadmap.md](docs/phase-roadmap.md) for detailed plans.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache 2.0](LICENSE) — matches the Qwen model family license.