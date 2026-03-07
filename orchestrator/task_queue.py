"""
task_queue.py — Redis-backed dependency-aware task DAG scheduler.

Task structure:
    {id, role, desc, deps: [], status: pending|running|complete|failed}

Execution rules:
    - A task is only eligible when ALL its deps have status=complete
    - Tasks with no deps are immediately eligible
    - Topological order is enforced — no task runs before its dependencies
    - If a task fails, all tasks that depend on it (transitively) are marked blocked

Redis key layout:
    tasks:{session_id}:{task_id}  →  JSON task dict
    tasklist:{session_id}         →  Redis list of task_ids (insertion order)
"""

import json
import logging
from typing import Optional

import redis.asyncio as aioredis

import config
from agent_manager import AgentManager

log = logging.getLogger("task_queue")


class TaskQueue:
    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self):
        self._redis = await aioredis.from_url(
            config.REDIS_URL, encoding="utf-8", decode_responses=True
        )
        await self._redis.ping()
        log.info("Redis connected: %s", config.REDIS_URL)

    async def close(self):
        if self._redis:
            await self._redis.aclose()

    # ── Task CRUD ─────────────────────────────────────────────────────────────

    def _task_key(self, session_id: str, task_id: str) -> str:
        return f"tasks:{session_id}:{task_id}"

    def _list_key(self, session_id: str) -> str:
        return f"tasklist:{session_id}"

    async def _save_task(self, session_id: str, task: dict) -> None:
        key = self._task_key(session_id, task["id"])
        await self._redis.set(key, json.dumps(task))

    async def _load_task(self, session_id: str, task_id: str) -> Optional[dict]:
        key = self._task_key(session_id, task_id)
        raw = await self._redis.get(key)
        return json.loads(raw) if raw else None

    async def _all_task_ids(self, session_id: str) -> list[str]:
        return await self._redis.lrange(self._list_key(session_id), 0, -1)

    async def _all_tasks(self, session_id: str) -> list[dict]:
        ids = await self._all_task_ids(session_id)
        tasks = []
        for tid in ids:
            t = await self._load_task(session_id, tid)
            if t:
                tasks.append(t)
        return tasks

    # ── Load plan ─────────────────────────────────────────────────────────────

    async def load_plan(self, session_id: str, tasks: list[dict]) -> dict:
        """
        Load a validated task list into Redis for a session.
        Clears any existing tasks for this session first.
        Returns a summary.
        """
        # Clear existing tasks
        old_ids = await self._all_task_ids(session_id)
        for tid in old_ids:
            await self._redis.delete(self._task_key(session_id, tid))
        await self._redis.delete(self._list_key(session_id))

        # Validate DAG (no cycles)
        _validate_dag(tasks)

        # Store each task
        for task in tasks:
            task["status"] = "pending"
            await self._save_task(session_id, task)
            await self._redis.rpush(self._list_key(session_id), task["id"])

        log.info("loaded %d tasks for session %s", len(tasks), session_id)
        return {"session_id": session_id, "tasks_loaded": len(tasks)}

    # ── Scheduling ────────────────────────────────────────────────────────────

    async def get_ready_tasks(self, session_id: str) -> list[dict]:
        """Return tasks whose deps are all complete and status is pending."""
        tasks   = await self._all_tasks(session_id)
        done    = {t["id"] for t in tasks if t["status"] == "complete"}
        return [
            t for t in tasks
            if t["status"] == "pending"
            and all(dep in done for dep in t.get("deps", []))
        ]

    async def get_session_status(self, session_id: str) -> dict:
        tasks = await self._all_tasks(session_id)
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return {
            "session_id": session_id,
            "total":      len(tasks),
            "pending":    counts.get("pending",  0),
            "running":    counts.get("running",  0),
            "complete":   counts.get("complete", 0),
            "failed":     counts.get("failed",   0),
            "blocked":    counts.get("blocked",  0),
            "tasks":      tasks,
        }

    async def update_status(
        self, session_id: str, task_id: str, status: str, result: str = ""
    ) -> None:
        task = await self._load_task(session_id, task_id)
        if not task:
            log.warning("update_status: task not found %s/%s", session_id, task_id)
            return
        task["status"] = status
        if result:
            task["result"] = result
        await self._save_task(session_id, task)

        # If failed, mark all transitive dependents as blocked
        if status == "failed":
            await self._propagate_blocked(session_id, task_id)

    async def _propagate_blocked(self, session_id: str, failed_id: str) -> None:
        """Mark all tasks that (transitively) depend on failed_id as blocked."""
        tasks = await self._all_tasks(session_id)
        blocked = {failed_id}
        changed = True
        while changed:
            changed = False
            for t in tasks:
                if t["status"] in ("pending", "running") and t["id"] not in blocked:
                    if any(dep in blocked for dep in t.get("deps", [])):
                        t["status"] = "blocked"
                        await self._save_task(session_id, t)
                        blocked.add(t["id"])
                        changed = True
                        log.info("blocked task %s (depends on failed %s)", t["id"], failed_id)

    # ── Execute plan ──────────────────────────────────────────────────────────

    async def execute_plan(
        self, session_id: str, agent_mgr: AgentManager
    ) -> dict:
        """
        Execute all ready tasks sequentially until the plan is complete or stuck.
        Returns a summary of what happened.
        """
        executed = []
        while True:
            ready = await self.get_ready_tasks(session_id)
            if not ready:
                break

            for task in ready:
                log.info("executing task %s (%s): %s",
                         task["id"], task["role"], task["desc"][:80])
                await self.update_status(session_id, task["id"], "running")

                result = await agent_mgr.spawn_and_run(
                    role=task["role"],
                    task=task["desc"],
                    session_id=session_id,
                )

                if result["status"] in ("done",):
                    await self.update_status(
                        session_id, task["id"], "complete",
                        result=result.get("result", "")
                    )
                    executed.append({**task, "status": "complete"})
                else:
                    await self.update_status(
                        session_id, task["id"], "failed",
                        result=result.get("error", "unknown error")
                    )
                    executed.append({**task, "status": "failed"})

        final = await self.get_session_status(session_id)
        return {
            "executed":  len(executed),
            "complete":  final["complete"],
            "failed":    final["failed"],
            "blocked":   final["blocked"],
            "remaining": final["pending"],
            "tasks":     executed,
        }


# ── DAG validation ────────────────────────────────────────────────────────────

def _validate_dag(tasks: list[dict]) -> None:
    """Raise ValueError if the task list contains cycles or missing dep refs."""
    ids = {t["id"] for t in tasks}
    for t in tasks:
        for dep in t.get("deps", []):
            if dep not in ids:
                raise ValueError(
                    f"Task {t['id']!r} depends on {dep!r} which doesn't exist"
                )
    # Kahn's algorithm cycle detection
    in_degree = {t["id"]: 0 for t in tasks}
    for t in tasks:
        for dep in t.get("deps", []):
            in_degree[t["id"]] += 1
    queue  = [tid for tid, deg in in_degree.items() if deg == 0]
    visited = 0
    adj: dict[str, list[str]] = {t["id"]: [] for t in tasks}
    for t in tasks:
        for dep in t.get("deps", []):
            adj[dep].append(t["id"])
    while queue:
        node = queue.pop()
        visited += 1
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    if visited != len(tasks):
        raise ValueError("Task DAG contains a cycle")


# ── Singleton ─────────────────────────────────────────────────────────────────

task_queue = TaskQueue()