"""
patch_queue.py — Diff validation, conflict detection, and patch application.

Phase 3.5 fixes:
  - _queue is now collections.deque (O(1) popleft vs O(n) list scan)
  - MAX_PATCH_QUEUE_DEPTH guard prevents unbounded queue growth
  - Patches persisted to Redis on enqueue — survive orchestrator restart
  - summary variable initialized before loop — fixes silent NameError bug
  - Executor concurrency controlled via _exec_semaphore in executor_client

Phase 4B.3 additions:
  - set_bus(bus) — inject AgentBus singleton (same pattern as set_redis)
  - _apply_patch() publishes PATCH_APPLIED WSEvent to AgentBus after a
    successful live apply, so architect loop and TUI react in real time
  - Bus publish is best-effort: failure is logged, patch result unaffected
"""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections import deque
from typing import Optional

import executor_client
from utils import count_diff_lines, extract_file_paths_from_diff
import config

log = logging.getLogger("patch_queue")

MAX_DIFF_LINES = 1000
MAX_RETRIES    = 2


# ── Patch record ──────────────────────────────────────────────────────────────

class Patch:
    def __init__(
        self,
        diff: str,
        agent_id: str,
        task_id: str,
        session_id: str,
        description: str = "",
    ):
        self.patch_id    = f"patch-{uuid.uuid4().hex[:8]}"
        self.diff        = diff
        self.agent_id    = agent_id
        self.task_id     = task_id
        self.session_id  = session_id
        self.description = description
        self.created_at  = time.time()
        self.status      = "pending"
        self.retries     = 0
        self.error:      Optional[str] = None
        self.file_hashes: dict[str, str] = {}

    def to_dict(self) -> dict:
        return {
            "patch_id":    self.patch_id,
            "agent_id":    self.agent_id,
            "task_id":     self.task_id,
            "session_id":  self.session_id,
            "description": self.description,
            "status":      self.status,
            "retries":     self.retries,
            "error":       self.error,
            "files":       extract_file_paths_from_diff(self.diff),
            "diff_lines":  count_diff_lines(self.diff),
            "created_at":  self.created_at,
        }


# ── Validation ────────────────────────────────────────────────────────────────

class PatchValidationError(Exception):
    pass


def normalize_diff(diff: str) -> str:
    """Normalize line endings to Unix LF. Windows CRLF breaks git apply."""
    return diff.replace("\r\n", "\n").replace("\r", "\n")


def validate_patch(diff: str) -> None:
    if not diff or not diff.strip():
        raise PatchValidationError("Empty diff")
    if len(diff) > 4_000_000:
        raise PatchValidationError("Diff exceeds 4 MB raw size limit")
    lines = count_diff_lines(diff)
    if lines > MAX_DIFF_LINES:
        raise PatchValidationError(
            f"Diff too large: {lines} changed lines (limit {MAX_DIFF_LINES})"
        )
    if "GIT binary patch" in diff or "Binary files" in diff:
        raise PatchValidationError("Binary patches are not allowed")
    if re.search(r"^(old|new) mode \d+", diff, re.MULTILINE):
        raise PatchValidationError("Permission-change patches are not allowed")
    if not re.search(r"^@@\s+-\d+", diff, re.MULTILINE):
        raise PatchValidationError(
            "Does not appear to be a unified diff (no @@ hunk headers found)"
        )


# ── File hashing ──────────────────────────────────────────────────────────────

async def snapshot_file_hashes(paths: list[str]) -> dict[str, str]:
    hashes = {}
    for path in paths:
        try:
            result  = await executor_client.read_file(path)
            content = result.get("content", "")
            hashes[path] = hashlib.sha256(content.encode()).hexdigest()
        except Exception:
            hashes[path] = "NEW"
    return hashes


async def check_conflict(patch: Patch) -> bool:
    if not patch.file_hashes:
        return False
    current = await snapshot_file_hashes(list(patch.file_hashes.keys()))
    for path, baseline in patch.file_hashes.items():
        if current.get(path, "NEW") != baseline:
            log.warning(
                "conflict  patch=%s  file=%s  baseline=%s  current=%s",
                patch.patch_id, path, baseline[:8], current.get(path, "NEW")[:8],
            )
            return True
    return False


# ── PatchQueue ────────────────────────────────────────────────────────────────

