# Architecture

## Design Principles

1. **Agents are dumb workers. The orchestrator is the brain.**
   Agents have no persistent state. All coordination, memory, and routing is the orchestrator's job.

2. **Patch-based editing only.**
   Agents produce unified diffs, never raw files. Every change is validated before touching disk.

3. **Methods, not modules.**
   New functionality is added as methods within existing modules. The module list is stable.

4. **Workspace isolation.**
   The orchestrator reads the workspace read-only. Only the executor writes to it.

5. **Fail loudly, recover gracefully.**
   Failed patches, timed-out agents, and bad diffs are all recorded to the failures collection. Future agents learn from them.

---

## Component Map

```
orchestrator/
├── main.py                  FastAPI app, endpoint definitions, lifespan wiring
├── config.py                All env vars, ROLE_ENDPOINTS, FALLBACK_ORDER
├── models.py                Pydantic schemas (ChatCompletionRequest/Response)
├── router.py                Health-aware dispatch to model backends
├── agent_manager.py         Spawn/track/kill agents, task decomposition, watchdog, metrics hook
├── context_manager.py       5-tier priority context building, token budgeting, antipattern injection
├── memory_manager.py        ChromaDB client, embedding, 4 collections, reranker, symbol search
├── ast_indexer.py           Tree-sitter AST chunking → function/class boundaries (Phase 3.1)
├── patch_queue.py           Diff validation, conflict detection, git apply, test-fix loop
├── task_queue.py            Redis-backed DAG scheduler, parallel execution
├── debate_engine.py         Architect vs Reviewer multi-round debate
├── skill_loader.py          Markdown skill files → agent system prompts
├── session_hooks.py         on_start/on_end/on_failure, skill extraction, failure pattern mining
├── command_parser.py        /command detection from chat messages
├── executor_client.py       HTTP client wrapper for executor container, run_tests()
├── file_watcher.py          watchdog-based workspace hash registry in Redis (Phase 2.4)
├── webhook_handler.py       GitHub webhook receiver — CI failures + issue decomposition (Phase 3.3)
├── fine_tune_collector.py   Training data JSONL collection from successful sessions (Phase 3.2)
├── metrics.py               Token counting, request timing, per-role summaries (Phase 2.3)
└── utils.py                 Token counting, CRLF normalization, diff helpers, extract_diffs_from_result
```

---

## Request Flow

### Normal Chat (no command)

```
User message
  → main.py: POST /v1/chat/completions
  → command_parser.parse() → None (not a command)
  → router.dispatch(role="coder")
      → resolve_endpoint() — check health, walk fallback chain if needed
      → POST ollama:11434/api/chat (laptop) OR vllm:8001/v1/chat/completions (GPU)
      → stream or collect response
  → return OpenAI-format response
```

### /architect Command

```
/architect "build a rate limiter"
  → command_parser.parse() → ParsedCommand(name="architect", args="build...")
  → _handle_command()
  → agent_manager.spawn_and_run(role="architect", task="build...")
      → skill_loader.build_system_prompt("architect", task)
      → context_manager.build_prompt(task, system_prompt, history)
          → memory_manager.search_codebase(task) → AST symbol chunks (P2)
          → memory_manager.search_antipatterns(task) → known pitfalls (P2.5)
          → memory_manager.recall(task) → past sessions + failures (P4)
          → _trim_conversation(history, budget) → (P3 + P5)
      → router.dispatch(role="architect", messages=context)
      → metrics.record_request() ← token counts + latency
      → return plan text
  → wrap in _make_response() → OpenAI format
```

### /execute Command (Task DAG)

```
/execute
  → task_queue.execute_plan(session_id, agent_mgr)
      → get_ready_tasks() — tasks where all deps are complete
      → asyncio.gather() — run independent tasks concurrently (≤ MAX_PARALLEL_AGENTS)
      → for each task:
          → agent_manager.spawn_and_run(role=task.role, task=task.desc)
          → extract_diffs_from_result(output) — find ```diff blocks
          → patch_queue.enqueue(diff) — validate + snapshot hashes
          → patch_queue._apply_patch() — conflict check → sandbox → live apply
          → executor_client.run_tests() — run pytest
          → if fail: coder agent proposes fix → re-apply (≤ MAX_FIX_ATTEMPTS)
          → update_status(task_id, "complete"|"failed")
          → if failed: _propagate_blocked() — mark dependents as blocked
  → return summary
```

### GitHub Webhook — Failed CI

```
POST /v1/webhook/github  (X-GitHub-Event: workflow_run)
  → webhook_handler.verify_signature() — HMAC-SHA256 check
  → handle_workflow_run()
      → _fetch_failed_job_logs(run_id) — GitHub API
      → agent_manager.spawn_and_run(role="coder", task=failure_logs)
      → extract_diffs_from_result(output)
      → patch_queue.enqueue(diff) per diff found
  → return {diffs_found, enqueued, session_id}
