# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.5.1] — 2026-03-12 — Phase 4B.4 TUI Stabilization

### Summary
First complete, zero-crash TUI release. All missing files created, all
unregistered screens wired, all known bugs fixed. No new features — this
version exists solely to make the existing TUI scaffold fully functional.

---

### Added

**`tui/tui/__init__.py`**
- Package init with `__version__ = "0.5.1"`

**`tui/tui/screens/model_config.py`** — new file
- `ModelConfigScreen(client, state, session_id)` — full-screen overlay
  wrapping `ModelPanel`. On `ModelsConfigured` event: calls
  `client.configure_models()`, updates `AppState.session.models`, shows
  brief success flash, pops screen. Esc cancels without applying.

**`tui/tui/screens/command_palette.py`** — new file
- `CommandPaletteScreen(partial, on_execute, command_history)` — floating
  overlay (64 cols, centered). Contains a filter `Input` pre-populated with
  `partial`. `ListView`-style rendering of all 14 registered commands,
  fuzzy prefix-filtered as user types. Arrow keys navigate list. Enter
  executes selected command via injected `on_execute` async callable.
  Arrow-up in empty filter input cycles `command_history` deque (20 items).
  Esc dismisses without executing.
- Full command list: `architect`, `execute`, `review`, `test`, `debate`,
  `memory`, `spawn`, `kill`, `model`, `index`, `status`, `learn`, `end`,
  `help`. `/debate` explicitly noted as opt-in, not triggered by `/architect`.

**`tui/tui/screens/help.py`** — new file
- `HelpScreen` — full-screen keybinding reference. 7 sections: Global,
  Session, Chat, Launcher, Project, Command Palette, Input Bar. Esc closes.

**`tui/tui/services/session_service.py`** — `handle_inline_command()` added
- Executes `/commands` typed in the session input bar directly against the
  orchestrator without opening the command palette.
- Supported inline: `architect`, `execute`, `review`, `test`, `debate`,
  `memory`, `spawn`, `kill`, `index`, `status`, `learn`, `end`.
- Special return values signal the session screen to open overlays:
  `__open_model_config__`, `__open_command_palette__`, `__open_help__`.
- Unknown commands return `__open_command_palette__` so the palette opens
  as a fallback. Never raises — errors returned as strings.

### Changed

**`tui/tui/__init__.py`**
- Version bumped `0.5.0` → `0.5.1`

**`tui/tui/app.py`**
- Screen router extended with three new entries: `"model_config"`,
  `"command_palette"`, `"help"` — previously unregistered, caused
  `ValueError` on `m`, `/`, and `?` keypresses in session screen.
- Factory methods added: `_make_model_config()`, `_make_command_palette()`,
  `_make_help()`.
- All three new screen imports added.

**`tui/tui/screens/session.py`**
- `_cancel_all_tasks()` — added null guard: skips `None` tasks and already-
  done tasks. Fixes task leak when screen unmounts before `_init()` completes.
- `on_input_bar_command_triggered()` — now calls
  `sess_svc.handle_inline_command()` first; only opens command palette for
  unknown commands or `__open_command_palette__` signal. Inline results
  logged to `AppState.session.event_log` as system events.
- `_cmd_history: deque` — 20-item command history shared with
  `CommandPaletteScreen` across invocations.
- `on_key()` — `"model_config"`, `"command_palette"`, `"help"` all route
  correctly now via registered screen names.

**`tui/tui/widgets/agent_pane.py`**
- `compose()` — header `Static` and content `RichLog` now use
  `classes="pane-header"` and `classes="pane-log"` respectively instead of
  id-based selectors. Fixes CSS mismatch where `#pane-header` and
  `#pane-content` selectors in `aicaf.tcss` never matched.
- `_on_flush()` — scroll anchoring added: only calls `scroll_end()` when
  `self._at_bottom` is `True`. `on_scroll()` updates `_at_bottom` by
  comparing `scroll_y` to `virtual_size.height - size.height`.
- `_header_text()` — token count added to right side of header.
- `on_unmount()` — animation task cancel now guards `not task.done()`.

