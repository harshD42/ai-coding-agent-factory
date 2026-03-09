"""
task_queue.py — Redis-backed dependency-aware task DAG scheduler.

Task structure:
    {id, role, desc, deps: [], status: pending|running|complete|failed}

Execution rules:
    - A task is only eligible when ALL its deps have status=complete
    - Tasks with no deps are immediately eligible
    - Topological order is enforced
    - If a task fails, all transitive dependents are marked blocked
    - Independent ready tasks run concurrently (Step 2.6, capped by MAX_PARALLEL_AGENTS)

Phase 4A.2 additions:
    - Task leasing via Redis SETNX — prevents duplicate execution when the
      orchestrator restarts mid-session with tasks already in-flight.
      Key: task:{session_id}:{task_id}:lease  TTL: config.TASK_LEASE_TTL

Redis key layout:
    tasks:{session_id}:{task_id}            →  JSON task dict
    tasklist:{session_id}                   →  Redis list of task_ids
    task:{session_id}:{task_id}:lease       →  worker_id string (SETNX + TTL)
"""

import asyncio
import json
import logging
import uuid
from typing import Optional

import redis.asyncio as aioredis

import config
from agent_manager import AgentManager
from utils import extract_diffs_from_result

log = logging.getLogger("task_queue")

_PATCH_ROLES = {"coder", "tester"}


