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

Execute all ready tasks in dependency order.

```json
{"session_id": "my-session"}
```

**Response:**
```json
{"executed": 2, "complete": 2, "failed": 0, "blocked": 0, "remaining": 0}
```

### GET /v1/tasks/status?session_id=my-session

```json
{"session_id": "my-session", "total": 2, "pending": 0, "complete": 2, ...}
```

---

## Memory

### POST /v1/index

Re-index the project workspace into ChromaDB.

```json
{"files_indexed": 42, "chunks": 187, "skipped": 3}
```

### GET /v1/memory/recall?q=rate+limiter

Semantic search over past sessions and failures.

```json
{"query": "rate limiter", "results": [{"content": "...", "distance": 1.2, "collection": "sessions"}]}
```

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

### GET /v1/patches/status

```json
{"total": 3, "pending": 0, "applied": 2, "rejected": 1, "conflict": 0}
```

### GET /v1/patches/list?session_id=s1

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

---

## Health

### GET /health

```json
{"status": "ok", "profile": "laptop", "version": "0.6.0"}
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
| `/status` | none | System health summary |
| `/index` | none | Re-index codebase |