**`tui/tui/widgets/dag_sidebar.py`**
- Removed `DEFAULT_CSS` block entirely. All sidebar styling lives in
  `aicaf.tcss`. The Python-level `DEFAULT_CSS` was redundant and fragile
  (app-level CSS silently overrides widget-level CSS).
- Models section now shows `[m] reconfigure` hint inline.

**`tui/tui/widgets/footer_bar.py`**
- Added missing hint entries for `"new_project"`, `"new_session"`,
  `"help"`, `"model_config"`, `"command_palette"`. Previously these screens
  would show an empty footer bar.

**`tui/tui/widgets/input_bar.py`**
- Command history: `_history: deque[str](maxlen=20)` and `_history_idx`
  added. Arrow-up/down cycle through history when input is focused.
  History is saved on every `Submitted` or `CommandTriggered` event.
  Consecutive identical entries are deduplicated.
- `on_mount()` — auto-focuses the inner `Input` on mount.
- `on_key()` — `prevent_default()` + `stop()` called on Up/Down so
  Textual's default focus-cycling behavior does not interfere.

**`tui/tui/widgets/model_panel.py`**
- `init_val` bug fixed: was using an inverted dict comprehension
  `{v: l for l, v in opts}` which always evaluated `False`, causing the
  current model to never be pre-selected. Replaced with:
  `option_values = [v for _, v in opts]; init_val = cur if cur in option_values else opts[0][1]`

**`tui/tui/aicaf.tcss`**
- `#pane-header` → `.pane-header`, `#pane-content` → `.pane-log` to match
  widget class attributes set in `agent_pane.py`.
- Added `#input-hint`, `#chat-status-bar`, `#logs-header`, `#help-container`,
  `#help-log`, `#cp-filter`, `#cp-list`, `#model-config-container`,
  `#agent-placeholder` selectors.
- Added `Collapsible` and `CollapsibleTitle` styling.
- Added `Input` generic styling (used by new project / new session forms).
- All redundant inline widget `DEFAULT_CSS` now consolidated here.

### Fixed

| # | File | Issue |
|---|---|---|
| 1 | `app.py` | `"model_config"` screen not registered → `ValueError` on `m` key |
| 2 | `app.py` | `"command_palette"` screen not registered → `ValueError` on `/` |
| 3 | `app.py` | `"help"` screen not registered → `ValueError` on `?` key |
| 4 | `session.py` | Task leak: `_cancel_all_tasks()` called on `None` tasks |
| 5 | `model_panel.py` | `init_val` always fell back to first option regardless of stored model |
| 6 | `agent_pane.py` | CSS id mismatch: `#pane-header`/`#pane-content` never matched rendered ids |
| 7 | `dag_sidebar.py` | Redundant `DEFAULT_CSS` conflicting with `aicaf.tcss` |
| 8 | `agent_pane.py` | Auto-scroll yanked viewport when user had scrolled up |
| 9 | `session.py` | `/commands` in input bar always tried to open unregistered palette screen |
| 10 | `input_bar.py` | No command history — arrow-up had no effect |
| 11 | `footer_bar.py` | Missing hint entries for `new_project`, `new_session`, `help`, `model_config`, `command_palette` |

### Notes
- `handle_inline_command()` routes through `POST /v1/chat/completions` for
  agent-spawning commands (`architect`, `review`, `test`) to preserve the
  OpenAI-compatible streaming path. Direct API calls are used for structural
  commands (`execute`, `index`, `status`, `spawn`, `kill`, `learn`, `end`).
- Command history is not persisted to disk in v0.5.1 — it lives in
  `AppState.ui` for the session lifetime only. Disk persistence is part of
  the v0.6.0 session log work.
- `chat.py` scroll anchoring is deferred to v0.6.0 when `ConversationLog`
  replaces the `RichLog` in chat mode. The pattern is identical to what was
  applied to `AgentPane`.
- The `executor_client.py` backend file required no changes for v0.5.1.

---

## [0.5.0] — 2026-03-09 — Phase 4B Complete (Sessions · Streaming · Agent Bus)

### Added

