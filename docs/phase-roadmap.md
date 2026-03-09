# Phase Roadmap

## Status legend
| Symbol | Meaning |
|---|---|
| ✅ | Complete and shipped |
| 🔧 | In progress |
| 🔲 | Planned, not started |

---

## Phase 1 — Foundation ✅ Complete (v0.1.0)

**Goal:** A working multi-agent coding system running entirely locally.

| Component | Status |
|---|---|
| Docker stack (Ollama, ChromaDB, Redis, Executor) | ✅ |
| OpenAI-compatible orchestrator API | ✅ |
| 5 agent roles (architect / coder / reviewer / tester / documenter) | ✅ |
| Debate engine (architect vs reviewer, max N rounds) | ✅ |
| Redis-backed task DAG scheduler | ✅ |
| Patch queue (validate → sandbox → apply) | ✅ |
| ChromaDB memory (sessions / codebase / skills / failures) | ✅ |
| 5-tier context manager with token budgeting | ✅ |
| Skill loader + session hooks | ✅ |
| /command parser (Cline / Roo Code compatible) | ✅ |
| PowerShell CLI | ✅ |
| CI pipeline + unit tests | ✅ |

---

## Phase 2 — Intelligence ✅ Complete (v0.2.0)

**Goal:** Close the loop — agents apply their own changes, tests run automatically, failures self-correct.

| Component | Status |
|---|---|
| Auto-patch extraction from agent output (`extract_diffs_from_result`) | ✅ |
| Diff auto-enqueue after coder/tester tasks | ✅ |
| Test runner integration (`executor_client.run_tests`) | ✅ |
| Test-fix loop — failures fed back to coder (≤ MAX_FIX_ATTEMPTS) | ✅ |
| Metrics — token counts + latency per agent call | ✅ |
| File watcher — live Redis hash registry via watchdog | ✅ |
| Reranker — ChromaDB search precision via Ollama /api/rerank | ✅ |
| Parallel agent execution via asyncio.gather | ✅ |

---

## Phase 3 — Specialization ✅ Complete (v0.3.0)

**Goal:** Deep understanding of your specific codebase.

| Component | Status |
|---|---|
| Tree-sitter AST indexing — function/class boundary chunks | ✅ |
| Symbol search — `GET /v1/memory/symbol?name=X` | ✅ |
| Fine-tune data collection — JSONL export of (task, diff) pairs | ✅ |
| GitHub webhook — failed CI → coder agent proposes fix | ✅ |
| GitHub webhook — new issue → architect decomposes to task DAG | ✅ |
| Failure pattern learning — auto anti-pattern skill extraction | ✅ |
| Anti-pattern injection into agent context (P2.5 tier) | ✅ |

### Step 3.1 — Tree-sitter AST Indexing
**Files:** `ast_indexer.py` (new), `memory_manager.py`, `context_manager.py`

Replaces line-based chunking with function/class boundary chunks. Agents receive complete, meaningful code units rather than arbitrary windows. Each chunk carries `symbol`, `symbol_type`, `start_line`, `end_line`, `language` metadata. Supported languages: Python, JS, TS, Go, Rust, Java, C, C++. Unsupported files fall back to line chunking.

### Step 3.2 — Fine-Tune Data Collection
**Files:** `fine_tune_collector.py` (new), `session_hooks.py`

Every successful (patch applied + tests pass) session writes a training record in Alpaca format to `/app/memory/training_data.jsonl`. Exportable via `GET /v1/finetune/export`. On laptop profile this is pure data collection — offline LoRA training with LLaMA-Factory or Axolotl.

### Step 3.3 — CI/CD Webhook
**Files:** `webhook_handler.py` (new), `main.py`

`POST /v1/webhook/github` receives GitHub webhook events. On `workflow_run` failure: fetches job logs via GitHub API, spawns coder agent with failure context, enqueues fix diffs. On `issues` opened: spawns architect to decompose, loads task DAG into Redis. HMAC-SHA256 signature validation on every request.

### Step 3.4 — Failure Pattern Learning
**Files:** `memory_manager.py`, `session_hooks.py`, `context_manager.py`

