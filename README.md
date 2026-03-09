# 🏭 AI Coding Agent Factory

> A fully local, Dockerized, multi-agent AI coding system powered by open-weight Qwen models.

[![CI](https://github.com/harshD42/ai-coding-agent-factory/actions/workflows/ci.yml/badge.svg)](https://github.com/harshD42/ai-coding-agent-factory/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Phase](https://img.shields.io/badge/Phase-4B%20Complete-green.svg)](docs/phase-roadmap.md)
[![Release](https://img.shields.io/github/v/release/harshD42/ai-coding-agent-factory)](https://github.com/harshD42/ai-coding-agent-factory/releases)

---

## What Is This?

AI Coding Agent Factory is a self-hosted, privacy-first multi-agent coding assistant. It runs entirely on your own hardware — no API keys, no cloud, no data leaving your machine.

You interact through VS Code (Roo Code extension), a terminal CLI, or the native `aicaf` TUI (Phase 4B). Behind the scenes, an orchestrator routes your requests to specialized AI agents (architect, coder, reviewer, tester, documenter), manages persistent sessions, streams tokens in real time, enforces patch-based file editing, runs tests automatically, and learns from failures over time.

**Core principle:** Agents are dumb workers. The orchestrator is the brain. Your IDE or terminal is the UI.

---

## Features

**Multi-agent pipeline** — Architect plans, Coder implements, Reviewer critiques, Tester writes tests, Documenter writes docs. Each role has its own system prompt, isolated memory, and dedicated model assignment.

**Persistent sessions** — Sessions are Redis-backed and survive orchestrator restarts. Session state, model assignments, agent IDs, and task IDs are all tracked and queryable.

**Real-time token streaming** — Tokens flow from model → agent outbox → SSE endpoint → TUI agent pane with no Redis in the hot path. Each agent has a dedicated SSE stream at `GET /v1/agents/{id}/stream`.

**Agent message bus** — Structured events (work complete, patch applied, test result) flow over a dual-transport bus: `asyncio.Queue` in-process for agent→architect coordination, Redis pub/sub for WebSocket→TUI fan-out.

**Dynamic model assignment** — Each session can override which model each role uses via `POST /v1/session/configure`. Profile defaults apply when no override is set.

**Debate engine** — Architect and Reviewer debate plans for up to N rounds before execution begins.

**Dependency-aware task DAG** — Tasks execute in topological order with Redis-backed state and task leasing (prevents duplicate execution on restart). Independent tasks run concurrently.

**Patch-based editing** — Agents produce unified diffs, never raw files. Every patch is validated against the workspace before applying.

**Automatic test-fix loop** — After a patch applies, pytest runs automatically. If tests fail, the failure is fed back to the coder for a fix diff, up to `MAX_FIX_ATTEMPTS` times.

**AST-aware codebase indexing** — Python, JS, TS, Go, Rust, Java, C, C++ files are indexed at function/class boundaries using tree-sitter. Agents get complete, meaningful code units as context.

**Failure pattern learning** — After enough similar failures accumulate, the system automatically extracts "what not to do" skills and injects them into future agent prompts as known pitfalls.

**Fine-tune data collection** — Every successful (patch applied + tests pass) session is recorded as a training example. Export via `GET /v1/finetune/export` for offline LoRA fine-tuning.

**GitHub CI/CD integration** — Connect your repo's webhook to auto-fix failing CI or decompose new issues into task DAGs.

**Hardware adaptive** — Laptop (Ollama 7B), single GPU (24GB+), multi-GPU server (3× dedicated models). `PROFILE=auto` detects your hardware at startup.

**Fully local** — All models run on your hardware via Ollama or vLLM.

---

## Architecture

### Component Map

```mermaid
graph TB
    subgraph Client["Client Layer"]
        IDE["Roo Code / VS Code"]
        TUI["aicaf TUI (Phase 4B)"]
        CLI["PowerShell CLI"]
    end

    subgraph Orchestrator["Orchestrator :9000 (FastAPI)"]
        direction TB
        MAIN["main.py\nEndpoints + Lifespan"]
        CMD["command_parser"]
        ROUTER["router\nHealth-aware dispatch"]
        RP["routing_policy\nPer-session model resolution"]
        AM["agent_manager\nSpawn · Track · Stream"]
        CTX["context_manager\n6-tier prompt building"]
        BUS["agent_bus\nasyncio.Queue + Redis pub/sub"]
        SM["session_manager\nLifecycle · TTL · State"]
        MR["model_registry\nCatalog · Detection · Pull"]
        PQ["patch_queue\nValidate · Apply · Test-fix loop"]
        TQ["task_queue\nDAG · Leasing · Parallel exec"]
        DE["debate_engine"]
        MEM["memory_manager\nChromaDB · Embed · Rerank"]
        AST["ast_indexer\nTree-sitter chunking"]
        SH["session_hooks\nSkill extract · Antipatterns"]
        FW["file_watcher\nWorkspace hash registry"]
        WH["webhook_handler\nGitHub CI + Issues"]
        FT["fine_tune_collector"]
        METRICS["metrics"]
    end

    subgraph Infra["Infrastructure"]
        REDIS[("Redis :6379\nSession state · DAG\nPub/Sub · Leases")]
        CHROMA[("ChromaDB :8100\nSessions · Codebase\nSkills · Failures")]
        EXEC["Executor :9001\nSandboxed git apply\npytest · npm · cargo"]
    end

    subgraph Models["Model Servers"]
        OLLAMA["Ollama :11434\nLaptop profile"]
        VLLM1["vLLM :8001\nCoder + Tester"]
        VLLM2["vLLM :8002\nArchitect + Documenter"]
        VLLM3["vLLM :8003\nReviewer"]
    end

    IDE -->|"HTTP :9000\nOpenAI-compatible"| MAIN
    TUI -->|"HTTP + WebSocket\nSSE streams"| MAIN
    CLI -->|"HTTP :9000"| MAIN

    MAIN --> CMD
    MAIN --> SM
    MAIN --> MR
    CMD --> AM
    AM --> CTX
    AM --> ROUTER
    AM --> BUS
    CTX --> MEM
    MEM --> AST
    ROUTER --> RP
    RP --> REDIS
    BUS --> REDIS
    SM --> REDIS
    PQ --> EXEC
    PQ --> BUS
    TQ --> REDIS
    TQ --> AM
    MEM --> CHROMA
    SH --> MEM
    FW --> REDIS
    ROUTER --> OLLAMA
    ROUTER --> VLLM1
    ROUTER --> VLLM2
    ROUTER --> VLLM3
```

---

### Request Sequence — `/architect` + `/execute`

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant TUI as TUI / IDE
    participant Orch as Orchestrator
    participant SM as SessionManager
    participant AM as AgentManager
    participant Bus as AgentBus
    participant Router as Router + RoutingPolicy
    participant Model as Model Server
    participant PQ as PatchQueue
    participant Exec as Executor
    participant Redis as Redis
    participant Chroma as ChromaDB

    User->>TUI: POST /v1/sessions {task}
    TUI->>Orch: POST /v1/sessions
    Orch->>SM: create_session(task, models)
    SM->>Redis: SET session:state:{id} + HSET session:models:{id}
    SM-->>TUI: SessionState {session_id}

    User->>TUI: /architect "add JWT auth"
    TUI->>Orch: POST /v1/chat/completions
    Orch->>AM: spawn_and_run(role=architect)
    AM->>SM: register_agent(session_id, agent_id)
    AM->>Router: resolve(role=architect, session_id)
    Router->>Redis: HGET session:models:{id} architect
    Redis-->>Router: model_name
    Router-->>AM: (endpoint, model, backend)
    AM->>Chroma: search_codebase + search_antipatterns + recall
    Chroma-->>AM: context chunks
    AM->>Model: dispatch(stream=True, messages)
    Model-->>AM: token stream
    AM->>AM: put_nowait(token) → agent.outbox
    Note over TUI,AM: TUI polls GET /v1/agents/{id}/stream (SSE)
    AM-->>TUI: token chunks via SSE
    AM->>Bus: publish(WORK_COMPLETE, session_id)
    Bus->>Redis: PUBLISH bus:session:{id}
    Bus-->>TUI: WSEvent via WebSocket

    User->>TUI: /execute
    TUI->>Orch: POST /v1/tasks/execute
    Orch->>Redis: GET ready tasks (deps satisfied)

    loop For each ready task batch (parallel)
        Orch->>Redis: SETNX task:{id}:lease (acquire)
        Orch->>AM: spawn_and_run(role=coder, task)
        AM->>Model: dispatch(stream=True)
        Model-->>AM: token stream → agent.outbox → SSE → TUI
        AM->>Bus: publish(WORK_COMPLETE)
        Bus-->>TUI: WSEvent via WebSocket
        AM->>PQ: enqueue(diff)
        PQ->>Exec: apply_patch(sandbox)
        PQ->>Exec: apply_patch(live)
        PQ->>Exec: run_tests()
        Exec-->>PQ: {passed: true}
        PQ->>Bus: publish(PATCH_APPLIED)
        Bus-->>TUI: WSEvent via WebSocket
        Orch->>Redis: DEL task:{id}:lease (release)
        Orch->>Redis: SET task status=complete
    end

    Orch-->>TUI: {executed, complete, failed}

    User->>TUI: POST /v1/sessions/{id}/end
    TUI->>Orch: end session
    Orch->>SM: end_session(summary, transcript, failures)
    SM->>Chroma: save_session + extract_skills + mine_antipatterns
    SM->>Bus: publish(STATUS/ended)
    Bus->>Redis: PUBLISH bus:session:{id} → WebSocket closes
    SM->>Redis: session:state TTL refresh (7 days, queryable)
```

---

### Streaming Token Path

```mermaid
graph LR
    M["Model Server\nOllama / vLLM"] -->|"SSE chunks\nstream=True"| D["router.dispatch()"]
    D -->|"iterate lines\nparse delta.content"| RA["_run_agent()"]
    RA -->|"put_nowait(token)\nno Redis"| OB["agent.outbox\nasyncio.Queue"]
    OB -->|"subscribe_stream()\nyield token"| SSE["GET /v1/agents/{id}/stream\nEventSourceResponse"]
    SSE -->|"data: token\ndata: [DONE]"| TUI["TUI agent pane\nor SSE consumer"]

    RA -->|"WORK_COMPLETE\nWSEvent"| BUS["AgentBus\npublish()"]
    BUS -->|"put_nowait\nno network"| IQ["asyncio.Queue\narchitect loop"]
    BUS -->|"PUBLISH\nbus:session:{id}"| PUB["Redis pub/sub"]
    PUB -->|"subscribe_session()\nyield WSEvent"| WS["WebSocket\n/ws/session/{id}"]
    WS -->|"send_json(event)"| TUIWS["TUI session screen\nstatus + DAG updates"]
```

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

# Single GPU (24GB+)
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

| Profile | Command | Min VRAM | Models |
|---------|---------|----------|--------|
| `laptop` | `--profile laptop` | 0 (CPU) / 8GB | Ollama qwen2.5-coder:7b (all roles) |
| `gpu-shared` | `--profile gpu-shared` | 48GB | vLLM Qwen3-Coder-Next-80B (all roles) |
| `gpu` | `--profile gpu` | 80GB+ across 3 GPUs | Dedicated model per role |

Set `PROFILE=auto` in `.env` to detect hardware automatically at startup. The decision is logged at `WARNING` level so it's always visible in orchestrator startup logs.

See [docs/hardware-requirements.md](docs/hardware-requirements.md) for full VRAM breakdown.

---

## Available Commands

| Command | Description |
|---------|-------------|
| `/architect <task>` | Generate implementation plan |
| `/debate <topic>` | Architect vs Reviewer debate |
| `/review <text>` | Review code or plan |
| `/test <task>` | Write tests |
| `/execute` | Execute task queue (parallel, DAG-ordered) |
| `/memory <query>` | Search past sessions |
| `/learn` | Extract reusable skill from current session |
| `/status` | System health · metrics · models · sessions |
| `/index` | Re-index codebase (AST-aware, incremental) |

---

## Session & Streaming API (Phase 4B)

### Create a managed session

```bash
curl -X POST http://localhost:9000/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"task": "Add JWT authentication", "models": {"coder": "qwen2.5-coder:7b"}}'
```

### Stream agent tokens (SSE)

```bash
curl -N http://localhost:9000/v1/agents/{agent_id}/stream
# → data: def authenticate(
# → data: token: str
# → data: [DONE]
```

### WebSocket session events

```javascript
const ws = new WebSocket('ws://localhost:9000/ws/session/{session_id}');
ws.onmessage = (e) => {
  const event = JSON.parse(e.data);
  // event.type: work_complete | patch_applied | test_result | status | ...
};
```

### Send message to specific agent

```bash
curl -X POST http://localhost:9000/v1/agents/{agent_id}/message \
  -H "Content-Type: application/json" \
  -d '{"message": "focus on the auth middleware only", "sender": "user"}'
```

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

---

## Project Structure

```
ai-coding-agent-factory/
├── docker-compose.yml            # Full stack definition (3 profiles)
├── .env.example                  # Configuration template
├── Makefile                      # make up/down/test/lint/index/status
├── executor/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py                   # Sandboxed command runner
├── orchestrator/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                   # FastAPI app + all endpoints (v0.5.0)
│   ├── config.py                 # All env vars + profile detection
│   ├── router.py                 # Health-aware model routing
│   ├── routing_policy.py         # Per-session model resolution (Phase 4A.2)
│   ├── model_registry.py         # Model catalog + detection + pull (Phase 4A.1)
│   ├── session_manager.py        # Session lifecycle + Redis state (Phase 4B.1)
│   ├── agent_bus.py              # Dual-transport message bus (Phase 4B.3)
│   ├── agent_manager.py          # Spawn · track · stream · inbox/outbox
│   ├── context_manager.py        # 6-tier prompt building + antipatterns
│   ├── memory_manager.py         # ChromaDB + embedding + reranker + symbols
│   ├── ast_indexer.py            # Tree-sitter AST chunking
│   ├── patch_queue.py            # Diff validation + test-fix loop
│   ├── task_queue.py             # Redis DAG + leasing + parallel execution
│   ├── debate_engine.py          # Multi-round debate
│   ├── models.py                 # Pydantic schemas incl. WSEvent
│   ├── skill_loader.py           # Markdown skills → prompts
│   ├── session_hooks.py          # Lifecycle + failure pattern mining
│   ├── file_watcher.py           # watchdog workspace monitor
│   ├── webhook_handler.py        # GitHub CI/issue webhook
│   ├── fine_tune_collector.py    # Training data JSONL collection
│   ├── gateway.py                # LiteLLM gateway (USE_LITELLM=true, optional)
│   ├── metrics.py                # Token + latency tracking
│   ├── executor_client.py        # HTTP client for executor
│   ├── command_parser.py         # /command detection
│   └── utils.py                  # Shared utilities
├── agents/                       # Agent system prompts (markdown)
├── skills/                       # Domain knowledge (add yours here)
├── rules/                        # Always-on coding rules
├── cli/
│   └── agent.ps1                 # PowerShell CLI
├── tests/
│   ├── unit/                     # 475+ unit tests, no Docker needed
│   └── integration/              # Smoke tests, requires running stack
└── docs/
    ├── api-reference.md
    ├── architecture.md
    ├── deployment.md
    ├── hardware-requirements.md
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
| **Phase 3.5** | v0.3.5 | ✅ Complete | Stability pass: history trim, LRU cache, patch deque, router timeout |
| **Phase 4A** | v0.4.x | ✅ Complete | Model registry, dynamic routing, vLLM validation, LiteLLM gateway |
| **Phase 4B** | v0.5.0 | ✅ Complete | Persistent sessions, token streaming, agent bus, WebSocket |
| **Phase 4B.4** | v0.5.x | 🔲 In Progress | `aicaf` TUI — native terminal interface |
| **Phase 5** | v0.6.0 | 🔲 Planned | Postgres persistence, NATS bus, Qdrant, multi-user, observability |

See [docs/phase-roadmap.md](docs/phase-roadmap.md) for full details.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache 2.0](LICENSE) — matches the Qwen model family license.