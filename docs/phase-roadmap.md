# Phase Roadmap

## Phase 1 — Foundation ✅ Complete (v0.1.0)

**Goal:** A working multi-agent coding system running entirely locally.

| Component | Status |
|-----------|--------|
| Docker stack (Ollama, ChromaDB, Redis, Executor) | ✅ |
| OpenAI-compatible orchestrator API | ✅ |
| 5 agent roles (architect/coder/reviewer/tester/documenter) | ✅ |
| Debate engine (architect vs reviewer, max N rounds) | ✅ |
| Redis-backed task DAG scheduler | ✅ |
| Patch queue (validate → sandbox → apply) | ✅ |
| ChromaDB memory (sessions/codebase/skills/failures) | ✅ |
| 5-tier context manager with token budgeting | ✅ |
| Skill loader + session hooks | ✅ |
| /command parser (Cline/Roo Code compatible) | ✅ |
| PowerShell CLI | ✅ |
| CI pipeline + unit tests | ✅ |

---

## Phase 2 — Intelligence ✅ Complete (v0.2.0)

**Goal:** Close the loop — agents apply their own changes, tests run automatically, failures self-correct.

| Component | Status |
|-----------|--------|
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
|-----------|--------|
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

## Phase 4 — Scale 🔲 Future (v0.4.0)

**Goal:** Production-ready, multi-user, releasable.

### Step 4.1 — LiteLLM Gateway
- Replace direct vLLM/Ollama routing with LiteLLM
- Load balancing, cost tracking, rate limiting
- Drop-in: change `CODER_URL` to LiteLLM endpoint

### Step 4.2 — Qdrant Migration
- Replace ChromaDB with Qdrant for >1M vector scale
- Sparse+dense hybrid search
- Payload filtering (search within project, within session)

### Step 4.3 — Custom VS Code Extension
- Multi-panel UI: agent activity, task DAG visualization, patch review
- Real-time agent status streaming
- One-click patch approve/reject

### Step 4.4 — Multi-User / Project Isolation
- Per-project ChromaDB collections + Redis namespaces
- Project switcher UI
- Authentication layer (API key or OAuth)
- Shared model servers, isolated memory

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

Phase 4 (v0.4.0 — planned):
  gateway.py        NEW — LiteLLM client swap
  auth.py           NEW — API key middleware
  router.py         updated — route through LiteLLM
  memory_manager.py updated — Qdrant client swap
```