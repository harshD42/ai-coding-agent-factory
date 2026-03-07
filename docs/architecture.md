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
├── main.py               FastAPI app, endpoint definitions, lifespan wiring
├── config.py             All env vars, ROLE_ENDPOINTS, FALLBACK_ORDER
├── models.py             Pydantic schemas (ChatCompletionRequest/Response)
├── router.py             Health-aware dispatch to model backends
├── agent_manager.py      Spawn/track/kill agents, task decomposition, watchdog
├── context_manager.py    5-tier priority context building, token budgeting
├── memory_manager.py     ChromaDB client, embedding, 4 collections
├── patch_queue.py        Diff validation, conflict detection, git apply
├── task_queue.py         Redis-backed DAG scheduler, topological execution
├── debate_engine.py      Architect vs Reviewer multi-round debate
├── skill_loader.py       Markdown skill files → agent system prompts
├── session_hooks.py      on_start/on_end/on_failure, skill extraction
├── command_parser.py     /command detection from chat messages
├── executor_client.py    HTTP client wrapper for executor container
├── utils.py              Token counting, CRLF normalization, diff helpers
└── metrics.py            (Phase 2) Token counting, request timing
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
          → memory_manager.search_codebase(task) → relevant file chunks (P2)
          → memory_manager.recall(task) → past sessions + failures (P4)
          → _trim_conversation(history, budget) → (P3 + P5)
      → router.dispatch(role="architect", messages=context)
      → return plan text
  → wrap in _make_response() → OpenAI format
```

### /execute Command (Task DAG)

```
/execute
  → task_queue.execute_plan(session_id, agent_mgr)
      → get_ready_tasks() — tasks where all deps are complete
      → for each ready task:
          → agent_manager.spawn_and_run(role=task.role, task=task.desc)
          → update_status(task_id, "complete"|"failed")
          → if failed: _propagate_blocked() — mark dependents as blocked
          → loop until no more ready tasks
  → return summary
```

---

## Memory Architecture

### ChromaDB Collections

| Collection | Contents | Written by | Read by |
|------------|----------|------------|---------|
| `sessions` | Session summaries, decisions | `session_hooks.on_session_end()` | `memory_manager.recall()` |
| `codebase` | File chunks from workspace | `memory_manager.index_codebase()` | `context_manager.build_prompt()` |
| `skills` | Extracted reusable patterns | `session_hooks.extract_skills()` | `skill_loader.find_relevant_skills()` |
| `failures` | Failed patches + errors | `memory_manager.record_failure()` | `memory_manager.recall()` |

### Redis Keys

| Key Pattern | Contents | TTL |
|-------------|----------|-----|
| `tasks:{session_id}:{task_id}` | Task JSON (status, result, deps) | None |
| `tasklist:{session_id}` | Ordered list of task IDs | None |

---

## Context Priority System (5 Tiers)

When building a prompt, the context manager fills the window in priority order:

```
P1 [never cut]:  Agent system prompt + current task description
P2 [never cut]:  Relevant codebase chunks (embedding search, top-4)
P3 [cut last]:   Recent conversation messages (last 6 verbatim)
P4 [cut second]: Past session memories + failure records (top-3)
P5 [cut first]:  Older conversation turns (summarized)

Budget: MAX_CONTEXT_TOKENS (24000) - RESPONSE_BUDGET (2048) = 21952 tokens available
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