**`orchestrator/session_manager.py`** — new file (Phase 4B.1)
- `SessionState` — Pydantic model for session wire type stored at `session:state:{session_id}` (TTL `SESSION_TTL`, 7 days). Fields: `session_id`, `status`, `created_at`, `updated_at`, `task`, `models`, `agent_ids`, `task_ids`, `metadata`
- `SessionManager.create_session(task, session_id, models, metadata)` — creates a new session, calls `SessionHooks.on_session_start()` for past-context recall, optionally writes role→model assignments to `session:models:{id}` HASH immediately so `RoutingPolicy` can resolve them before the first agent runs
- `SessionManager.get_session(session_id)` — returns `SessionState` or `None` if expired/missing
- `SessionManager.update_session(session_id, **kwargs)` — patches arbitrary allowed fields; list fields (`agent_ids`, `task_ids`) support append-style string updates with automatic deduplication; always refreshes `session:models` TTL on every write
- `SessionManager.configure_models(session_id, models)` — sets or merges role→model assignments; if `session:state` exists, merges and refreshes both key TTLs atomically via pipeline; if not, writes only the HASH key and returns a minimal state
- `SessionManager.register_agent(session_id, agent_id)` — appends `agent_id` to session's `agent_ids` list; no-op if session does not exist
- `SessionManager.register_task(session_id, task_id)` — appends `task_id` to session's `task_ids` list
- `SessionManager.end_session(session_id, summary, transcript, failures)` — marks session as `ended`, delegates to `SessionHooks.on_session_end()`, triggers `cleanup_idle_agents()`; publishes `STATUS/ended` WSEvent to AgentBus so `subscribe_session()` generators exit cleanly; session:state key retained in Redis until TTL expires
- `SessionManager.pause_session(session_id)` / `resume_session(session_id)` — status transitions; `resume_session` raises `ValueError` if called on an ended session
- `SessionManager.list_sessions(status)` — SCAN-based enumeration of all `session:state:*` keys; optional status filter (`active` | `paused` | `ended`); ordered by `created_at` descending
- `_write_state()` — internal pipeline that atomically sets `session:state` and refreshes `session:models` TTL so both keys always share `SESSION_TTL`
- `init_session_manager(redis, agent_mgr, bus)` / `get_session_manager()` singleton

**`orchestrator/agent_bus.py`** — new file (Phase 4B.3)
- `AgentBus` — dual-transport message bus: `asyncio.Queue` per session (in-process, zero overhead) for agent→architect coordination; Redis pub/sub on `bus:session:{session_id}` for orchestrator→WebSocket→TUI fan-out
- `publish(session_id, event)` — writes to both transports; rejects `WSEventType.TOKEN` events at the gate (tokens must never enter the bus); in-process queue full → drops to Redis only; Redis failure logged and swallowed
- `subscribe_architect(session_id)` — async generator; blocks on in-process queue; filters to 6 architect-relevant event types (`work_complete`, `work_failed`, `patch_applied`, `test_result`, `debate_point`, `interrupt`); exits on `None` sentinel
- `subscribe_session(session_id)` — async generator over Redis pub/sub; used exclusively by WebSocket handler; exits on `STATUS/ended` event or Redis connection drop
- `cleanup_session(session_id)` — pops in-process queue and pushes `None` sentinel so any active `subscribe_architect()` generator exits cleanly; called by `SessionManager.end_session()`
- `init_agent_bus(redis)` / `get_agent_bus()` singleton

**`orchestrator/models.py`** — Phase 4B additions
- `AgentMessageRequest` — Pydantic body for `POST /v1/agents/{agent_id}/message`. Fields: `message: str`, `sender: str = "user"`
- `SessionConfigRequest` — Pydantic body for `POST /v1/sessions`. Fields: `task`, `session_id?`, `models?`, `metadata?`
- `WSEventType` — `str` enum: `token`, `work_complete`, `work_failed`, `patch_applied`, `test_result`, `interrupt`, `status`, `debate_point`. Token events are SSE-only and never appear on the bus
- `WSEvent` — Pydantic envelope for all structured bus events: `type`, `session_id`, `agent_id?`, `payload: dict`, `ts: float`