After each session end, `cluster_failures()` groups recent failures by semantic similarity. If a cluster reaches `N_FAILURES_THRESHOLD`, the model extracts an anti-pattern skill saved to ChromaDB with `type=antipattern`. On the next agent call, `context_manager` injects matching anti-patterns as a `## Known Pitfalls` section in the system prompt.

---

## Phase 3.5 — Stability Pass ✅ Complete (v0.3.5)

**Goal:** Fix all known bugs and performance gaps before Phase 4 complexity. No new features — pure correctness.

| Fix | File | Status |
|---|---|---|
| Agent history trim (prevents prompt explosion) | `agent_manager.py` | ✅ |
| Agent idle cleanup (prevents memory leak) | `agent_manager.py` | ✅ |
| AGENTS_DIR from config, not hardcoded | `agent_manager.py` | ✅ |
| Patch queue deque — O(1) popleft | `patch_queue.py` | ✅ |
| Patch queue depth guard | `patch_queue.py` | ✅ |
| summary NameError fix in test_fix_loop | `patch_queue.py` | ✅ |
| Redis patch persistence on enqueue | `patch_queue.py` | ✅ |
| Parallel embedding via asyncio.gather | `memory_manager.py` | ✅ |
| LRU embed cache (OrderedDict, bounded) | `memory_manager.py` | ✅ |
| Incremental indexing — skip unchanged files | `memory_manager.py` | ✅ |
| URL parsing via urlparse (HTTPS-safe) | `memory_manager.py` | ✅ |
| Failure deduplication via content hash | `memory_manager.py` | ✅ |
| MODEL_CALL_TIMEOUT on every dispatch call | `router.py` | ✅ |
| Executor concurrency semaphore | `executor_client.py` | ✅ |
| seccomp annotation on executor container | `docker-compose.yml` | ✅ |

---

## Phase 4A — Model Layer 🔧 In Progress (v0.4.0)

**Goal:** Hardware portability is real and tested. Model selection is dynamic per session. vLLM actually runs and is validated.

### Step 4A.1 — Model Registry ✅
**Files:** `model_registry.py` (new), `context_manager.py`, `main.py`

| Component | Status |
|---|---|
| `MODEL_CATALOG` — 10 models, role-affinity tags, context_length, vram_approx_gb | ✅ |
| `ROLE_TAG_MAP` — role → tag affinity (tester accepts coder-tagged models) | ✅ |
| `detect_available()` — queries Ollama + vLLM at startup; failures are warnings not crashes | ✅ |
| `get_models_for_role(role)` — filtered, on-disk-first sorted list for TUI selectors | ✅ |
| `get_context_length(model)` — authoritative context window per model | ✅ |
| `build_prompt(model=...)` — per-model token budget replaces global MAX_CONTEXT_TOKENS | ✅ |
| Antipattern confidence filtering (threshold 0.6) in context_manager | ✅ |
| `GET /v1/models/catalog` — full catalog with on_disk status | ✅ |
| `GET /v1/models/for-role?role=X` — role-filtered model list | ✅ |
| `POST /v1/models/pull` — Ollama pull with mid-session guard (409 if agents running) | ✅ |
| `POST /v1/models/refresh` — re-detect without restart | ✅ |
| 41 new unit tests (30 registry + 11 context manager additions) | ✅ |

### Step 4A.2 — Dynamic Model Assignment 🔲
**Files:** `routing_policy.py` (new), `router.py`, `agent_manager.py`, `config.py`, `task_queue.py`, `main.py`

Replace static profile-baked `ROLE_ENDPOINTS`/`ROLE_MODELS` with per-session model assignments in Redis. A `RoutingPolicy` class owns all resolution logic, keeping `router.py` as a thin dispatcher.

