"""
patch_queue.py — Diff validation, conflict detection, and patch application.

Flow:
    1. Agent produces a unified diff
    2. validate_patch()      — size, binary, permissions checks
    3. check_conflict()      — compare file hashes against baseline
    4. apply_patch_sandbox() — dry-run via executor (git apply --check)
    5. apply_patch_live()    — actually apply via executor
    6. On conflict: re-queue task or flag for human review (after 2 retries)

Step 2.2 adds:
    test_fix_loop()          — after apply, run pytest; on failure feed stderr
                               back to the coder agent for a fix diff (≤ MAX_FIX_ATTEMPTS)
"""

import asyncio
import hashlib
import logging
import re
import time
import uuid
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
    """
    Structural validation before touching the filesystem.
    Raises PatchValidationError with a human-readable reason on failure.
    """
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
    """Return True if any target file has changed since the patch was generated."""
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
        self._queue:   list[Patch]      = []
        self._patches: dict[str, Patch] = {}
        self._lock     = asyncio.Lock()

    # ── Enqueue ───────────────────────────────────────────────────────────────

    async def enqueue(
        self,
        diff: str,
        agent_id: str = "auto",
        task_id:  str = "",
        session_id: str = "default",
        description: str = "",
    ) -> Patch:
        """
        Validate and enqueue a patch. Snapshots file hashes for conflict detection.
        Raises PatchValidationError if the diff is malformed.
        """
        validate_patch(diff)
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

        log.info("enqueued  patch=%s  files=%s  lines=%d",
                 patch.patch_id, affected, count_diff_lines(diff))
        return patch

    # ── Process ───────────────────────────────────────────────────────────────

    async def process_next(self) -> Optional[dict]:
        async with self._lock:
            pending = [p for p in self._queue if p.status == "pending"]
            if not pending:
                return None
            patch = pending[0]
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
        """Full pipeline: conflict check → sandbox → live apply."""
        try:
            if await check_conflict(patch):
                patch.retries += 1
                if patch.retries >= MAX_RETRIES:
                    patch.status = "conflict"
                    patch.error  = "Max retries exceeded — flagged for human review"
                    log.error("patch conflict unresolved  patch=%s", patch.patch_id)
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
                return {**patch.to_dict(), "action": "rejected"}

            live = await executor_client.apply_patch(patch.diff, target="live")
            if not live.get("applied"):
                patch.status = "rejected"
                patch.error  = live.get("message", "Live apply failed")
                log.error("patch rejected (live)  patch=%s", patch.patch_id)
                return {**patch.to_dict(), "action": "rejected"}

            patch.status = "applied"
            log.info("patch applied  patch=%s", patch.patch_id)
            return {**patch.to_dict(), "action": "applied"}

        except Exception as e:
            patch.status = "rejected"
            patch.error  = str(e)
            log.exception("patch error  patch=%s: %s", patch.patch_id, e)
            return {**patch.to_dict(), "action": "error", "error": str(e)}

    # ── Step 2.2: Test-fix loop ───────────────────────────────────────────────

    async def test_fix_loop(
        self,
        patch: Patch,
        agent_mgr,                     # AgentManager — avoid circular import
        test_pattern: str = "tests/",
        max_attempts: int = None,
    ) -> dict:
        """
        Apply *patch*, run pytest, and if tests fail feed stderr back to
        the coder for a fix diff.  Repeats up to max_attempts times.

        Flow per attempt:
            1. Apply patch via _apply_patch()
            2. Run executor.run_tests(test_pattern)
            3. If pass  → return success result
            4. If fail  → spawn coder agent with failure context
                        → extract fix diff from coder output
                        → enqueue and loop

        After max_attempts failures the patch is flagged needs_review.

        Returns a result dict with extra keys:
            "test_passed":    bool
            "attempts":       int
            "test_summary":   str   (last pytest summary line)
        """
        if max_attempts is None:
            max_attempts = config.MAX_FIX_ATTEMPTS

        from utils import extract_diffs_from_result   # local import — already in utils

        attempt    = 0
        current_patch = patch

        while attempt < max_attempts:
            attempt += 1
            log.info("test_fix_loop: attempt %d/%d  patch=%s",
                     attempt, max_attempts, current_patch.patch_id)

            # 1. Apply
            apply_result = await self._apply_patch(current_patch)
            if apply_result.get("action") not in ("applied",):
                # Patch was rejected or conflicted — no point running tests
                apply_result["test_passed"]  = False
                apply_result["attempts"]     = attempt
                apply_result["test_summary"] = ""
                return apply_result

            # 2. Run tests
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

            # 3. Tests failed
            log.warning(
                "test_fix_loop: FAIL attempt %d  patch=%s  summary=%r",
                attempt, current_patch.patch_id, summary
            )

            if attempt >= max_attempts:
                break

            # 4. Ask coder for a fix
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
                log.warning(
                    "test_fix_loop: coder produced no diff on attempt %d", attempt
                )
                break

            # Enqueue first fix diff and loop
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

        # Exhausted attempts
        current_patch.status = "conflict"
        current_patch.error  = (
            f"Tests still failing after {attempt} fix attempt(s) — flagged for human review"
        )
        log.error("test_fix_loop: max attempts reached  patch=%s", patch.patch_id)
        return {
            **current_patch.to_dict(),
            "action":       "needs_review",
            "test_passed":  False,
            "attempts":     attempt,
            "test_summary": summary if "summary" in dir() else "",
        }

    # ── Query ─────────────────────────────────────────────────────────────────

    def get_patch(self, patch_id: str) -> Optional[Patch]:
        return self._patches.get(patch_id)

    def list_patches(self, session_id: str = None) -> list[dict]:
        patches = self._patches.values()
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