**`orchestrator/agent_manager.py`** — Phase 4B additions
- `Agent.inbox` — `asyncio.Queue(maxsize=256)` for inbound messages (user/architect → agent)
- `Agent.outbox` — `asyncio.Queue(maxsize=1024)` for outbound token chunks (agent → SSE consumers)
- `Agent.to_dict()` now includes `inbox_depth` and `outbox_depth`
- `AgentManager.send_message(agent_id, message)` — pushes to agent inbox; returns `False` cleanly for unknown or terminal agents; returns `False` with error log if inbox full
- `AgentManager.subscribe_stream(agent_id)` — async generator yielding string chunks from agent outbox until `None` sentinel; 30-second per-chunk timeout handles silent-but-running agents
- `AgentManager.has_agent(agent_id)` — `True` if agent is registered; used by SSE endpoint 2s poll
- `AgentManager.get_agents_for_session(session_id)` — returns all agents for a session
- `AgentManager.set_bus(bus)` — late-wire AgentBus after lifespan init
- `AgentManager.__init__` accepts `bus=None`; `init_agent_manager(mem, redis, bus)` updated
- `spawn_and_run()` — registers agent with `SessionManager.register_agent()` after spawn (non-fatal if not initialised); pushes `None` sentinel to outbox on all terminal paths via `_drain_outbox_sentinel()`
- `_run_agent()` — switched to `stream=True`; tokens pushed to `agent.outbox` token-by-token as they arrive; dict fallback for LiteLLM path and backends that ignore `stream=True`; publishes `WORK_COMPLETE` WSEvent to AgentBus after successful model call
- `spawn_and_run()` timeout and exception paths publish `WORK_FAILED` WSEvent with `reason` field (`"timeout"` | `"exception"`)

**`orchestrator/patch_queue.py`** — Phase 4B.3 additions
- `set_bus(bus)` — late-wire AgentBus (same pattern as `set_redis()`)
- `_publish_patch_applied(patch)` — publishes `PATCH_APPLIED` WSEvent to AgentBus after successful live apply; separated from `_apply_patch()` so bus failure never affects patch result; best-effort, failure logged

**New API endpoints (`orchestrator/main.py`)**
- `GET  /v1/sessions` — list all sessions; optional `?status=active|paused|ended` filter; ordered newest first
- `GET  /v1/sessions/{session_id}` — get session state; 404 if not found or TTL expired
- `POST /v1/sessions` — create managed session. Body: `{task, session_id?, models?, metadata?}`. Validates model names against catalog. Returns full `SessionState`
- `POST /v1/sessions/{session_id}/end` — end session; optional `{summary, transcript, failures}`; triggers hooks + agent cleanup; state readable until TTL
- `POST /v1/sessions/{session_id}/pause` — transition to `paused`; 404 if not found
- `POST /v1/sessions/{session_id}/resume` — transition to `active`; 409 if session is ended
- `GET  /v1/agents/{agent_id}/stream` — SSE token stream; polls 2s for agent registration (race-condition safe), streams `agent.outbox` token-by-token, closes with `data: [DONE]`
- `WS   /ws/session/{session_id}` — full-duplex WebSocket; yields structured `WSEvent` JSON; heartbeat ping every `WS_HEARTBEAT_INTERVAL` seconds; delegates to `AgentBus.subscribe_session()`
- `POST /v1/agents/{agent_id}/message` — deliver message to agent inbox; 404 if not found; 409 if terminal

**New test files**
- `tests/unit/test_session_manager.py` — 30 tests; `FakeRedis` in-memory substitute; covers full session lifecycle, TTL sync, configure_models, register, list ordering
- `tests/unit/test_streaming.py` — 12 tests; token-by-token streaming, full content assembly, Ollama/vLLM chunk shapes, dict fallback, outbox-full handling, sentinel on completion/failure
- `tests/unit/test_agent_bus.py` — 22 tests; `FakePubSub`; covers both transports, TOKEN rejection, full-queue fallback, Redis failure handling, architect filtering, cleanup sentinel, subscribe_session, patch_queue integration, session_manager integration
- `tests/integration/test_phase4b.py` — 25 integration smoke tests covering 4B.1 session CRUD, 4B.2 streaming endpoints, 4B.3 bus observable side effects