| Component | Status |
|---|---|
| `routing_policy.py` — `RoutingPolicy` class with `resolve(role, session_id)` | 🔲 |
| `router.dispatch()` delegates resolution to `RoutingPolicy` | 🔲 |
| `agent_manager` passes `session_id` + `redis` to router; model name to context_manager | 🔲 |
| `Agent.model` field — populated before `_run_agent()` | 🔲 |
| `POST /v1/session/configure` — store role→model map in Redis (TTL 24h) | 🔲 |
| Remove `ROLE_ENDPOINTS` / `ROLE_MODELS` from `config.py` | 🔲 |
| Task leasing via Redis SETNX — prevent duplicate execution on restart | 🔲 |

### Step 4A.3 — vLLM Validation 🔲
**Files:** `config.py`, `file_watcher.py`, `executor/main.py`, `session_hooks.py`, `tests/integration/test_gpu_profiles.py` (new), `docs/hardware-requirements.md` (new)

| Component | Status |
|---|---|
| `PROFILE=auto` hardware detection via nvidia-smi with loud logging | 🔲 |
| GPU profile smoke tests — all three vLLM services, fallback chain | 🔲 |
| `docs/hardware-requirements.md` — VRAM requirements per profile | 🔲 |
| File watcher 500ms debounce — coalesce rapid editor write events | 🔲 |
| Commit-based reindex trigger — index on git commit, not raw file change | 🔲 |
| Executor per-execution limits — `resource.setrlimit` CPU + disk guards | 🔲 |
| Antipattern confidence scoring in session_hooks skill extraction | 🔲 |

### Step 4A.4 — LiteLLM Gateway (optional, flag-gated) 🔲
**Files:** `gateway.py` (new), `router.py`, `config.py`, `requirements.txt`

| Component | Status |
|---|---|
| `gateway.py` — thin LiteLLM wrapper, `USE_LITELLM=false` by default | 🔲 |
| `router.dispatch()` checks flag, routes through gateway if enabled | 🔲 |
| Zero behaviour change when flag is off | 🔲 |

---

## Phase 4B — Terminal Experience 🔲 (v0.5.0)

**Goal:** `$ aicaf` is the product. Agents become long-lived. Users interact in real time.

### Step 4B.1 — Persistent Agent Sessions 🔲
**Files:** `session_manager.py` (new), `agent_manager.py`, `main.py`

Agents become long-lived async tasks with an `asyncio.Queue` inbox. Sessions are Redis-backed and survive orchestrator restarts (state checkpointed, not full history).

| Component | Status |
|---|---|
| `SessionState` Pydantic model — status, models, agent_ids, task_ids | 🔲 |
| `SessionManager` — create / get / update / end / list sessions | 🔲 |
| Redis key: `session:state:{session_id}` (TTL 7 days) | 🔲 |
| `Agent` state machine: IDLE → ASSIGNED → RUNNING → WAITING_FOR_INPUT → COMPLETE | 🔲 |
| `AgentManager.send_message(agent_id, msg)` — push to asyncio.Queue inbox | 🔲 |
| `AgentManager.subscribe_stream(agent_id)` — yield token chunks from outbox | 🔲 |
| `GET /v1/sessions` — list sessions (optional ?status= filter) | 🔲 |
| `GET /v1/sessions/{id}` — session state | 🔲 |
| `POST /v1/sessions` — create session | 🔲 |
| `POST /v1/sessions/{id}/end` — end session + trigger agent cleanup | 🔲 |

### Step 4B.2 — Streaming Endpoints 🔲
**Files:** `main.py`, `models.py`, `requirements.txt`

Tokens flow direct: Model → Router → WebSocket/SSE → TUI. The bus carries only structured events, never raw tokens.

| Component | Status |
|---|---|
| `sse-starlette`, `websockets` added to requirements.txt | 🔲 |
| `WSEvent`, `WSEventType`, `SessionConfigRequest`, `AgentMessageRequest` in models.py | 🔲 |
| `WebSocket /ws/session/{session_id}` — full-duplex structured event channel | 🔲 |
| `GET /v1/agents/{id}/stream` — SSE token stream per agent | 🔲 |
| `POST /v1/agents/{id}/message` — send message to specific agent | 🔲 |
| WebSocket disconnect handled cleanly | 🔲 |

### Step 4B.3 — Inter-Agent Message Bus 🔲
**Files:** `agent_bus.py` (new), `agent_manager.py`, `main.py`

