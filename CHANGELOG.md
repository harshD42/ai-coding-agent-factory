# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.3.5] — 2026-03-07 — Phase 3.5 Stability Pass

### Fixed

**Agent Manager**
- `agent._history` now trimmed to `MAX_AGENT_HISTORY` (default 20) after every turn — prevents prompt size growing unboundedly in long sessions
- `_load_agent_prompt()` path now read from `config.AGENTS_DIR` instead of hardcoded `/app/agents` — fixes local development outside Docker
- `cleanup_idle_agents()` added — prunes finished agents older than `AGENT_IDLE_TIMEOUT` from the in-memory registry, preventing memory leak in long-running instances

**Patch Queue**
- `_queue` changed from `list` to `collections.deque` — O(1) popleft vs O(n) scan on every `process_next()` call
- `MAX_PATCH_QUEUE_DEPTH` guard added to `enqueue()` — rejects new patches when queue is full instead of growing without bound
- `summary` variable initialized to `""` before the `test_fix_loop` while loop — fixes silent `NameError` that occurred when the loop exited before any test ran
- Patches now persisted to Redis on enqueue via `set_redis()` injection — patch metadata survives orchestrator restart
- `_unpersist_patch()` cleans up Redis on apply/reject/conflict

**Memory Manager**
- `_embed_batch()` now uses `asyncio.gather()` — parallel embedding instead of sequential loop (wall time ≈ single embed time for batches)
- LRU embed cache (`_LRUEmbedCache`) replaces plain dict — `OrderedDict`-based eviction at `EMBED_CACHE_MAX_SIZE` prevents unbounded RAM growth
- `connect()` URL parsed via `urllib.parse.urlparse` — robust against HTTPS URLs, custom paths, and missing ports (old `str.split(':')` failed on all three)
- `index_codebase()` now performs incremental indexing — stores `file_hash` in chunk metadata and skips re-embedding files whose content hasn't changed; second call on unchanged workspace completes in <1s
- `record_failure()` uses content hash as ChromaDB doc ID — identical failures deduplicated automatically via upsert

**Router**
- Non-streaming `dispatch()` now wrapped in `asyncio.wait_for(MODEL_CALL_TIMEOUT)` — stalled vLLM/Ollama endpoint raises `TimeoutError` instead of hanging the agent indefinitely

**Executor Client**
- `asyncio.Semaphore(MAX_EXECUTOR_CONCURRENCY)` added to `apply_patch()` and `run_tests()` — prevents executor container saturation when multiple parallel agents submit patches simultaneously

**Docker**
- `executor` service in `docker-compose.yml` annotated with `seccomp:unconfined` — documents intent to add a custom seccomp profile in Phase 5

### Added
- `POST /v1/agents/cleanup` endpoint — trigger idle agent pruning on demand
- `index_codebase` response now includes `files_unchanged` count
- `/status` command output now includes patch queue depth limit, embed cache size, and executor concurrency slots
- 30 new unit tests covering all Phase 3.5 fixes (history trim, LRU eviction, parallel embed, URL parsing, failure dedup, deque, depth guard, semaphore, router timeout)
- `tests/integration/test_phase35.py` — 8 smoke tests verifying all fixes end-to-end

### Changed
- Orchestrator version bumped to `0.3.5`
- `cleanup_idle_agents()` called automatically on `POST /v1/session/end`

### Config vars added
| Variable | Default | Description |
|---|---|---|
| `MAX_AGENT_HISTORY` | `20` | Max conversation turns kept per agent |
| `AGENT_IDLE_TIMEOUT` | `3600` | Seconds before finished agent is pruned |
| `AGENTS_DIR` | `/app/agents` | Directory for agent `.md` prompt files |
| `MAX_PATCH_QUEUE_DEPTH` | `50` | Max queued patches before rejection |
| `EMBED_CACHE_MAX_SIZE` | `1000` | LRU embed cache max entries |
| `MAX_EXECUTOR_CONCURRENCY` | `2` | Max concurrent sandbox operations |
| `MODEL_CALL_TIMEOUT` | `120` | Per-call model HTTP timeout (seconds) |

---

## [0.3.0] — 2026-03-07 — Phase 3 Complete

### Added

