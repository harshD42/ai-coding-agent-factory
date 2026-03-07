import pytest
from unittest.mock import AsyncMock, MagicMock, patch as mock_patch


class TestFileWatcher:
    def test_singleton_exists(self):
        from file_watcher import file_watcher, FileWatcher
        assert isinstance(file_watcher, FileWatcher)

    def test_rel_path(self):
        from file_watcher import _rel
        assert _rel("/workspace/src/foo.py") == "src/foo.py"

    def test_should_skip_pyc(self):
        from file_watcher import _should_skip
        assert _should_skip("/workspace/src/foo.pyc") is True

    def test_should_skip_git(self):
        from file_watcher import _should_skip
        assert _should_skip("/workspace/.git/config") is True

    def test_should_not_skip_py(self):
        from file_watcher import _should_skip
        assert _should_skip("/workspace/src/main.py") is False

    @pytest.mark.asyncio
    async def test_start_without_watchdog_logs_warning(self):
        """If watchdog isn't installed, start() logs a warning and returns."""
        from file_watcher import FileWatcher
        fw = FileWatcher()
        redis_mock = AsyncMock()

        with mock_patch("file_watcher._WATCHDOG_AVAILABLE", False):
            # Should not raise
            await fw.start(redis_mock)
        assert fw._observer is None

    @pytest.mark.asyncio
    async def test_get_hash_without_redis(self):
        from file_watcher import FileWatcher
        fw = FileWatcher()
        result = await fw.get_hash("any/path.py")
        assert result is None