class TaskQueue:
    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._patch_queue = None

    def set_patch_queue(self, pq) -> None:
        """Wire in the PatchQueue singleton after both objects are created."""
        self._patch_queue = pq

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

    def _lease_key(self, session_id: str, task_id: str) -> str:
        return f"task:{session_id}:{task_id}:lease"

    async def _save_task(self, session_id: str, task: dict) -> None:
        await self._redis.set(self._task_key(session_id, task["id"]), json.dumps(task))

    async def _load_task(self, session_id: str, task_id: str) -> Optional[dict]:
        raw = await self._redis.get(self._task_key(session_id, task_id))
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

    # ── Phase 4A.2: Task leasing ──────────────────────────────────────────────

    async def _acquire_task_lease(
        self, session_id: str, task_id: str, worker_id: str
    ) -> bool:
        """
        Acquire an exclusive lease on a task using Redis SETNX.
        Returns True if this worker now owns the task.
        Returns False if another worker already holds the lease.

        TTL is config.TASK_LEASE_TTL (default 600s). If the orchestrator
        crashes mid-task, the lease expires and the task becomes retryable.
        """
        key    = self._lease_key(session_id, task_id)
        result = await self._redis.set(
            key, worker_id, nx=True, ex=config.TASK_LEASE_TTL
        )
        return result is True

    async def _release_task_lease(self, session_id: str, task_id: str) -> None:
        """Release the lease on task completion or failure."""
        await self._redis.delete(self._lease_key(session_id, task_id))

    # ── Load plan ─────────────────────────────────────────────────────────────

    async def load_plan(self, session_id: str, tasks: list[dict]) -> dict:
        """Load a validated task list into Redis. Clears any existing tasks first."""
        old_ids = await self._all_task_ids(session_id)
        for tid in old_ids:
            await self._redis.delete(self._task_key(session_id, tid))
            await self._redis.delete(self._lease_key(session_id, tid))
        await self._redis.delete(self._list_key(session_id))

        _validate_dag(tasks)

        for task in tasks:
            task["status"] = "pending"
            await self._save_task(session_id, task)
            await self._redis.rpush(self._list_key(session_id), task["id"])

        log.info("loaded %d tasks for session %s", len(tasks), session_id)
        return {"session_id": session_id, "tasks_loaded": len(tasks)}

    # ── Scheduling ────────────────────────────────────────────────────────────

    async def get_ready_tasks(self, session_id: str) -> list[dict]:
        """Return tasks whose deps are all complete and status is pending."""
        tasks = await self._all_tasks(session_id)
        done  = {t["id"] for t in tasks if t["status"] == "complete"}
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
        if status == "failed":
            await self._propagate_blocked(session_id, task_id)

    async def _propagate_blocked(self, session_id: str, failed_id: str) -> None:
        """Mark all tasks that (transitively) depend on failed_id as blocked."""
        tasks   = await self._all_tasks(session_id)
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

    # ── Auto-patch helper ─────────────────────────────────────────────────────

    async def _auto_apply_patches(
        self, session_id: str, task: dict, output: str
    ) -> list[dict]:
        """Extract diffs from output and enqueue each via patch_queue."""
        if self._patch_queue is None:
            return []
        if task.get("role") not in _PATCH_ROLES:
            return []
        diffs = extract_diffs_from_result(output)
        if not diffs:
            return []
        log.info("task %s: found %d diff(s), auto-enqueueing", task["id"], len(diffs))
        results = []
        for i, diff in enumerate(diffs, 1):
            try:
                enq = await self._patch_queue.enqueue(
                    diff=diff,
                    agent_id=task.get("id", "auto"),
                    task_id=task.get("id", "auto"),
                    session_id=session_id,
                    description=f"Auto-patch from task {task['id']} ({task.get('role')})",
                )
                result_dict = enq.to_dict() if hasattr(enq, "to_dict") else enq
                log.info("task %s diff %d/%d enqueued: %s",
                         task["id"], i, len(diffs), result_dict.get("patch_id", "?"))
                results.append(result_dict)
            except Exception as exc:
                log.error("task %s diff %d/%d enqueue failed: %s",
                          task["id"], i, len(diffs), exc)
                results.append({"error": str(exc)})
        return results

    # ── Execute plan ──────────────────────────────────────────────────────────

    async def execute_plan(self, session_id: str, agent_mgr: AgentManager) -> dict:
        """
        Execute all ready tasks until the plan is complete or stuck.
        Independent ready tasks run concurrently (≤ MAX_PARALLEL_AGENTS).
        """
        executed = []
        while True:
            ready = await self.get_ready_tasks(session_id)
            if not ready:
                break
            batch = ready[: config.MAX_PARALLEL_AGENTS]
            for task in batch:
                await self.update_status(session_id, task["id"], "running")
            task_coros = [
                self._run_single_task(session_id, task, agent_mgr)
                for task in batch
            ]
            batch_results = await asyncio.gather(*task_coros, return_exceptions=True)
            for task, res in zip(batch, batch_results):
                if isinstance(res, Exception):
                    log.error("unexpected error in task %s: %s", task["id"], res)
                    await self.update_status(
                        session_id, task["id"], "failed", result=str(res)
                    )
                    executed.append({**task, "status": "failed"})
                else:
                    executed.append(res)

        final = await self.get_session_status(session_id)
        return {
            "executed":  len(executed),
            "complete":  final["complete"],
            "failed":    final["failed"],
            "blocked":   final["blocked"],
            "remaining": final["pending"],
            "tasks":     executed,
        }

    async def _run_single_task(
        self, session_id: str, task: dict, agent_mgr: AgentManager
    ) -> dict:
        """
        Run one task: acquire lease → spawn agent → auto-apply patches
        → release lease → update status.

        Phase 4A.2: lease acquisition prevents duplicate execution when the
        orchestrator restarts mid-session. If another worker already holds
        the lease for this task, we skip it cleanly.
        """
        worker_id = uuid.uuid4().hex[:12]

        # Acquire lease — skip if another worker beat us to it
        if not await self._acquire_task_lease(session_id, task["id"], worker_id):
            log.warning(
                "task %s already leased by another worker, skipping", task["id"]
            )
            return {**task, "status": "skipped", "reason": "lease_held"}

        log.info("executing task %s (%s): %s",
                 task["id"], task["role"], task["desc"][:80])

        try:
            result = await agent_mgr.spawn_and_run(
                role=task["role"],
                task=task["desc"],
                session_id=session_id,
            )
            output        = result.get("result", "") or ""
            patch_results = await self._auto_apply_patches(session_id, task, output)
            patches_ok    = sum(1 for r in patch_results if "error" not in r)
            patches_fail  = len(patch_results) - patches_ok

            if result["status"] == "done":
                await self.update_status(session_id, task["id"], "complete", result=output)
                return {
                    **task,
                    "status":          "complete",
                    "patches_applied": patches_ok,
                    "patches_failed":  patches_fail,
                }
            else:
                await self.update_status(
                    session_id, task["id"], "failed",
                    result=result.get("error", "unknown error"),
                )
                return {
                    **task,
                    "status":          "failed",
                    "patches_applied": patches_ok,
                    "patches_failed":  patches_fail,
                }
        finally:
            # Always release lease — even on exception
            await self._release_task_lease(session_id, task["id"])


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
    in_degree = {t["id"]: 0 for t in tasks}
    adj: dict[str, list[str]] = {t["id"]: [] for t in tasks}
    for t in tasks:
        for dep in t.get("deps", []):
            in_degree[t["id"]] += 1
            adj[dep].append(t["id"])
    queue   = [tid for tid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nb in adj[node]:
            in_degree[nb] -= 1
            if in_degree[nb] == 0:
                queue.append(nb)
    if visited != len(tasks):
        raise ValueError("Task DAG contains a cycle")


# ── Singleton ─────────────────────────────────────────────────────────────────

task_queue = TaskQueue()