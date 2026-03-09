"""
file_watcher.py — Real-time workspace file-change tracking (Step 2.4).

Uses the watchdog library to monitor /workspace for file modifications,
creations, and deletions.  On each event the file's SHA-256 hash is
updated (or removed) in a Redis hash registry under the key:

    filewatch:hashes   →  {relative/path: sha256_hex | "DELETED"}

patch_queue.check_conflict() can then query this live registry instead
of re-reading files from disk (which required a round-trip to executor).

Redis pub/sub:
    Channel "filewatch:events" receives JSON messages:
        {"event": "modified"|"created"|"deleted", "path": "relative/path"}

Phase 4A.3 additions:
    - 500ms debounce on raw file events — editors (VS Code, vim) trigger
      multiple write events per save (temp file, swap file, actual write).
      Without debouncing a single save causes 3-5 redundant reindex calls.
      Implementation: per-path asyncio.TimerHandle coalescing window.
    - Commit-based reindex trigger — raw file events still update the hash
      registry and pub/sub (used by conflict detection), but indexing is
      no longer triggered directly by file changes. Instead, a
      "codebase_updated" event is published after a successful git commit
      in patch_queue. This guarantees the codebase index always reflects
      a complete committed state, never a half-applied patch.

Lifecycle:
    Call start() once in the FastAPI lifespan (after Redis is connected).
    Call stop()  in the shutdown hook.
"""

import asyncio
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis

try:
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    from watchdog.observers import Observer
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

import config

log = logging.getLogger("file_watcher")

WATCH_PATH       = "/workspace"
REGISTRY_KEY     = "filewatch:hashes"
EVENT_CHANNEL    = "filewatch:events"
SKIP_EXTENSIONS  = {".pyc", ".pyo", ".swp", ".tmp", ".log"}
SKIP_DIRS        = {".git", "__pycache__", "node_modules", ".venv"}

# Phase 4A.3: debounce window in seconds — coalesces rapid editor write events
DEBOUNCE_SECONDS = 0.5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rel(path: str) -> str:
    """Return path relative to WATCH_PATH, using forward slashes."""
    try:
        return str(Path(path).relative_to(WATCH_PATH)).replace("\\", "/")
    except ValueError:
        return path


def _should_skip(path: str) -> bool:
    p = Path(path)
    if p.suffix.lower() in SKIP_EXTENSIONS:
        return True
    return any(part in SKIP_DIRS for part in p.parts)


def _sha256(path: str) -> Optional[str]:
    try:
        data = Path(path).read_bytes()
        return hashlib.sha256(data).hexdigest()
    except Exception:
        return None


# ── Watchdog event handler ────────────────────────────────────────────────────

