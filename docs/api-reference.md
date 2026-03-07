# API Reference

Base URL: `http://localhost:9000`

All endpoints accept and return JSON. The `/v1/chat/completions` endpoint is OpenAI-compatible.

---

## Chat (OpenAI-Compatible)

### POST /v1/chat/completions

The main entry point. Cline/Roo Code connects here. Supports `/commands` in the last user message.

**Request:**
```json
{
  "model": "orchestrator",
  "messages": [{"role": "user", "content": "/status"}],
  "stream": false
}
```

**Response:** OpenAI-format `ChatCompletion` object.

**Supported /commands:** See [Commands](#commands) section.

---

### GET /v1/models

Returns the model list. Required by Cline/Roo Code on connect.

```json
{"object": "list", "data": [{"id": "orchestrator", "object": "model"}]}
```

---

## Agents

### POST /v1/agents/spawn

Spawn an agent by role and run it with a task.

```json
{"role": "architect", "task": "Design a rate limiter", "session_id": "my-session"}
```

**Roles:** `architect`, `coder`, `reviewer`, `tester`, `documenter`

**Response:**
```json
{"agent_id": "architect-abc123", "role": "architect", "result": "...", "status": "done"}
```

### POST /v1/agents/debate

Run a multi-round architect vs reviewer debate.

```json
{"topic": "Should we use Redis or Postgres?", "session_id": "s1", "max_rounds": 2}
```

**Response:**
```json
{"final_plan": "...", "consensus": true, "rounds": 1, "transcript": [...]}
```

### GET /v1/agents/status

```json
{"total": 5, "running": 1, "done": 4, "failed": 0}
```

### GET /v1/agents/list

Returns all agents with metadata.

### GET /v1/agents/{agent_id}/logs

Returns metadata and status for a specific agent.

---

## Task Queue (DAG)

### POST /v1/tasks/load

Load a task DAG for a session. Validates for cycles and missing dependencies.

```json
{
  "session_id": "my-session",
  "tasks": [
    {"id": "t1", "role": "coder",  "desc": "Write add function", "deps": []},
    {"id": "t2", "role": "tester", "desc": "Test add function",  "deps": ["t1"]}
  ]
}
```

### POST /v1/tasks/execute

Execute all ready tasks in dependency order. Independent tasks run concurrently (Phase 2.6).

```json
{"session_id": "my-session"}
```

**Response:**
```json
{
  "executed": 2, "complete": 2, "failed": 0,
  "blocked": 0, "remaining": 0,
  "tasks": [{"id": "t1", "status": "complete", "patches_applied": 1, "patches_failed": 0}]
}
```

### GET /v1/tasks/status?session_id=my-session

```json
{"session_id": "my-session", "total": 2, "pending": 0, "complete": 2, ...}
```

---

## Memory

### POST /v1/index

Re-index the project workspace into ChromaDB using AST-aware chunking (Phase 3.1). Python, JS, TS, Go, Rust, Java, C, C++ files are indexed at function/class boundaries. All other supported types use line-based chunking.

```json
{"files_indexed": 42, "chunks": 187, "skipped": 3}
```

### GET /v1/memory/recall?q=rate+limiter

Semantic search over past sessions and failures, with reranking (Phase 2.5).

```json
{"query": "rate limiter", "results": [{"content": "...", "distance": 1.2, "collection": "sessions"}]}
```

### GET /v1/memory/symbol?name=multiply&k=5

**Phase 3.1** — Search the indexed codebase for a function or class by name. Returns AST-enriched chunks with symbol metadata.

```json
{
  "query": "multiply",
  "count": 1,
  "results": [{
    "content": "def multiply(a, b):\n    return a * b\n",
    "metadata": {
      "file": "hello.py",
      "symbol": "multiply",
      "symbol_type": "function",
      "start_line": 4,
      "end_line": 5,
      "language": "python"
    },
    "distance": 0.0,
    "collection": "codebase"
  }]
}
```

**Query params:** `name` (required), `k` (optional, default 5)

### POST /v1/memory/save

Persist a session summary.

```json
{"session_id": "s1", "content": "Built a rate limiter using Redis sorted sets"}
```

---

## Patches

### POST /v1/patches/submit

Submit a unified diff for validation and queuing.

```json
{"diff": "--- a/f.py\n+++ b/f.py\n@@ ...", "agent_id": "coder-x", "task_id": "t1", "session_id": "s1"}
```

### POST /v1/patches/process

Process all pending patches (validate → sandbox → apply).

### POST /v1/patches/test

**Phase 2.2** — Submit a diff, apply it, run pytest, and auto-fix failures up to `MAX_FIX_ATTEMPTS` times.

```json
{
  "diff": "--- a/f.py\n+++ b/f.py\n@@ ...",
  "session_id": "s1",
  "test_pattern": "tests/"
}
```

**Response includes:** `test_passed`, `attempts`, `test_summary` in addition to standard patch fields.

### GET /v1/patches/status

```json
{"total": 3, "pending": 0, "applied": 2, "rejected": 1, "conflict": 0}
```

### GET /v1/patches/list?session_id=s1

---

## Metrics

### GET /v1/metrics

**Phase 2.3** — Return aggregate token counts and latency across all agent calls.

```json
{
  "total_requests": 12,
  "total_tokens_in": 18400,
  "total_tokens_out": 5600,
  "avg_latency_ms": 1840.5,
  "by_role": {
    "coder": {"requests": 8, "tokens_in": 12000, "tokens_out": 4000, "avg_latency_ms": 1900.0},
    "architect": {"requests": 4, "tokens_in": 6400, "tokens_out": 1600, "avg_latency_ms": 1720.0}
  }
}
```

**Query params:** `session_id` (optional) — filter to a single session.

---

## Fine-tune Data

### GET /v1/finetune/stats

**Phase 3.2** — Return stats about collected training examples.

```json
{"records": 42, "size_bytes": 186320, "path": "/app/memory/training_data.jsonl"}
```

### GET /v1/finetune/export?limit=100

**Phase 3.2** — Download training data as JSONL (Alpaca format). Each line: `{instruction, input, output, metadata}`.

**Query params:** `limit` (optional) — cap number of records.

Returns `Content-Type: application/x-ndjson` with `Content-Disposition: attachment`.

### DELETE /v1/finetune/clear

**Phase 3.2** — Delete all collected training records.

```json
{"deleted": 42}
```

---

## Webhook

### POST /v1/webhook/github

**Phase 3.3** — Receive GitHub webhook events. Requires `X-Hub-Signature-256` header signed with `GITHUB_WEBHOOK_SECRET`.

**Supported events:**

| Event | Trigger | Action |
|-------|---------|--------|
| `workflow_run` | CI fails | Coder agent analyzes logs → proposes fix diff → enqueues patch |
| `issues` | Issue opened | Architect decomposes issue → loads task DAG |

**Setup:** In your GitHub repo → Settings → Webhooks → add `http://your-server:9000/v1/webhook/github`, content type `application/json`, set a secret matching `GITHUB_WEBHOOK_SECRET`.

**Response (workflow_run):**
```json
{
  "event": "workflow_run",
  "run_id": 12345,
  "session_id": "ci-fix-12345",
  "diffs_found": 1,
  "enqueued": 1,
  "agent_status": "done"
}
```

**Response (issues):**
```json
{
  "event": "issues",
  "issue_number": 42,
  "session_id": "issue-42",
  "tasks_loaded": 3,
  "plan_preview": "..."
}
```

---

## Skills

### GET /v1/skills/list

List all loaded skills and available commands.

### POST /v1/skills/learn

Extract a reusable skill from a session transcript.

```json
{"session_id": "s1", "transcript": [{"role": "architect", "content": "..."}]}
```

---

## Session Hooks

### POST /v1/session/start

```json
{"session_id": "s1", "task": "build a rate limiter"}
```

Returns relevant past context for the session.

### POST /v1/session/end

```json
{"session_id": "s1", "summary": "Built rate limiter", "transcript": [...], "failures": [...]}
```

**Phase 3.4:** If `failures` are provided and similar failures exist in ChromaDB above the threshold, an anti-pattern skill is automatically extracted and saved.

---

## Health

### GET /health

```json
{"status": "ok", "profile": "laptop", "version": "0.3.0"}
```

---

## Commands Reference

Commands are detected in the last user message sent to `/v1/chat/completions`.

| Command | Args | Description |
|---------|------|-------------|
| `/architect <task>` | required | Generate implementation plan |
| `/debate <topic>` | required | Multi-round architect vs reviewer debate |
| `/review <text>` | required | Review code or a plan |
| `/test <task>` | required | Write tests |
| `/execute` | none | Execute current task queue |
| `/memory <query>` | required | Search past sessions |
| `/learn` | none | Extract skill from session |
| `/status` | none | System health + metrics + training data count |
| `/index` | none | Re-index codebase (AST-aware) |