Internal agent-to-agent messaging uses `asyncio.Queue` (single node, zero infrastructure overhead). Redis pub/sub is used only for the WebSocket broadcast path — crossing the network boundary to the TUI.

| Component | Status |
|---|---|
| `AgentBus` — per-session `asyncio.Queue` for internal events | 🔲 |
| `publish()` — puts on in-process queue AND Redis pub/sub for WebSocket | 🔲 |
| `subscribe_architect()` — filters to: work_complete, work_failed, debate_point, patch_applied, test_result | 🔲 |
| `subscribe_session()` — all events, used by WebSocket handler | 🔲 |
| `cleanup_session()` — removes queue when session ends | 🔲 |
| Architect receives work_complete → delegates next DAG task reactively | 🔲 |
| Raw token events never appear on bus (SSE path only) | 🔲 |
| Phase 5 note: replace asyncio.Queue with NATS JetStream if multi-node needed | 🔲 |

### Step 4B.4 — TUI Package 🔲
**Files:** `tui/` (new package — pipx installable)

| Component | Status |
|---|---|
| `tui/main.py` — `$ aicaf` entry point, argparse (--url, --session, --profile) | 🔲 |
| `tui/app.py` — Textual App, screen routing | 🔲 |
| `tui/client.py` — WebSocket + SSE + REST client to orchestrator | 🔲 |
| `tui/screens/welcome.py` — model selection per role, session config | 🔲 |
| `tui/screens/session.py` — multi-pane streaming session screen | 🔲 |
| `tui/screens/model_pull.py` — download progress (startup only) | 🔲 |
| `tui/widgets/agent_pane.py` — per-agent streaming text window | 🔲 |
| `tui/widgets/input_bar.py` — user input with @agent targeting | 🔲 |
| `tui/widgets/status_bar.py` — tokens, patches, session ID, elapsed time | 🔲 |
| `tui/widgets/dag_view.py` — live task DAG with color-coded status nodes | 🔲 |
| `tui/pyproject.toml` — `pipx install ./tui` → `aicaf` command | 🔲 |
| `AICAF_URL` env var — point TUI at remote GPU server | 🔲 |

---

## Phase 5 — Production Scale 🔲 (v0.6.0, future)

**Goal:** Multi-user, distributed, observable. Not needed for single-developer use.

| Step | Description | Status |
|---|---|---|
| 5.1 Postgres | Long-term session, patch, and metrics persistence. Redis stays for queues and pub/sub. Skills/antipatterns/fine-tune migrated from ChromaDB. | 🔲 |
| 5.2 NATS | Replace Redis pub/sub in `agent_bus.py` with NATS JetStream. Only if running distributed — adds ops burden not worth it on single node. | 🔲 |
| 5.3 Qdrant | Replace ChromaDB for >1M vectors. Hybrid sparse+dense search. Payload filtering per project/session. | 🔲 |
| 5.4 Multi-user | Per-project Redis + ChromaDB namespaces. API key auth middleware. Project switcher in TUI. | 🔲 |
| 5.5 Observability | OpenTelemetry traces per agent run. Prometheus metrics at `/metrics`. Structured log export. Grafana dashboard. | 🔲 |

---

## Sequence Diagram — Phase 3 Full Flow

```
User: /architect "add OAuth2 login"
  │
  ▼
Architect agent
  ← context includes AST symbol chunks (P2)
  ← context includes known pitfalls (P2.5, if any)
  → plan text
  │
  ▼
Debate: reviewer critiques → architect revises (≤3 rounds)
  │
  ▼
decompose_plan_to_tasks() → JSON task DAG → Redis
  │
User: /execute
  │
  ▼
TaskQueue.execute_plan()  [parallel via asyncio.gather]
  ├─ t1 (coder): implement OAuth2 handler
  │     │
  │     ▼
  │   agent output → extract_diffs_from_result()
  │     │
  │     ▼
  │   patch_queue.enqueue(diff) → snapshot file hashes
  │     │
  │     ▼
  │   conflict check → sandbox → live apply
  │     │
  │     ▼
  │   executor.run_tests("tests/")
  │     ├─ PASS → record training example (3.2) → task complete ✅
  │     └─ FAIL → coder fixes → re-apply (≤MAX_FIX_ATTEMPTS)
  │
  ├─ t2 (tester): write tests [waits for t1]
  └─ t3 (documenter): update README [waits for t1]
  │
  ▼
session_hooks.on_session_end()
  → save to ChromaDB
  → extract_skills() → save if pattern found
  → _mine_failure_patterns() → save anti-pattern if threshold reached

Meanwhile (async):
  GitHub CI fails → POST /v1/webhook/github
    → coder agent analyzes logs
    → fix diff enqueued automatically
```