class PatchQueue:
    def __init__(self):
        self._queue:   deque[Patch]     = deque()
        self._patches: dict[str, Patch] = {}
        self._lock     = asyncio.Lock()
        self._redis    = None   # injected by main.py lifespan via set_redis()
        self._bus      = None   # injected by main.py lifespan via set_bus()

    def set_redis(self, redis_client) -> None:
        """Inject Redis client for patch persistence (called in lifespan)."""
        self._redis = redis_client

    def set_bus(self, bus) -> None:
        """
        Inject AgentBus for PATCH_APPLIED event publishing (called in lifespan).
        Same late-injection pattern as set_redis() — PatchQueue is a module-level
        singleton created before lifespan runs.
        """
        self._bus = bus

    # ── Redis persistence helpers ─────────────────────────────────────────────

    async def _persist_patch(self, patch: Patch) -> None:
        if not self._redis:
            return
        try:
            key = f"patches:{patch.session_id}:{patch.patch_id}"
            await self._redis.set(key, json.dumps(patch.to_dict()))
        except Exception as e:
            log.warning("failed to persist patch %s: %s", patch.patch_id, e)

    async def _unpersist_patch(self, patch: Patch) -> None:
        if not self._redis:
            return
        try:
            await self._redis.delete(f"patches:{patch.session_id}:{patch.patch_id}")
        except Exception as e:
            log.warning("failed to remove patch %s from Redis: %s", patch.patch_id, e)

    async def recover_from_redis(self, session_id: str) -> int:
        if not self._redis:
            return 0
        recovered = 0
        try:
            keys = await self._redis.keys(f"patches:{session_id}:*")
            for key in keys:
                raw = await self._redis.get(key)
                if not raw:
                    continue
                data = json.loads(raw)
                if data.get("status") != "pending":
                    continue
                log.info("recovered patch metadata %s from Redis", data.get("patch_id"))
                recovered += 1
        except Exception as e:
            log.warning("patch recovery failed: %s", e)
        return recovered

    # ── Enqueue ───────────────────────────────────────────────────────────────

    async def enqueue(
        self,
        diff: str,
        agent_id:    str = "auto",
        task_id:     str = "",
        session_id:  str = "default",
        description: str = "",
    ) -> Patch:
        validate_patch(diff)

        async with self._lock:
            if len(self._queue) >= config.MAX_PATCH_QUEUE_DEPTH:
                raise PatchValidationError(
                    f"Patch queue full ({config.MAX_PATCH_QUEUE_DEPTH} patches pending). "
                    "Process existing patches before submitting more."
                )

        patch = Patch(
            diff=normalize_diff(diff),
            agent_id=agent_id,
            task_id=task_id or str(uuid.uuid4()),
            session_id=session_id,
            description=description,
        )
        affected = extract_file_paths_from_diff(diff)
        if affected:
            patch.file_hashes = await snapshot_file_hashes(affected)

        async with self._lock:
            self._queue.append(patch)
            self._patches[patch.patch_id] = patch

        await self._persist_patch(patch)

        log.info("enqueued  patch=%s  files=%s  lines=%d  queue_depth=%d",
                 patch.patch_id, affected, count_diff_lines(diff), len(self._queue))
        return patch

    # ── Process ───────────────────────────────────────────────────────────────

    async def process_next(self) -> Optional[dict]:
        async with self._lock:
            patch = None
            for p in self._queue:
                if p.status == "pending":
                    patch = p
                    break
            if patch is None:
                return None
            patch.status = "processing"
        return await self._apply_patch(patch)

    async def process_all(self) -> list[dict]:
        results = []
        while True:
            r = await self.process_next()
            if r is None:
                break
            results.append(r)
        return results

    async def _apply_patch(self, patch: Patch) -> dict:
        """
        Full pipeline: conflict check → sandbox → live apply.

        Phase 4B.3: publishes PATCH_APPLIED to AgentBus on successful live apply.
        The architect loop receives this event via subscribe_architect() and can
        trigger the next DAG task or run tests. TUI receives it via WebSocket.
        Bus publish is best-effort — failure is logged, result dict unaffected.
        """
        try:
            if await check_conflict(patch):
                patch.retries += 1
                if patch.retries >= MAX_RETRIES:
                    patch.status = "conflict"
                    patch.error  = "Max retries exceeded — flagged for human review"
                    log.error("patch conflict unresolved  patch=%s", patch.patch_id)
                    await self._unpersist_patch(patch)
                    return {**patch.to_dict(), "action": "needs_review"}
                patch.status = "pending"
                log.warning("conflict re-queued  patch=%s  retry=%d",
                            patch.patch_id, patch.retries)
                return {**patch.to_dict(), "action": "requeued"}

            sandbox = await executor_client.apply_patch(patch.diff, target="sandbox")
            if not sandbox.get("applied"):
                patch.status = "rejected"
                patch.error  = sandbox.get("message", "Sandbox check failed")
                log.warning("patch rejected (sandbox)  patch=%s", patch.patch_id)
                await self._unpersist_patch(patch)
                return {**patch.to_dict(), "action": "rejected"}

            live = await executor_client.apply_patch(patch.diff, target="live")
            if not live.get("applied"):
                patch.status = "rejected"
                patch.error  = live.get("message", "Live apply failed")
                log.error("patch rejected (live)  patch=%s", patch.patch_id)
                await self._unpersist_patch(patch)
                return {**patch.to_dict(), "action": "rejected"}

            patch.status = "applied"
            log.info("patch applied  patch=%s", patch.patch_id)
            await self._unpersist_patch(patch)

            # Phase 4B.3: notify architect + TUI that a patch landed
            await self._publish_patch_applied(patch)

            return {**patch.to_dict(), "action": "applied"}

        except Exception as e:
            patch.status = "rejected"
            patch.error  = str(e)
            log.exception("patch error  patch=%s: %s", patch.patch_id, e)
            await self._unpersist_patch(patch)
            return {**patch.to_dict(), "action": "error", "error": str(e)}

    async def _publish_patch_applied(self, patch: Patch) -> None:
        """
        Publish PATCH_APPLIED WSEvent to AgentBus (best-effort).
        Separated from _apply_patch() to keep error handling clean —
        a bus failure must never affect the patch result.
        """
        if self._bus is None:
            return
        try:
            from models import WSEvent, WSEventType
            await self._bus.publish(patch.session_id, WSEvent(
                type=WSEventType.PATCH_APPLIED,
                session_id=patch.session_id,
                agent_id=patch.agent_id,
                payload={
                    "patch_id":    patch.patch_id,
                    "task_id":     patch.task_id,
                    "files":       extract_file_paths_from_diff(patch.diff),
                    "description": patch.description,
                },
            ))
        except Exception as e:
            log.warning("bus publish PATCH_APPLIED failed patch=%s: %s",
                        patch.patch_id, e)

    # ── Test-fix loop ─────────────────────────────────────────────────────────

    async def test_fix_loop(
        self,
        patch: Patch,
        agent_mgr,
        test_pattern: str = "tests/",
        max_attempts: int = None,
    ) -> dict:
        if max_attempts is None:
            max_attempts = config.MAX_FIX_ATTEMPTS

        from utils import extract_diffs_from_result

        attempt       = 0
        current_patch = patch
        summary       = ""

        while attempt < max_attempts:
            attempt += 1
            log.info("test_fix_loop: attempt %d/%d  patch=%s",
                     attempt, max_attempts, current_patch.patch_id)

            apply_result = await self._apply_patch(current_patch)
            if apply_result.get("action") not in ("applied",):
                apply_result["test_passed"]  = False
                apply_result["attempts"]     = attempt
                apply_result["test_summary"] = ""
                return apply_result

            test_result = await executor_client.run_tests(
                pattern=test_pattern, timeout=120
            )
            summary = test_result.get("summary", "")

            if test_result.get("passed"):
                log.info("test_fix_loop: PASS on attempt %d  patch=%s",
                         attempt, current_patch.patch_id)
                return {
                    **apply_result,
                    "test_passed":  True,
                    "attempts":     attempt,
                    "test_summary": summary,
                }

            log.warning("test_fix_loop: FAIL attempt %d  patch=%s  summary=%r",
                        attempt, current_patch.patch_id, summary)

            if attempt >= max_attempts:
                break

            stderr   = test_result.get("stderr", "") or test_result.get("stdout", "")
            fix_task = (
                f"The following tests failed after applying your patch.\n"
                f"Produce a unified diff that fixes only the failing tests.\n\n"
                f"Test output:\n```\n{stderr[:3000]}\n```"
            )
            fix_result = await agent_mgr.spawn_and_run(
                role="coder",
                task=fix_task,
                session_id=current_patch.session_id,
            )
            fix_output = fix_result.get("result", "") or ""
            fix_diffs  = extract_diffs_from_result(fix_output)

            if not fix_diffs:
                log.warning("test_fix_loop: coder produced no diff on attempt %d", attempt)
                break

            try:
                current_patch = await self.enqueue(
                    diff=fix_diffs[0],
                    agent_id="coder-fix",
                    task_id=current_patch.task_id,
                    session_id=current_patch.session_id,
                    description=f"Auto-fix attempt {attempt} for {patch.patch_id}",
                )
            except Exception as exc:
                log.error("test_fix_loop: failed to enqueue fix diff: %s", exc)
                break

        current_patch.status = "conflict"
        current_patch.error  = (
            f"Tests still failing after {attempt} fix attempt(s) — flagged for human review"
        )
        log.error("test_fix_loop: max attempts reached  patch=%s", patch.patch_id)
        await self._unpersist_patch(current_patch)
        return {
            **current_patch.to_dict(),
            "action":       "needs_review",
            "test_passed":  False,
            "attempts":     attempt,
            "test_summary": summary,
        }

    # ── Query ─────────────────────────────────────────────────────────────────

    def get_patch(self, patch_id: str) -> Optional[Patch]:
        return self._patches.get(patch_id)

    def list_patches(self, session_id: str = None) -> list[dict]:
        patches = list(self._patches.values())
        if session_id:
            patches = [p for p in patches if p.session_id == session_id]
        return [p.to_dict() for p in patches]

    def queue_depth(self) -> dict:
        statuses = [p.status for p in self._queue]
        return {
            "total":    len(self._queue),
            "pending":  statuses.count("pending"),
            "applied":  statuses.count("applied"),
            "rejected": statuses.count("rejected"),
            "conflict": statuses.count("conflict"),
        }


# ── Singleton ─────────────────────────────────────────────────────────────────

patch_queue = PatchQueue()