### Changed

**`orchestrator/main.py`**
- Version bumped to `0.5.0`
- Lifespan wiring order: `AgentBus` initialised before `SessionManager`; bus passed to `init_session_manager()`, `mgr.set_bus()`, `patch_queue.set_bus()`
- `POST /v1/session/configure` — delegates to `SessionManager.configure_models()` instead of raw `redis.hset`; uses `SESSION_TTL` (7 days) instead of deprecated `SESSION_MODELS_TTL` (24h); atomically refreshes both key TTLs for existing sessions
- `POST /v1/tasks/load` — calls `SessionManager.register_task()` for each loaded task (non-fatal if session not managed)
- `_session_event_loop()` — stub replaced with real `AgentBus.subscribe_session()` delegation
- `/status` command now includes active session count
- Legacy `POST /v1/session/start` and `POST /v1/session/end` retained for backwards compatibility

**`orchestrator/config.py`**
- `SESSION_TTL = 604800` (7 days) added — canonical TTL for both `session:state` and `session:models` keys
- `WS_HEARTBEAT_INTERVAL = 30` added — WebSocket keepalive ping interval (seconds)
- `BUS_EVENT_TTL = 3600` added — supplementary bus event log TTL
- `AICAF_URL = "http://localhost:9000"` added — default TUI → orchestrator URL
- `SESSION_MODELS_TTL` marked deprecated — retained to avoid breaking callers; removed in Phase 5

**`orchestrator/requirements.txt`**
- Added `sse-starlette==2.1.0`
- Added `websockets==13.0`

**`.github/workflows/ci.yml`**
- Added `sse-starlette==2.1.0` and `websockets==13.0` to unit test pip install list (required by `test_streaming.py` and `test_agent_bus.py`)

**`README.md`**
- Phase badge updated to `4B Complete`
- Old hand-drawn ASCII architecture diagram replaced with three Mermaid diagrams: component map (all 20+ modules + infrastructure), end-to-end sequence diagram (`/architect` + `/execute` full flow), streaming token path diagram (asyncio.Queue→SSE vs Redis pub/sub→WebSocket split)
- New "Session & Streaming API" section with curl/JS examples
- Roadmap table updated through Phase 5
- Project structure updated with all new files (`session_manager.py`, `agent_bus.py`, `routing_policy.py`, `model_registry.py`, `gateway.py`)
- Unit test count updated to 475+

### Notes
- `Agent.inbox` and `Agent.outbox` are in-process `asyncio.Queue` objects — they exist only for the lifetime of the orchestrator process. Session state is Redis-backed and survives restart; the queues do not. In-flight inbox messages are lost on crash — acceptable for single-developer use; addressed in Phase 5 (NATS) if needed
- `subscribe_stream()` supports one consumer per agent in 4B. Multi-consumer fan-out is handled by `AgentBus` Redis pub/sub in 4B.3
- `WSEventType.TOKEN` is reserved and intentionally never published to the bus — tokens flow exclusively through the SSE path to avoid per-token Redis overhead
- Phase 5 migration path: replace `asyncio.Queue` in `agent_bus.py` with NATS JetStream for multi-node support — the `AgentBus` interface (`publish`, `subscribe_architect`, `subscribe_session`, `cleanup_session`) is stable

### Config vars added
| Variable | Default | Description |
|---|---|---|
| `SESSION_TTL` | `604800` | Redis TTL for `session:state` and `session:models` keys (7 days) |
| `WS_HEARTBEAT_INTERVAL` | `30` | WebSocket keepalive ping interval (seconds) |
| `BUS_EVENT_TTL` | `3600` | Supplementary bus event log TTL (seconds) |
| `AICAF_URL` | `http://localhost:9000` | Default TUI → orchestrator URL |

---

## [0.4.3] — 2026-03-08 — Phase 4A.4 LiteLLM Gateway (optional)

