# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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