---

## Sequence Diagram — Phase 4B Full Flow

```
$ aicaf
  │
  ▼
TUI welcome screen
  → GET /v1/models/catalog
  → GET /v1/models/for-role?role=architect (+ coder, reviewer, tester, documenter)
  → user selects model per role
  → POST /v1/session/configure  {session_id, models: {role: model}}
  → POST /v1/sessions           {task: "..."}
  │
  ▼
TUI session screen
  → WebSocket /ws/session/{id}   (structured events)
  → SSE /v1/agents/{id}/stream   (token stream per agent)
  │
  ▼
User types: "Refactor auth module to use JWT"
  │
  ▼
Architect agent
  ← routing_policy.resolve("architect", session_id) → Redis → Qwen3.5-35B
  ← context_manager.build_prompt(model="Qwen3.5-35B") → budget = 65536 - 2048
  → plan → decompose → DAG → Redis
  │
  ▼
TaskQueue parallel execution
  ├─ t1 (coder)    → patch → executor → tests → PASS ✅
  ├─ t2 (tester)   → parallel with t2 once t1 done
  └─ t3 (reviewer) → critique → debate_point event → architect
  │
  ▼
Agent bus events (asyncio.Queue internal):
  work_complete → architect delegates next task reactively
  patch_applied → TUI DAG node goes green
  test_result   → TUI shows pass/fail summary

User types: @coder add JWT expiry handling
  → POST /v1/agents/{coder_id}/message
  → coder asyncio.Queue inbox receives message
  → processed after current task completes
  │
  ▼
session_hooks.on_session_end()
  → skill extracted: "JWT implementation pattern for FastAPI"
  → antipattern saved: "fixed window counter is not true rate limiting" (confidence: 0.85)
  → fine-tune record written for each successful patch
```

---

## Redis Key Schema (canonical — all phases)

All Redis keys across all phases follow this schema. No ad-hoc keys permitted.

```
# Task DAG
dag:{session_id}                        HASH   task_id → JSON task state
dag:{session_id}:order                  LIST   topological execution order

# Task leasing (Phase 4A.2 — prevents duplicate execution on restart)
task:{session_id}:{task_id}:lease       STRING worker_id  (SETNX + TTL 600s)

# Patch queue
patches:{session_id}                    HASH   patch_id → JSON patch state

# Agent state
agent:inbox:{agent_id}                  LIST   pending messages
agent:outbox:{agent_id}                 PUBSUB token stream to WebSocket

# Session
session:state:{session_id}              STRING JSON SessionState  (TTL 7 days)
session:models:{session_id}             HASH   role → model_name  (TTL 24h)

# File watcher
filewatch:hashes                        HASH   filepath → SHA-256
filewatch:events                        PUBSUB channel

# Agent bus (WebSocket fan-out only — internal comms use asyncio.Queue)
bus:session:{session_id}                PUBSUB JSON WSEvent

# Metrics
metrics:{session_id}                    HASH   aggregated counters
```

---

## Module Delta Per Phase

