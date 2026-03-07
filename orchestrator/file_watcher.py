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

Lifecycle:
    Call start() once in the FastAPI lifespan (after Redis is connected).
    Call stop()  in the shutdown hook.

Notes:
    - The orchestrator mounts /workspace read-only, so we watch the path
      that is available to the orchestrator container: /workspace.
    - Hash computation reads the file from disk directly (not via executor)
      because the orchestrator has read-only access to the same volume.
    - watchdog is CPU-light (inotify on Linux, FSEvents on macOS).
    - This is the ONLY new file introduced in Phase 2.
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
    We use a thread-safe queue because watchdog runs on a background thread.
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

    Usage:
        watcher = FileWatcher()
        await watcher.start(redis_client)   # call in lifespan startup
        ...
        await watcher.stop()                # call in lifespan shutdown
    """

    def __init__(self, watch_path: str = WATCH_PATH):
        self._watch_path = watch_path
        self._redis:    Optional[aioredis.Redis] = None
        self._observer: Optional["Observer"]     = None
        self._worker:   Optional[asyncio.Task]   = None
        self._queue:    asyncio.Queue            = asyncio.Queue()
        self._running   = False

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

        # Initial scan
        await self._full_scan()
        log.info("file_watcher: initial scan complete")

        # Start watchdog observer on a daemon thread
        loop    = asyncio.get_event_loop()
        handler = _Handler(self._queue, loop)
        self._observer = Observer()
        self._observer.schedule(handler, self._watch_path, recursive=True)
        self._observer.start()
        log.info("file_watcher: watching %s", self._watch_path)

        # Async worker drains the event queue and updates Redis
        self._worker = asyncio.create_task(self._event_worker())

    async def stop(self) -> None:
        """Stop the watcher cleanly."""
        self._running = False
        if self._observer:
            self._observer.stop()
            # Join on a thread pool executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._observer.join)
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        log.info("file_watcher: stopped")

    # ── Redis helpers ─────────────────────────────────────────────────────────

    async def get_hash(self, rel_path: str) -> Optional[str]:
        """Return the current hash for a relative path, or None if unknown."""
        if not self._redis:
            return None
        val = await self._redis.hget(REGISTRY_KEY, rel_path)
        return val  # "DELETED" | sha256_hex | None

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

        pipe = self._redis.pipeline()
        count = 0
        for p in root.rglob("*"):
            if p.is_file() and not _should_skip(str(p)):
                rel  = _rel(str(p))
                sha  = _sha256(str(p))
                if sha:
                    pipe.hset(REGISTRY_KEY, rel, sha)
                    count += 1
        await pipe.execute()
        log.info("file_watcher: indexed %d files", count)

    # ── Event worker ──────────────────────────────────────────────────────────

    async def _event_worker(self) -> None:
        """Drain the event queue and sync each change to Redis."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            path     = event["path"]
            evt_type = event["event"]
            rel      = _rel(path)

            if evt_type == "deleted":
                await self._del_hash(rel)
                await self._publish("deleted", rel)
                log.debug("file_watcher: deleted  %s", rel)
            else:
                # modified or created
                sha = await asyncio.get_event_loop().run_in_executor(
                    None, _sha256, path
                )
                if sha:
                    await self._set_hash(rel, sha)
                    await self._publish(evt_type, rel)
                    log.debug("file_watcher: %s  %s  %s", evt_type, rel, sha[:8])


# ── Singleton ─────────────────────────────────────────────────────────────────

file_watcher = FileWatcher()