**Step 3.1 — AST Indexing**
- `ast_indexer.py` — tree-sitter chunking for Python, JS, TS, Go, Rust, Java, C, C++
- `memory_manager.index_codebase()` now uses symbol-boundary chunks (function/class level)
- Each chunk carries `symbol`, `symbol_type`, `start_line`, `end_line`, `language` metadata
- `memory_manager.search_symbol(name)` — find any function or class by name
- `GET /v1/memory/symbol?name=X` endpoint
- `orchestrator/Dockerfile` — added `gcc` for tree-sitter C extension compilation
- `orchestrator/requirements.txt` — `tree-sitter==0.23.2` + 8 language grammar packages
- `context_manager` codebase chunks now display symbol name and type in system prompt

**Step 3.2 — Fine-tune Data Collection**
- `fine_tune_collector.py` — appends `(instruction, input, output)` records on successful patches
- `GET /v1/finetune/stats` — training data record count and file size
- `GET /v1/finetune/export` — download JSONL in Alpaca format
- `DELETE /v1/finetune/clear` — delete all collected records
- `session_hooks.record_training_example()` — called when patch applies and tests pass
- `config.TRAINING_DATA_PATH` env var

**Step 3.3 — GitHub Webhook**
- `webhook_handler.py` — HMAC-SHA256 signature validation, event routing
- `workflow_run` event: fetches failed CI logs via GitHub API, spawns coder, enqueues fix diffs
- `issues` opened event: architect decomposes issue body into task DAG, loads into Redis
- `POST /v1/webhook/github` endpoint
- `config.GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN`, `GITHUB_REPO` env vars

**Step 3.4 — Failure Pattern Learning**
- `memory_manager.cluster_failures()` — groups failures by embedding distance similarity
- `memory_manager.search_antipatterns()` — query skills filtered by `type=antipattern`
- `session_hooks._mine_failure_patterns()` — auto-extracts anti-pattern skills when cluster reaches threshold
- `context_manager` P2.5 tier — injects `## Known Pitfalls` section from anti-pattern skills
- `config.N_FAILURES_THRESHOLD` env var (default 3)

### Changed
- `memory_manager.save_skill()` now accepts optional `metadata` dict (used for `type=antipattern` tag)
- `/status` command now includes training data record count
- `orchestrator` version bumped to `0.3.0`

---

## [0.2.0] — 2026-03-07 — Phase 2 Complete

### Added

**Step 2.1 — Auto-Patch Application**
- `utils.extract_diffs_from_result(text)` — regex extraction of ` ```diff/patch/udiff ` blocks from agent output
- `task_queue.set_patch_queue(pq)` — dependency injection to avoid circular imports
- `task_queue._auto_apply_patches()` — auto-enqueues diffs after coder/tester tasks
- `task_queue._run_single_task()` — extracted for parallel execution support
- `config.MAX_FIX_ATTEMPTS=3`

**Step 2.2 — Test Runner Integration**
- `executor_client.run_tests(pattern, timeout)` — runs pytest inside executor sandbox
- `patch_queue.test_fix_loop(patch, agent_mgr, max_attempts)` — apply → test → fix loop
- `POST /v1/patches/test` endpoint
- `executor/requirements.txt` — added `pytest`
- `executor/main.py` lifespan — baseline git commit on startup so `git apply` works

**Step 2.3 — Metrics**
- `metrics.py` filled from stub — `record_request()`, `get_summary()`, `get_session_summary()`
- `metrics.parse_usage()` — extracts token counts from both Ollama and vLLM response shapes
- `agent_manager._run_agent()` — hooks `metrics.record_request()` before/after model call
- `router.py` — Ollama non-streaming response now passes `prompt_eval_count`/`eval_count` as `usage`
- `GET /v1/metrics` endpoint with optional `session_id` filter
- `/status` command includes metrics summary

**Step 2.4 — File Watcher**
- `file_watcher.py` — watchdog observer, Redis hash registry, pub/sub events
- `FileWatcher.start(redis)` / `.stop()` wired into orchestrator lifespan
- `filewatch:hashes` Redis key — live SHA-256 map of workspace files
- `filewatch:events` Redis pub/sub channel

**Step 2.5 — Reranker**
- `memory_manager.rerank(query, results, top_k)` — Ollama `/api/rerank` with graceful fallback
- Called after embedding search in `recall()` and `search_codebase()`
- `RERANKER_TIMEOUT=5.0s` — skips reranking if model is slow

**Step 2.6 — Parallel Agent Execution**
- `task_queue.execute_plan()` — `asyncio.gather()` for independent task batches
- `config.MAX_PARALLEL_AGENTS=3`

### Fixed
- `executor/main.py` — `git apply --whitespace=fix` prevents corrupt-patch errors from minor whitespace differences
- `executor/main.py` lifespan — `git add -A && git commit` ensures baseline before any `git apply`

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