### Added

**`orchestrator/gateway.py`** — new file
- `gateway_dispatch(messages, model, stream, ...)` — thin async wrapper around `litellm.acompletion()`
- Handles provider normalisation, cost tracking, and retries via LiteLLM
- Streaming path via `_stream_litellm()` yields SSE chunks compatible with existing SSE consumers
- Guards: raises `RuntimeError` if called with `USE_LITELLM=false`; raises `ImportError` with install instructions if `litellm` package is not installed

**`orchestrator/router.py`**
- `dispatch()` checks `config.USE_LITELLM` at entry. If `true`, imports `gateway.gateway_dispatch` and routes through it. If `false` (default), zero change to existing Ollama/vLLM path

### Notes
- `USE_LITELLM=false` by default — no behaviour change for existing deployments
- `litellm` is intentionally not in `orchestrator/requirements.txt`. Install manually when enabling: `pip install litellm>=1.40.0`
- LiteLLM sometimes lags behind provider API updates. The direct Ollama/vLLM router path is preserved and remains the default

---

## [0.4.2] — 2026-03-08 — Phase 4A.3 Validation & Hardening

### Added

**`docs/hardware-requirements.md`** — new file
- Full VRAM requirements per profile (laptop / gpu-shared / gpu)
- `PROFILE=auto` detection logic documented with example log output
- Per-session model override usage example
- Minimum system requirements table
- Note on VRAM estimate accuracy (quantization, TP, KV cache)

**`orchestrator/file_watcher.py`** — Phase 4A.3 additions
- 500ms debounce on raw file events via per-path `asyncio.TimerHandle` coalescing
- `publish_codebase_updated()` — publishes `{"event": "codebase_updated"}` to `filewatch:events` after git commit; guarantees index always reflects a committed state
- `_process_event()` extracted from `_event_worker()` for testability
- `stop()` now cancels all pending debounce handles before shutting down observer

**`orchestrator/session_hooks.py`** — Phase 4A.3 additions
- `_parse_confidence(text)` — parses `CONFIDENCE: 0.0–1.0` from model responses; clamps to valid range; defaults to `0.8` when missing or unparseable
- `extract_skills()` — prompt updated to request `CONFIDENCE` rating; stored in `save_skill()` metadata
- `_mine_failure_patterns()` — prompt updated to request `CONFIDENCE` rating; stored in antipattern metadata
- Low-confidence items saved to ChromaDB regardless — filtered at read time in `context_manager` (threshold 0.6)

**`executor/main.py`** — Phase 4A.3 additions
- `_apply_execution_limits()` — `resource.setrlimit` guards as `preexec_fn`: `RLIMIT_CPU` (60s), `RLIMIT_FSIZE` (500MB), `RLIMIT_NOFILE` (256 fds)
- Applied to both `/execute` and `/apply-patch` endpoints
- `setrlimit` failure logged as WARNING, does not crash executor

**`orchestrator/routing_policy.py`** — Phase 4A.3 refactor (extracted from router, formalised)
- `RoutingPolicy` class owns all endpoint/model resolution; `router.py` becomes a thin dispatcher
- `_profile_endpoint(role)` / `_profile_model(role)` — profile default logic
- `_endpoint_for_model(model_name)` / `_backend_type(url)` — model→URL mapping

### Changed
- Orchestrator version bumped to `0.4.2`
- `PROFILE=auto` detection added to `config.py` via `_detect_profile()`; decision logged at `WARNING` level

### Notes
- `resource.setrlimit` is Linux-only; macOS dev outside Docker logs a warning and proceeds
- File watcher debounce window is 500ms (`DEBOUNCE_SECONDS` constant)

---

## [0.4.1] — 2026-03-08 — Phase 4A.2 Dynamic Model Assignment

### Added