class _Handler(FileSystemEventHandler if _WATCHDOG_AVAILABLE else object):
    """
    Receives watchdog filesystem events and queues them for async processing.
    Uses a thread-safe queue because watchdog runs on a background thread.
    """

    def __init__(self, event_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        if _WATCHDOG_AVAILABLE:
            super().__init__()
        self._q    = event_queue
        self._loop = loop

    def _enqueue(self, event_type: str, path: str) -> None:
        if _should_skip(path):
            return
        try:
            self._loop.call_soon_threadsafe(
                self._q.put_nowait, {"event": event_type, "path": path}
            )
        except Exception as e:
            log.debug("enqueue error: %s", e)

    def on_modified(self, event: "FileSystemEvent") -> None:
        if not event.is_directory:
            self._enqueue("modified", event.src_path)

    def on_created(self, event: "FileSystemEvent") -> None:
        if not event.is_directory:
            self._enqueue("created", event.src_path)

    def on_deleted(self, event: "FileSystemEvent") -> None:
        if not event.is_directory:
            self._enqueue("deleted", event.src_path)

    def on_moved(self, event: "FileSystemEvent") -> None:
        if not event.is_directory:
            self._enqueue("deleted", event.src_path)
            self._enqueue("created", event.dest_path)


# ── FileWatcher ───────────────────────────────────────────────────────────────

class FileWatcher:
    """
    Watches /workspace and keeps a Redis hash registry up to date.

    Phase 4A.3: raw file events are debounced (500ms) before processing.
    Indexing is NOT triggered by file events — only by explicit
    publish_codebase_updated() calls (made after successful git commits).

    Usage:
        watcher = FileWatcher()
        await watcher.start(redis_client)
        ...
        await watcher.publish_codebase_updated()  # called by patch_queue
        ...
        await watcher.stop()
    """

    def __init__(self, watch_path: str = WATCH_PATH):
        self._watch_path  = watch_path
        self._redis:      Optional[aioredis.Redis] = None
        self._observer:   Optional["Observer"]     = None
        self._worker:     Optional[asyncio.Task]   = None
        self._queue:      asyncio.Queue            = asyncio.Queue()
        self._running     = False

        # Phase 4A.3: debounce state — path → pending asyncio.Handle
        self._pending_debounce: dict[str, asyncio.TimerHandle] = {}

    async def start(self, redis_client: aioredis.Redis) -> None:
        """
        Start watching. Performs an initial full scan to populate the registry,
        then launches the watchdog observer + async event worker.
        """
        if not _WATCHDOG_AVAILABLE:
            log.warning(
                "watchdog package not installed — file watcher disabled. "
                "Add watchdog==4.0.0 to orchestrator/requirements.txt"
            )
            return

        self._redis   = redis_client
        self._running = True

        await self._full_scan()
        log.info("file_watcher: initial scan complete")

        loop    = asyncio.get_event_loop()
        handler = _Handler(self._queue, loop)
        self._observer = Observer()
        self._observer.schedule(handler, self._watch_path, recursive=True)
        self._observer.start()
        log.info("file_watcher: watching %s (debounce=%.1fs)", self._watch_path, DEBOUNCE_SECONDS)

        self._worker = asyncio.create_task(self._event_worker())

    async def stop(self) -> None:
        """Stop the watcher cleanly."""
        self._running = False

        # Cancel any pending debounce handles
        for handle in self._pending_debounce.values():
            handle.cancel()
        self._pending_debounce.clear()

        if self._observer:
            self._observer.stop()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._observer.join)
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        log.info("file_watcher: stopped")

    # ── Phase 4A.3: Commit-based reindex signal ───────────────────────────────

    async def publish_codebase_updated(self) -> None:
        """
        Publish a 'codebase_updated' event to signal that a git commit has
        landed and the codebase index should be refreshed.

        Called by patch_queue after a successful git apply + commit.
        The orchestrator's /v1/index endpoint (or background task in 4B)
        subscribes to this and triggers memory_manager.index_codebase().

        This separation guarantees the index always reflects a complete
        committed state — never a half-applied patch or an editor temp file.
        """
        if not self._redis:
            return
        msg = json.dumps({"event": "codebase_updated"})
        await self._redis.publish(EVENT_CHANNEL, msg)
        log.info("file_watcher: codebase_updated published")

    # ── Redis helpers ─────────────────────────────────────────────────────────

    async def get_hash(self, rel_path: str) -> Optional[str]:
        """Return the current hash for a relative path, or None if unknown."""
        if not self._redis:
            return None
        return await self._redis.hget(REGISTRY_KEY, rel_path)

    async def _set_hash(self, rel_path: str, value: str) -> None:
        await self._redis.hset(REGISTRY_KEY, rel_path, value)

    async def _del_hash(self, rel_path: str) -> None:
        await self._redis.hdel(REGISTRY_KEY, rel_path)

    async def _publish(self, event_type: str, rel_path: str) -> None:
        msg = json.dumps({"event": event_type, "path": rel_path})
        await self._redis.publish(EVENT_CHANNEL, msg)

    # ── Scanning ──────────────────────────────────────────────────────────────

    async def _full_scan(self) -> None:
        """Walk the workspace and populate the hash registry."""
        root = Path(self._watch_path)
        if not root.exists():
            log.warning("file_watcher: watch path does not exist: %s", self._watch_path)
            return

        pipe  = self._redis.pipeline()
        count = 0
        for p in root.rglob("*"):
            if p.is_file() and not _should_skip(str(p)):
                rel = _rel(str(p))
                sha = _sha256(str(p))
                if sha:
                    pipe.hset(REGISTRY_KEY, rel, sha)
                    count += 1
        await pipe.execute()
        log.info("file_watcher: indexed %d files", count)

    # ── Event worker with debounce ────────────────────────────────────────────

    async def _event_worker(self) -> None:
        """
        Drain the event queue and sync each change to Redis.

        Phase 4A.3: events are debounced per path. If multiple events arrive
        for the same path within DEBOUNCE_SECONDS, only the final one is
        processed. This prevents 3-5 redundant hash updates per editor save.

        Hash registry and pub/sub are still updated on every debounced event —
        these are lightweight and needed for conflict detection. Reindexing
        is NOT triggered here; it happens via publish_codebase_updated() only.
        """
        loop = asyncio.get_event_loop()

        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            path     = event["path"]
            evt_type = event["event"]

            # Cancel any existing debounce handle for this path
            existing = self._pending_debounce.pop(path, None)
            if existing:
                existing.cancel()

            # Schedule debounced processing
            handle = loop.call_later(
                DEBOUNCE_SECONDS,
                lambda p=path, e=evt_type: asyncio.ensure_future(
                    self._process_event(p, e)
                ),
            )
            self._pending_debounce[path] = handle

    async def _process_event(self, path: str, evt_type: str) -> None:
        """
        Process a single debounced file event: update Redis hash registry
        and publish to the event channel.

        Does NOT trigger codebase reindexing — that is commit-driven only.
        """
        self._pending_debounce.pop(path, None)
        rel = _rel(path)

        if evt_type == "deleted":
            await self._del_hash(rel)
            await self._publish("deleted", rel)
            log.debug("file_watcher: deleted  %s", rel)
        else:
            loop = asyncio.get_event_loop()
            sha  = await loop.run_in_executor(None, _sha256, path)
            if sha:
                await self._set_hash(rel, sha)
                await self._publish(evt_type, rel)
                log.debug("file_watcher: %s  %s  %s", evt_type, rel, sha[:8])


# ── Singleton ─────────────────────────────────────────────────────────────────

file_watcher = FileWatcher()