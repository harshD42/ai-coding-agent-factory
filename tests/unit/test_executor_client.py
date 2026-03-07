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