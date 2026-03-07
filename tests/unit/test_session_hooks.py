"""tests/unit/test_session_hooks.py — Session hooks and skill extraction tests."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from session_hooks import SessionHooks, _parse_field


class TestParseField:
    def test_basic_parse(self):
        text = "SKILL_NAME: Redis Rate Limiter Pattern"
        assert _parse_field(text, "SKILL_NAME") == "Redis Rate Limiter Pattern"

    def test_skill_content_parse(self):
        text = "SKILL_NAME: foo\nSKILL_CONTENT: Use sorted sets for sliding windows."
        assert _parse_field(text, "SKILL_CONTENT") == "Use sorted sets for sliding windows."

    def test_case_insensitive(self):
        text = "skill_name: my skill"
        assert _parse_field(text, "SKILL_NAME") == "my skill"

    def test_missing_field_returns_empty(self):
        text = "SKILL_NAME: something"
        assert _parse_field(text, "SKILL_CONTENT") == ""

    def test_empty_text(self):
        assert _parse_field("", "SKILL_NAME") == ""

    def test_multiline_value_takes_first_line(self):
        text = "SKILL_NAME: first line\nmore content here"
        result = _parse_field(text, "SKILL_NAME")
        assert result == "first line"

    def test_whitespace_stripped(self):
        text = "SKILL_NAME:   padded value   "
        assert _parse_field(text, "SKILL_NAME") == "padded value"


class TestSessionHooks:
    def _make_hooks(self):
        mock_mem = MagicMock()
        mock_mem.recall       = AsyncMock(return_value=[])
        mock_mem.save_session = AsyncMock()
        mock_mem.record_failure = AsyncMock()
        mock_mem.save_skill   = AsyncMock()
        return SessionHooks(mock_mem), mock_mem

    @pytest.mark.asyncio
    async def test_on_session_start_returns_structure(self):
        hooks, _ = self._make_hooks()
        result = await hooks.on_session_start("sess-1", task="build auth")
        assert result["session_id"] == "sess-1"
        assert "past_context" in result
        assert "started_at" in result

    @pytest.mark.asyncio
    async def test_on_session_start_calls_recall(self):
        hooks, mock_mem = self._make_hooks()
        await hooks.on_session_start("sess-1", task="build auth")
        mock_mem.recall.assert_called_once_with("build auth", k=3)

    @pytest.mark.asyncio
    async def test_on_session_start_no_task_skips_recall(self):
        hooks, mock_mem = self._make_hooks()
        await hooks.on_session_start("sess-1", task="")
        mock_mem.recall.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_session_end_saves_session(self):
        hooks, mock_mem = self._make_hooks()
        await hooks.on_session_end("sess-1", summary="Built a rate limiter")
        mock_mem.save_session.assert_called_once()
        call_args = mock_mem.save_session.call_args
        assert call_args.kwargs["session_id"] == "sess-1"
        assert "Built a rate limiter" in call_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_on_session_end_records_failures(self):
        hooks, mock_mem = self._make_hooks()
        failures = [
            {"task_id": "t1", "description": "write auth", "error": "timeout", "approach": "async"},
        ]
        await hooks.on_session_end("sess-1", summary="done", failures=failures)
        mock_mem.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_session_end_no_failures_skips_record(self):
        hooks, mock_mem = self._make_hooks()
        await hooks.on_session_end("sess-1", summary="done")
        mock_mem.record_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_failure_delegates_to_memory(self):
        hooks, mock_mem = self._make_hooks()
        await hooks.on_failure("sess-1", "t1", "write fn", "timeout", "approach A")
        mock_mem.record_failure.assert_called_once_with(
            session_id="sess-1",
            task_id="t1",
            description="write fn",
            error="timeout",
            approach="approach A",
        )

    @pytest.mark.asyncio
    async def test_extract_skills_empty_transcript_returns_none(self):
        hooks, _ = self._make_hooks()
        result = await hooks.extract_skills("sess-1", [])
        assert result is None

    @pytest.mark.asyncio
    async def test_extract_skills_no_skill_detected(self):
        hooks, _ = self._make_hooks()
        transcript = [{"role": "user", "content": "hello"}]
        with patch("session_hooks._http") as mock_http:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"message": {"content": "NO_SKILL"}}
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await hooks.extract_skills("sess-1", transcript)
        assert result is None

    @pytest.mark.asyncio
    async def test_extract_skills_saves_when_found(self):
        hooks, mock_mem = self._make_hooks()
        transcript = [{"role": "architect", "content": "Use Redis sorted sets for rate limiting."}]
        model_response = (
            "SKILL_NAME: Redis Rate Limiting\n"
            "SKILL_CONTENT: Use sorted sets with ZADD/ZRANGEBYSCORE for sliding window rate limiting."
        )
        with patch("session_hooks._http") as mock_http:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"message": {"content": model_response}}
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await hooks.extract_skills("sess-1", transcript)
        assert result == "Redis Rate Limiting"
        mock_mem.save_skill.assert_called_once()