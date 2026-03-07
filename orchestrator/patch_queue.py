"""
patch_queue.py — Diff validation, conflict detection, and patch application.

Flow:
    1. Agent produces a unified diff
    2. validate_patch()       — size, binary, permissions checks
    3. check_conflict()       — compare file hashes against baseline
    4. apply_patch_sandbox()  — dry-run via executor (git apply --check)
    5. apply_patch_live()     — actually apply via executor
    6. On conflict: re-queue task or flag for human review (after 2 retries)

The queue is an in-memory list for MVP (Step 8 moves it to Redis-backed DAG).
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

log = logging.getLogger("patch_queue")

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_DIFF_LINES    = 1000   # reject patches larger than this
MAX_RETRIES       = 2      # conflict retries before human review


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
        self.status      = "pending"   # pending|validated|applied|rejected|conflict
        self.retries     = 0
        self.error:      Optional[str] = None
        # Snapshot of file hashes at the time the patch was generated
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

    # Reject binary patch markers
    if "GIT binary patch" in diff or "Binary files" in diff:
        raise PatchValidationError("Binary patches are not allowed")

    # Reject permission changes
    if re.search(r"^(old|new) mode \d+", diff, re.MULTILINE):
        raise PatchValidationError("Permission-change patches are not allowed")

    # Must look like a unified diff (has at least one hunk header)
    if not re.search(r"^@@\s+-\d+", diff, re.MULTILINE):
        raise PatchValidationError(
            "Does not appear to be a unified diff (no @@ hunk headers found)"
        )


# ── File hashing ──────────────────────────────────────────────────────────────

async def snapshot_file_hashes(paths: list[str]) -> dict[str, str]:
    """
    Read current content of each file and return {path: sha256_hex}.
    Files that don't exist yet get hash "NEW".
    """
    hashes = {}
    for path in paths:
        try:
            result = await executor_client.read_file(path)
            content = result.get("content", "")
            hashes[path] = hashlib.sha256(content.encode()).hexdigest()
        except Exception:
            hashes[path] = "NEW"
    return hashes


async def check_conflict(patch: Patch) -> bool:
    """
    Return True if any file targeted by the patch has changed since the
    patch was generated (i.e. its current hash differs from the snapshot).
    """
    if not patch.file_hashes:
        return False   # no baseline recorded — assume no conflict

    current_hashes = await snapshot_file_hashes(list(patch.file_hashes.keys()))
    for path, baseline_hash in patch.file_hashes.items():
        current = current_hashes.get(path, "NEW")
        if current != baseline_hash:
            log.warning(
                "conflict detected  patch=%s  file=%s  baseline=%s  current=%s",
                patch.patch_id, path, baseline_hash[:8], current[:8],
            )
            return True
    return False


# ── PatchQueue ────────────────────────────────────────────────────────────────

class PatchQueue:
    def __init__(self):
        self._queue:   list[Patch]        = []
        self._patches: dict[str, Patch]   = {}   # patch_id → Patch
        self._lock     = asyncio.Lock()

    # ── Enqueue ───────────────────────────────────────────────────────────────

    async def enqueue(
        self,
        diff: str,
        agent_id: str,
        task_id: str,
        session_id: str,
        description: str = "",
    ) -> Patch:
        """
        Validate and enqueue a patch.
        Snapshots file hashes at enqueue time for later conflict detection.
        Raises PatchValidationError if the diff is malformed.
        """
        validate_patch(diff)

        patch = Patch(
            diff=normalize_diff(diff),
            agent_id=agent_id,
            task_id=task_id,
            session_id=session_id,
            description=description,
        )

        # Snapshot current state of affected files
        affected = extract_file_paths_from_diff(diff)
        if affected:
            patch.file_hashes = await snapshot_file_hashes(affected)

        async with self._lock:
            self._queue.append(patch)
            self._patches[patch.patch_id] = patch

        log.info(
            "enqueued  patch=%s  files=%s  lines=%d",
            patch.patch_id, affected, count_diff_lines(diff),
        )
        return patch

    # ── Process ───────────────────────────────────────────────────────────────

    async def process_next(self) -> Optional[dict]:
        """
        Process the next pending patch in the queue.
        Returns a result dict or None if the queue is empty.
        """
        async with self._lock:
            pending = [p for p in self._queue if p.status == "pending"]
            if not pending:
                return None
            patch = pending[0]
            patch.status = "processing"

        return await self._apply_patch(patch)

    async def process_all(self) -> list[dict]:
        """Process all pending patches sequentially. Returns list of results."""
        results = []
        while True:
            result = await self.process_next()
            if result is None:
                break
            results.append(result)
        return results

    async def _apply_patch(self, patch: Patch) -> dict:
        """Full pipeline: conflict check → sandbox → live apply."""
        try:
            # 1. Conflict detection
            if await check_conflict(patch):
                patch.retries += 1
                if patch.retries >= MAX_RETRIES:
                    patch.status = "conflict"
                    patch.error  = "Max retries exceeded — flagged for human review"
                    log.error("patch conflict unresolved  patch=%s", patch.patch_id)
                    return {**patch.to_dict(), "action": "needs_review"}
                else:
                    patch.status = "pending"
                    log.warning(
                        "conflict re-queued  patch=%s  retry=%d",
                        patch.patch_id, patch.retries,
                    )
                    return {**patch.to_dict(), "action": "requeued"}

            # 2. Sandbox dry-run
            sandbox = await executor_client.apply_patch(patch.diff, target="sandbox")
            if not sandbox.get("applied"):
                patch.status = "rejected"
                patch.error  = sandbox.get("message", "Sandbox check failed")
                log.warning("patch rejected (sandbox)  patch=%s  reason=%s",
                            patch.patch_id, patch.error)
                return {**patch.to_dict(), "action": "rejected"}

            # 3. Live apply
            live = await executor_client.apply_patch(patch.diff, target="live")
            if not live.get("applied"):
                patch.status = "rejected"
                patch.error  = live.get("message", "Live apply failed")
                log.error("patch rejected (live)  patch=%s  reason=%s",
                          patch.patch_id, patch.error)
                return {**patch.to_dict(), "action": "rejected"}

            patch.status = "applied"
            log.info("patch applied  patch=%s", patch.patch_id)
            return {**patch.to_dict(), "action": "applied"}

        except Exception as e:
            patch.status = "rejected"
            patch.error  = str(e)
            log.exception("patch processing error  patch=%s: %s", patch.patch_id, e)
            return {**patch.to_dict(), "action": "error", "error": str(e)}

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
            "total":      len(self._queue),
            "pending":    statuses.count("pending"),
            "applied":    statuses.count("applied"),
            "rejected":   statuses.count("rejected"),
            "conflict":   statuses.count("conflict"),
        }


# ── Module-level singleton ────────────────────────────────────────────────────

patch_queue = PatchQueue()