**`orchestrator/routing_policy.py`** — new file
- `RoutingPolicy` class — owns all endpoint/model resolution logic extracted from `router.py`
- `resolve(role, session_id)` — two-tier resolution: Redis session override → profile default
- `get_session_models(session_id)` — returns stored role→model map for a session
- `_profile_endpoint(role)` / `_profile_model(role)` — profile default logic (replaces `config.ROLE_ENDPOINTS` / `config.ROLE_MODELS`)
- `_endpoint_for_model(model_name)` — maps a catalog model name to its service URL
- `_backend_type(url)` — returns `"ollama"` or `"vllm"` based on URL heuristic
- `init_routing_policy(redis)` / `get_routing_policy()` singleton pattern
- `set_redis(redis)` — wire Redis after construction (matches PatchQueue pattern)

**Task leasing (`orchestrator/task_queue.py`)**
- `_acquire_task_lease(session_id, task_id, worker_id)` — Redis SETNX with TTL `config.TASK_LEASE_TTL` (600s default)
- `_release_task_lease(session_id, task_id)` — deletes lease key in `finally` block — always released, even on exception
- `_run_single_task()` — acquires lease before execution; skips with `status: "skipped"` if lease already held
- `load_plan()` — clears existing lease keys when reloading a session plan
- Redis key format: `task:{session_id}:{task_id}:lease`

**New API endpoints**
- `POST /v1/session/configure` — store role→model map in Redis. Validates each model against catalog. Returns `{session_id, models, configured, ttl_seconds}`
- `GET /v1/session/models?session_id=X` — return current role→model overrides for a session

### Changed

**`orchestrator/config.py`**
- Removed `ROLE_ENDPOINTS` and `ROLE_MODELS` dicts — resolution logic moved to `routing_policy.py`
- Added `SHARED_MODEL` env var (used by gpu-shared profile)
- Added `SESSION_MODELS_TTL = 86400` (24h TTL for session model assignments, deprecated in 0.5.0)
- Added `TASK_LEASE_TTL = 600` (10min TTL for task lease keys)
- Added `USE_LITELLM = false` (flag-gated gateway, Phase 4A.4)
- Added `ALL_ROLES` list — canonical role list used across model_registry, routing_policy, TUI

**`orchestrator/router.py`**
- `resolve_endpoint()` now delegates to `RoutingPolicy.resolve()` — router owns no selection logic
- `dispatch()` signature gains `session_id: str = "default"`
- `set_policy(policy)` function added — called from `main.py` lifespan

**`orchestrator/agent_manager.py`**
- `AgentManager.__init__` accepts `redis=None`
- `Agent.model` field added — populated by `_run_agent()` before model call
- `_run_agent()` resolves model via `RoutingPolicy`, passes `model=` to `build_prompt()`, passes `session_id=` to `router.dispatch()`
- `to_dict()` now includes `model` field

**`orchestrator/main.py`**
- `init_routing_policy()` called in lifespan; `router.set_policy(policy)` called; `init_agent_manager()` called with `redis=task_queue._redis`

### Config vars added
| Variable | Default | Description |
|---|---|---|
| `SHARED_MODEL` | `Qwen/Qwen3-Coder-Next-80B-A3B-Instruct` | Model for gpu-shared profile |
| `SESSION_MODELS_TTL` | `86400` | Redis TTL for session model assignments — deprecated in v0.5.0 |
| `TASK_LEASE_TTL` | `600` | Redis TTL for task lease keys (seconds) |
| `USE_LITELLM` | `false` | Enable LiteLLM gateway (Phase 4A.4) |

---

## [0.4.0] — 2026-03-08 — Phase 4A.1 Model Registry

### Added

**`orchestrator/model_registry.py`** — new file
- `MODEL_CATALOG` — 10 model entries: Ollama laptop models (qwen2.5-coder 7B/32B, qwen3 8B/14B/32B, nomic-embed-text) and vLLM GPU models (Qwen3-Coder-Next-80B, Qwen3.5-35B, QwQ-32B, Qwen3-Embedding-0.6B)
- `ROLE_TAG_MAP` — role-affinity mapping; tester role accepts coder-tagged models
- `ModelRegistry.detect_available()` — queries Ollama `/api/tags` and vLLM `/v1/models` at startup; endpoint failures logged as warnings, never crash startup
- `ModelRegistry.get_models_for_role(role)` — filters catalog by role affinity; on-disk models sorted first
- `ModelRegistry.get_context_length(model)` — authoritative context window per model; used by `context_manager`; falls back to `DEFAULT_CONTEXT_LENGTH` (32768) for unknown models
- `ModelRegistry.catalog_with_status()` — full catalog annotated with `on_disk` flag
- `ModelRegistry.pull_model(name)` — non-streaming Ollama pull; refreshes on-disk cache; rejects vLLM-only models
- `ModelRegistry.close()` — closes internal HTTP client on shutdown
- `init_model_registry()` / `get_model_registry()` singleton

