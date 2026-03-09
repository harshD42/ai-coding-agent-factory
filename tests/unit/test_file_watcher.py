"""
tests/unit/test_file_watcher.py — FileWatcher unit tests.

Phase 4A.3 additions at bottom:
  - Debounce: rapid events for same path coalesced into one
  - publish_codebase_updated() publishes correct event
  - _process_event() updates hash registry and pub/sub
  - Deleted events remove hash entry
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Existing tests ────────────────────────────────────────────────────────────

class TestFileWatcher:
    def test_singleton_exists(self):
        from file_watcher import file_watcher, FileWatcher
        assert isinstance(file_watcher, FileWatcher)

    def test_rel_path(self):
        from file_watcher import _rel, WATCH_PATH
        result = _rel(f"{WATCH_PATH}/src/foo.py")
        assert result == "src/foo.py"

    def test_should_skip_pyc(self):
        from file_watcher import _should_skip
        assert _should_skip("/workspace/foo.pyc") is True

    def test_should_skip_git(self):
        from file_watcher import _should_skip
        assert _should_skip("/workspace/.git/HEAD") is True

    def test_should_not_skip_py(self):
        from file_watcher import _should_skip
        assert _should_skip("/workspace/foo.py") is False

    def test_start_without_watchdog_logs_warning(self):
        from file_watcher import FileWatcher
        import file_watcher as fw_module
        original = fw_module._WATCHDOG_AVAILABLE
        fw_module._WATCHDOG_AVAILABLE = False
        try:
            watcher = FileWatcher()
            mock_redis = MagicMock()
            import asyncio
            asyncio.get_event_loop().run_until_complete(watcher.start(mock_redis))
            assert watcher._running is False
        finally:
            fw_module._WATCHDOG_AVAILABLE = original

    def test_get_hash_without_redis(self):
        from file_watcher import FileWatcher
        watcher = FileWatcher()
        result  = asyncio.get_event_loop().run_until_complete(watcher.get_hash("foo.py"))
        assert result is None


# ── Phase 4A.3: Debounce tests ────────────────────────────────────────────────

class TestFileWatcherDebounce:
    """
    Tests for the 500ms debounce introduced in Phase 4A.3.
    Multiple rapid events for the same path should be coalesced
    into a single _process_event call.
    """

    def _make_watcher(self):
        from file_watcher import FileWatcher
        w = FileWatcher()
        w._redis   = MagicMock()
        w._running = True
        return w

    @pytest.mark.asyncio
    async def test_debounce_cancels_previous_handle(self):
        """
        When two events arrive for the same path within the debounce window,
        the first handle should be cancelled.
        """
        w = self._make_watcher()
        w._process_event = AsyncMock()

        # Simulate two rapid events for the same path
        path = "/workspace/foo.py"
        loop = asyncio.get_event_loop()

        # First event
        first_handle = MagicMock()
        w._pending_debounce[path] = first_handle

        # Second event arrives — should cancel first
        existing = w._pending_debounce.pop(path, None)
        if existing:
            existing.cancel()
        new_handle = loop.call_later(0.5, lambda: None)
        w._pending_debounce[path] = new_handle

        first_handle.cancel.assert_called_once()
        new_handle.cancel()  # cleanup

    @pytest.mark.asyncio
    async def test_debounce_clears_on_stop(self):
        """stop() must cancel all pending debounce handles."""
        w = self._make_watcher()
        w._observer = None
        w._worker   = None

        handle1 = MagicMock()
        handle2 = MagicMock()
        w._pending_debounce["/workspace/a.py"] = handle1
        w._pending_debounce["/workspace/b.py"] = handle2

        await w.stop()

        handle1.cancel.assert_called_once()
        handle2.cancel.assert_called_once()
        assert len(w._pending_debounce) == 0

    @pytest.mark.asyncio
    async def test_process_event_modified_updates_hash(self):
        """_process_event for a modified file should update the hash registry."""
        w = self._make_watcher()
        w._redis.hset    = AsyncMock()
        w._redis.publish = AsyncMock()

        with patch("file_watcher._sha256", return_value="abc123"), \
             patch("file_watcher._rel", return_value="foo.py"):
            await w._process_event("/workspace/foo.py", "modified")

        w._redis.hset.assert_called_once_with("filewatch:hashes", "foo.py", "abc123")

    @pytest.mark.asyncio
    async def test_process_event_deleted_removes_hash(self):
        """_process_event for a deleted file should remove the hash entry."""
        w = self._make_watcher()
        w._redis.hdel    = AsyncMock()
        w._redis.publish = AsyncMock()

        with patch("file_watcher._rel", return_value="gone.py"):
            await w._process_event("/workspace/gone.py", "deleted")

        w._redis.hdel.assert_called_once_with("filewatch:hashes", "gone.py")

    @pytest.mark.asyncio
    async def test_process_event_publishes_to_channel(self):
        """_process_event must publish to EVENT_CHANNEL for conflict detection."""
        w = self._make_watcher()
        w._redis.hset    = AsyncMock()
        w._redis.publish = AsyncMock()

        with patch("file_watcher._sha256", return_value="def456"), \
             patch("file_watcher._rel", return_value="bar.py"):
            await w._process_event("/workspace/bar.py", "created")

        w._redis.publish.assert_called_once()
        channel, msg = w._redis.publish.call_args[0]
        assert channel == "filewatch:events"
        payload = json.loads(msg)
        assert payload["event"] == "created"
        assert payload["path"]  == "bar.py"

    @pytest.mark.asyncio
    async def test_process_event_does_not_trigger_reindex(self):
        """
        _process_event must NOT call index_codebase or publish codebase_updated.
        Reindexing is commit-driven only (Phase 4A.3 design decision).
        """
        w = self._make_watcher()
        w._redis.hset    = AsyncMock()
        w._redis.publish = AsyncMock()

        with patch("file_watcher._sha256", return_value="aaa"), \
             patch("file_watcher._rel", return_value="x.py"):
            await w._process_event("/workspace/x.py", "modified")

        # publish should only be called once (the file event), not for codebase_updated
        assert w._redis.publish.call_count == 1
        channel, msg = w._redis.publish.call_args[0]
        payload = json.loads(msg)
        assert payload["event"] != "codebase_updated"


# ── Phase 4A.3: publish_codebase_updated ─────────────────────────────────────

class TestCodebaseUpdatedEvent:
    """
    publish_codebase_updated() is called by patch_queue after a successful
    git commit. It signals that the codebase index should be refreshed.
    """

    @pytest.mark.asyncio
    async def test_publish_codebase_updated_sends_correct_event(self):
        from file_watcher import FileWatcher
        w = FileWatcher()
        w._redis         = MagicMock()
        w._redis.publish = AsyncMock()

        await w.publish_codebase_updated()

        w._redis.publish.assert_called_once()
        channel, msg = w._redis.publish.call_args[0]
        assert channel == "filewatch:events"
        payload = json.loads(msg)
        assert payload["event"] == "codebase_updated"

    @pytest.mark.asyncio
    async def test_publish_codebase_updated_no_redis_does_not_crash(self):
        """If redis is None (not yet started), call is a no-op."""
        from file_watcher import FileWatcher
        w = FileWatcher()
        w._redis = None
        # Should not raise
        await w.publish_codebase_updated()