```

---

## Memory Architecture

### ChromaDB Collections

| Collection | Contents | Written by | Read by |
|------------|----------|------------|---------|
| `sessions` | Session summaries, decisions | `session_hooks.on_session_end()` | `memory_manager.recall()` |
| `codebase` | AST symbol chunks from workspace | `memory_manager.index_codebase()` | `context_manager.build_prompt()`, `memory_manager.search_symbol()` |
| `skills` | Reusable patterns + anti-patterns | `session_hooks.extract_skills()`, `session_hooks._mine_failure_patterns()` | `skill_loader`, `context_manager` (antipatterns) |
| `failures` | Failed patches + errors | `memory_manager.record_failure()` | `memory_manager.recall()`, `memory_manager.cluster_failures()` |

### Redis Keys

| Key Pattern | Contents | TTL |
|-------------|----------|-----|
| `tasks:{session_id}:{task_id}` | Task JSON (status, result, deps) | None |
| `tasklist:{session_id}` | Ordered list of task IDs | None |
| `filewatch:hashes` | Hash map of workspace file SHA-256s | None |

### Redis Pub/Sub

| Channel | Published by | Payload |
|---------|-------------|---------|
| `filewatch:events` | `file_watcher` | `{"event": "modified"\|"created"\|"deleted", "path": "..."}` |

---

## Context Priority System (6 Tiers)

When building a prompt, the context manager fills the window in priority order:

```
P1   [never cut]:  Agent system prompt + current task description
P2   [never cut]:  Relevant codebase chunks (AST-aware, top-4 symbols)
P2.5 [never cut]:  Anti-pattern warnings from skills collection (top-2)
P3   [cut last]:   Recent conversation messages (last 6 verbatim)
P4   [cut second]: Past session memories + failure records (top-3)
P5   [cut first]:  Older conversation turns (summarized)

Budget: MAX_CONTEXT_TOKENS (24000) - RESPONSE_BUDGET (2048) = 21952 tokens available
```

---

## AST Indexing (Phase 3.1)

When `/v1/index` is called, `ast_indexer.chunk_file()` is used instead of the old line-based chunker:

```
For each file in /workspace:
  → Detect language from extension
  → If supported (py/js/ts/go/rs/java/c/cpp):
      → tree-sitter parse → walk AST
      → Extract function_definition, class_definition, method_definition nodes
      → Each node → one chunk with metadata: symbol, symbol_type, start_line, end_line
      → Uncovered lines → module-level chunk
  → If unsupported or parse fails:
      → Fall back to 100-line overlapping windows

Chunk metadata stored in ChromaDB:
  file, chunk, total_chunks, symbol, symbol_type, start_line, end_line, language
```

---

## Failure Pattern Learning (Phase 3.4)

After each session ends with failures, `session_hooks._mine_failure_patterns()` runs:

```
cluster_failures(query=session_summary, k=30)
  → Fetch recent failures from ChromaDB
  → Group by embedding distance similarity
  → For each cluster with size >= N_FAILURES_THRESHOLD:
      → Ask model: "what anti-pattern caused these N failures?"
      → If pattern identified:
          → save_skill(name="antipattern:X", content="...", type="antipattern")

On next agent call:
  context_manager.build_prompt()
    → search_antipatterns(task, k=2) → P2.5 injection
    → Agent sees: "## Known Pitfalls — Avoid These"
```

---

## Security Model

The executor is the security boundary for filesystem and command access:

```
cap_drop: ALL          — no Linux capabilities
cap_add: DAC_OVERRIDE  — file access within workspace only
no-new-privileges      — cannot escalate
mem_limit: 2g          — OOM-killed if exceeded
cpus: 2                — CPU throttled
pids_limit: 64         — fork bombs prevented
tmpfs: /tmp (1G)       — ephemeral temp space
# NO docker socket     — cannot escape to host
# NO network egress    — cannot call external services
```

Allowed commands whitelist (enforced in executor before exec):
`pytest, npm, node, cargo, go, make, git, bash, sh, cat, ls, find, grep`

---

## Hardware Profiles

### Laptop Profile
```
Ollama (CPU/any GPU)
  └── qwen2.5-coder:7b → all roles via system prompt swap
  └── nomic-embed-text → embeddings
```

### GPU-Shared Profile
```
vLLM (single GPU, 24GB+)
  └── Qwen3-Coder-Next-80B-A3B → all roles via system prompt swap
Qwen3-Embedding-0.6B → embeddings (in-process)
```

### GPU Multi Profile
```
vLLM :8001 (GPU 0) → Qwen3-Coder-Next → coder + tester
vLLM :8002 (GPU 1) → Qwen3.5-35B-A3B → architect + documenter
vLLM :8003 (GPU 2) → QwQ-32B         → reviewer
Qwen3-Embedding-0.6B                  → embeddings (GPU 3 or CPU)
```