```
Phase 1 (v0.1.0 — complete):
  All base modules in orchestrator/ and executor/

Phase 2 (v0.2.0 — complete):
  utils.py           + extract_diffs_from_result()
  task_queue.py      + auto-patch in execute_plan(), asyncio.gather()
  patch_queue.py     + test_fix_loop()
  executor_client.py + run_tests()
  metrics.py         filled stub — token counts, latency, parse_usage()
  file_watcher.py    NEW — watchdog → Redis hash registry
  memory_manager.py  + rerank()
  router.py          + Ollama token count passthrough
  main.py            + GET /v1/metrics, POST /v1/patches/test

Phase 3 (v0.3.0 — complete):
  ast_indexer.py         NEW — tree-sitter chunking, _line_chunk fallback
  webhook_handler.py     NEW — GitHub workflow_run + issues handler
  fine_tune_collector.py NEW — JSONL training data collection
  memory_manager.py      + index_codebase() uses AST chunker
                         + search_symbol(), cluster_failures(), search_antipatterns()
  context_manager.py     + P2.5 antipattern injection
  session_hooks.py       + _mine_failure_patterns(), record_training_example()
  config.py              + GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN, GITHUB_REPO
                         + N_FAILURES_THRESHOLD, TRAINING_DATA_PATH
  orchestrator/Dockerfile + gcc for tree-sitter compilation
  orchestrator/requirements.txt + tree-sitter + 8 language grammars
  main.py                + GET /v1/memory/symbol, POST /v1/webhook/github
                         + GET /v1/finetune/stats|export, DELETE /v1/finetune/clear

Phase 3.5 (v0.3.5 — complete):
  agent_manager.py   + history trim, idle cleanup, AGENTS_DIR from config
  patch_queue.py     + deque, depth guard, Redis persist, summary fix
  memory_manager.py  + parallel embed, LRU cache, incremental index, URL fix, dedup
  router.py          + MODEL_CALL_TIMEOUT on every dispatch call
  executor_client.py + asyncio.Semaphore on apply_patch + run_tests
  config.py          + MAX_AGENT_HISTORY, AGENT_IDLE_TIMEOUT, AGENTS_DIR,
                       MAX_PATCH_QUEUE_DEPTH, EMBED_CACHE_MAX_SIZE,
                       MAX_EXECUTOR_CONCURRENCY, MODEL_CALL_TIMEOUT
  docker-compose.yml + seccomp annotation on executor

Phase 4A (v0.4.0 — in progress):
  model_registry.py  NEW — catalog, detection, role filtering, pull
  routing_policy.py  NEW — per-session model resolution (4A.2)
  gateway.py         NEW — LiteLLM flag-gated wrapper (4A.4, optional)
  context_manager.py + model= param, _resolve_token_budget(), confidence filter
  router.py          + delegates to RoutingPolicy (4A.2)
  agent_manager.py   + redis ref, model lookup, send_message, subscribe_stream
  config.py          + remove ROLE_ENDPOINTS/ROLE_MODELS, add USE_LITELLM
  task_queue.py      + _acquire_task_lease(), _release_task_lease()
  file_watcher.py    + 500ms debounce, commit-based reindex trigger (4A.3)
  executor/main.py   + resource.setrlimit CPU/disk guards (4A.3)
  session_hooks.py   + confidence score on skill/antipattern extraction (4A.3)
  main.py            + /v1/models/catalog|for-role|pull|refresh
                     + /v1/session/configure (4A.2)

Phase 4B (v0.5.0 — planned):
  session_manager.py NEW — persistent session lifecycle
  agent_bus.py       NEW — asyncio.Queue internal + Redis pub/sub WebSocket
  models.py          + WSEvent, WSEventType, SessionConfigRequest, AgentMessageRequest
  main.py            + WebSocket /ws/session/{id}
                     + SSE /v1/agents/{id}/stream
                     + POST /v1/agents/{id}/message
                     + GET|POST /v1/sessions, POST /v1/sessions/{id}/end
  requirements.txt   + sse-starlette, websockets
  tui/               NEW package — pipx install ./tui → $ aicaf

Phase 5 (v0.6.0 — future):
  persistence/postgres.py  NEW
  observability/telemetry.py NEW
  gateway.py               extended (if needed beyond flag-gate)
  memory_manager.py        Qdrant client swap (5.3)
  agent_bus.py             NATS swap (5.2, distributed only)
```