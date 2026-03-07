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

## Phase 2 — Intelligence 🔲 Planned (v0.2.0)

**Goal:** Close the loop — agents apply their own changes, tests run automatically, failures self-correct.

### Step 2.1 — Auto-Patch Application
**Files:** `task_queue.py`, `utils.py`

Extract diffs from agent output and submit them to the patch queue automatically. Currently agents produce diffs as text but nothing submits them.

- `utils.extract_diffs_from_result(text) → list[str]` — regex extract ` ```diff` blocks
- `task_queue.execute_plan()` — after each coder task, call extract + enqueue
- `config.MAX_FIX_ATTEMPTS=3` — prevent infinite loops

### Step 2.2 — Test Runner Integration
**Files:** `patch_queue.py`, `executor_client.py`

After a patch applies, automatically run tests and feed failures back to the coder.

- `executor_client.run_tests(pattern="tests/") → dict`
- `patch_queue.test_fix_loop(patch, max_attempts=3)`:
  1. Apply patch
  2. Run `pytest`
  3. If fail: send stderr to coder → get fix diff → re-apply
  4. After max_attempts: flag for human review
- `POST /v1/patches/test` endpoint

### Step 2.3 — Metrics
**Files:** `metrics.py` (new), `agent_manager.py`, `main.py`

Token counting and request timing per agent.

- `metrics.record_request(agent_id, role, tokens_in, tokens_out, latency_ms)`
- `metrics.get_summary() → dict`
- Hook into `agent_manager._run_agent()` before/after model call
- `GET /v1/metrics` endpoint

### Step 2.4 — File Watcher
**Files:** `file_watcher.py` (new), `patch_queue.py`

Real-time conflict detection using `watchdog` instead of snapshot hashing.

- Watch `/workspace` for changes → update hash registry in Redis
- `patch_queue.check_conflict()` queries live registry
- Emit Redis pub/sub events on file change

### Step 2.5 — Reranker
**Files:** `memory_manager.py`

Improve ChromaDB search precision with a cross-encoder reranking pass.

- Pull `nomic-reranker` via Ollama (or Qwen3-Reranker-0.6B via vLLM)
- `memory_manager.rerank(query, results) → list`
- Call after embedding search in `recall()` and `search_codebase()`

### Step 2.6 — Parallel Agent Execution
**Files:** `task_queue.py`

Run independent DAG tasks concurrently with `asyncio.gather()`.

- Group ready tasks by independence
- `config.MAX_PARALLEL_AGENTS=3`
- Patch queue already serializes application — safe for parallel agents

---

## Phase 3 — Specialization 🔲 Future (v0.3.0)

**Goal:** Deep understanding of your specific codebase.

### Step 3.1 — Tree-sitter AST Indexing
Replace line-based chunking with function/class boundary chunking.
- Symbol table: function names, class names, import graph
- `GET /v1/memory/symbol?name=MyClass` endpoint
- Requires: `tree-sitter` + language grammars

### Step 3.2 — Fine-Tuning Pipeline
Train on your (task, diff) pairs from successful sessions.
- Collect training data from `sessions` ChromaDB collection
- LoRA fine-tune Qwen2.5-Coder-7B on your patterns
- Auto-swap fine-tuned model into Ollama

### Step 3.3 — CI/CD Integration
Agents respond to GitHub events.
- Webhook endpoint for GitHub issues → create task
- PR review agent → auto-review on PR creation
- Failing CI → agent gets test output, proposes fix

### Step 3.4 — Failure Pattern Learning
Active use of the `failures` collection.
- After N similar failures, extract "what not to do" skill automatically
- Inject failure warnings into system prompts proactively
- Track success/failure rates per approach and role

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

## Sequence Diagram — Phase 2 Target Flow

```
User: /architect "add OAuth2 login"
  │
  ▼
Architect → plan (text)
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
TaskQueue.execute_plan()
  ├─ t1 (coder): implement OAuth2 handler
  │     │
  │     ▼
  │   agent output → extract_diffs_from_result()
  │     │
  │     ▼
  │   patch_queue.enqueue(diff)
  │     │
  │     ▼
  │   validate → sandbox → git apply
  │     │
  │     ▼
  │   executor.run("pytest tests/")
  │     ├─ PASS → task complete ✅
  │     └─ FAIL → send error to coder → fix diff → re-apply (≤3x)
  │
  ├─ t2 (tester): write tests [waits for t1]
  └─ t3 (documenter): update README [waits for t1]
  │
  ▼
session_hooks.on_session_end()
  → save to ChromaDB
  → extract_skills()
  → record any failures
```

---

## Module Delta Per Phase

```
Phase 1 (complete):
  All modules in orchestrator/ and executor/

Phase 2 adds:
  utils.py          + extract_diffs_from_result()
  task_queue.py     + auto-patch submission in execute_plan()
                    + asyncio.gather() for parallel tasks
  patch_queue.py    + test_fix_loop()
  executor_client.py + run_tests()
  metrics.py        (new file — token counting, request timing)
  file_watcher.py   (new file — watchdog integration)
  memory_manager.py + rerank()
  main.py           + GET /v1/metrics

Phase 3 adds:
  ast_indexer.py    (new file — tree-sitter chunking)
  memory_manager.py + index_codebase() uses AST chunker
                    + search_symbol()
  fine_tuner.py     (new file — training data collection + LoRA)
  webhook.py        (new file — GitHub webhook handler)

Phase 4 adds:
  gateway.py        (new file — LiteLLM client swap)
  auth.py           (new file — API key middleware)
  router.py         (updated — route through LiteLLM)
  memory_manager.py (updated — Qdrant client swap)
```