**`orchestrator/context_manager.py`** — Phase 4A.1 additions
- `build_prompt()` accepts optional `model: str = ""` parameter
- `_resolve_token_budget(model)` — queries `model_registry.get_context_length(model)`; falls back to `MAX_CONTEXT_TOKENS`; fully backwards-compatible
- Antipattern confidence filtering: items with `confidence < 0.6` excluded from "Known Pitfalls" injection; missing confidence field defaults to 1.0

**New API endpoints**
- `GET /v1/models/catalog` — full catalog with `on_disk`, `context_length`, `vram_approx_gb`, `backend`
- `GET /v1/models/for-role?role={role}` — filtered catalog for TUI role selectors
- `POST /v1/models/pull` — pull Ollama model; rejects 409 if any agent currently running
- `POST /v1/models/refresh` — re-run endpoint detection without restart

### Changed
- Orchestrator version bumped to `0.4.0`
- `lifespan()` initialises `ModelRegistry` and calls `detect_available()` after skill_loader; registry closed on shutdown
- `/status` command includes `Models: {on_disk}/{total} on disk` line

### Notes
- `vram_approx_gb` is indicative only — actual usage depends on quantisation, TP, KV cache
- Tags are role-affinity hints, not objective capability claims
- vLLM model pulls not supported via this endpoint; loaded by vLLM container from HuggingFace at startup

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
- `_embed_batch()` now uses `asyncio.gather()` — parallel embedding instead of sequential loop
- LRU embed cache (`_LRUEmbedCache`) replaces plain dict — `OrderedDict`-based eviction at `EMBED_CACHE_MAX_SIZE` prevents unbounded RAM growth
- `connect()` URL parsed via `urllib.parse.urlparse` — robust against HTTPS URLs, custom paths, and missing ports
- `index_codebase()` now performs incremental indexing — stores `file_hash` in chunk metadata and skips re-embedding unchanged files; second call on unchanged workspace completes in <1s
- `record_failure()` uses content hash as ChromaDB doc ID — identical failures deduplicated automatically via upsert

**Router**
- Non-streaming `dispatch()` now wrapped in `asyncio.wait_for(MODEL_CALL_TIMEOUT)` — stalled endpoint raises `TimeoutError` instead of hanging indefinitely

**Executor Client**
- `asyncio.Semaphore(MAX_EXECUTOR_CONCURRENCY)` added to `apply_patch()` and `run_tests()` — prevents executor saturation under parallel agents

**Docker**
- `executor` service annotated with `seccomp:unconfined` — documents intent to add custom seccomp profile in Phase 5

### Added
- `POST /v1/agents/cleanup` endpoint — trigger idle agent pruning on demand
- `index_codebase` response now includes `files_unchanged` count
- `/status` command output now includes patch queue depth limit, embed cache size, and executor concurrency slots
- 30 new unit tests covering all Phase 3.5 fixes
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
- Orchestrator version bumped to `0.3.0`

---

## [0.2.0] — 2026-03-07 — Phase 2 Complete

### Added

**Step 2.1 — Auto-Patch Application**
- `utils.extract_diffs_from_result(text)` — regex extraction of diff blocks from agent output
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
- `metrics.py` — `record_request()`, `get_summary()`, `get_session_summary()`
- `metrics.parse_usage()` — extracts token counts from both Ollama and vLLM response shapes
- `agent_manager._run_agent()` — hooks `metrics.record_request()` before/after model call
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
- `executor/main.py` — `git apply --whitespace=fix` prevents corrupt-patch errors
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