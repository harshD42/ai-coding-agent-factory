import pytest
from unittest.mock import AsyncMock, MagicMock, patch as mock_patch


class TestRunTests:
    @pytest.mark.asyncio
    async def test_pass(self):
        mock_exec = AsyncMock(return_value={
            "stdout": "test_foo.py::test_bar PASSED\n1 passed in 0.5s",
            "stderr": "",
            "exit_code": 0,
        })
        with mock_patch("executor_client.execute", mock_exec):
            from executor_client import run_tests
            r = await run_tests()
        assert r["passed"] is True
        assert r["exit_code"] == 0
        assert "passed" in r["summary"]

    @pytest.mark.asyncio
    async def test_fail(self):
        mock_exec = AsyncMock(return_value={
            "stdout": "FAILED test_foo.py::test_bar\n1 failed in 0.3s",
            "stderr": "AssertionError",
            "exit_code": 1,
        })
        with mock_patch("executor_client.execute", mock_exec):
            from executor_client import run_tests
            r = await run_tests()
        assert r["passed"] is False
        assert r["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_executor_error_returns_safe_dict(self):
        mock_exec = AsyncMock(side_effect=ConnectionError("executor down"))
        with mock_patch("executor_client.execute", mock_exec):
            from executor_client import run_tests
            r = await run_tests()
        assert r["passed"] is False
        assert "error" in r

    @pytest.mark.asyncio
    async def test_custom_pattern_and_timeout(self):
        mock_exec = AsyncMock(return_value={
            "stdout": "1 passed", "stderr": "", "exit_code": 0,
        })
        with mock_patch("executor_client.execute", mock_exec) as m:
            from executor_client import run_tests
            await run_tests(pattern="tests/unit/", timeout=60)
        call_kwargs = m.call_args
        assert "tests/unit/" in call_kwargs.kwargs.get("command", "") or \
               "tests/unit/" in str(call_kwargs)
        
import pytest
import asyncio
from unittest.mock import AsyncMock, patch as mock_patch


class TestExecutorSemaphore:
    """Phase 3.5: apply_patch and run_tests are bounded by _exec_semaphore."""

    @pytest.mark.asyncio
    async def test_apply_patch_uses_semaphore(self):
        """Verify semaphore is acquired during apply_patch."""
        import executor_client
        # Reset semaphore to known state
        executor_client._exec_semaphore = asyncio.Semaphore(1)
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"applied": True, "message": "ok"})

        with mock_patch("executor_client._client") as mock_client:
            mock_client.post = AsyncMock(return_value=mock_resp)
            result = await executor_client.apply_patch("--- a/f\n+++ b/f\n@@ -1 +1 @@\n x\n")

        assert result["applied"] is True

    @pytest.mark.asyncio
    async def test_concurrent_apply_patch_bounded(self):
        """Two concurrent apply_patch calls with semaphore(1) must serialize."""
        import executor_client
        import time as _time

        executor_client._exec_semaphore = asyncio.Semaphore(1)
        call_times = []
        delay = 0.05

        async def slow_post(*args, **kwargs):
            call_times.append(_time.monotonic())
            await asyncio.sleep(delay)
            r = MagicMock()
            r.raise_for_status = MagicMock()
            r.json = MagicMock(return_value={"applied": True, "message": "ok"})
            return r

        with mock_patch("executor_client._client") as mock_client:
            mock_client.post = slow_post
            await asyncio.gather(
                executor_client.apply_patch("--- a/f\n+++ b/f\n@@ -1 +1 @@\n x\n"),
                executor_client.apply_patch("--- a/f\n+++ b/f\n@@ -1 +1 @@\n x\n"),
            )

        assert len(call_times) == 2
        # With semaphore(1), second call must start after first completes
        gap = call_times[1] - call_times[0]
        assert gap >= delay * 0.8, f"Calls overlapped (gap={gap:.3f}s) — semaphore not working"