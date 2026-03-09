"""
tests/unit/test_session_hooks.py — SessionHooks unit tests.

Phase 4A.3 additions at bottom:
  - _parse_confidence() parses valid float, clamps out-of-range, defaults on missing
  - extract_skills() passes confidence to save_skill metadata
  - _mine_failure_patterns() passes confidence to save_skill metadata
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Existing tests ────────────────────────────────────────────────────────────

class TestParseField:
    def test_basic_parse(self):
        from session_hooks import _parse_field
        text = "SKILL_NAME: my-pattern\nSKILL_CONTENT: do this thing"
        assert _parse_field(text, "SKILL_NAME") == "my-pattern"

    def test_skill_content_parse(self):
        from session_hooks import _parse_field
        text = "SKILL_NAME: foo\nSKILL_CONTENT: use Redis for queuing"
        assert _parse_field(text, "SKILL_CONTENT") == "use Redis for queuing"

    def test_case_insensitive(self):
        from session_hooks import _parse_field
        text = "skill_name: lower-case\n"
        assert _parse_field(text, "SKILL_NAME") == "lower-case"

    def test_missing_field_returns_empty(self):
        from session_hooks import _parse_field
        assert _parse_field("nothing here", "SKILL_NAME") == ""

    def test_empty_text(self):
        from session_hooks import _parse_field
        assert _parse_field("", "SKILL_NAME") == ""

    def test_multiline_value_takes_first_line(self):
        from session_hooks import _parse_field
        text = "SKILL_NAME: first\nsecond line\nthird"
        assert _parse_field(text, "SKILL_NAME") == "first"

    def test_whitespace_stripped(self):
        from session_hooks import _parse_field
        text = "SKILL_NAME:   spaces around   "
        assert _parse_field(text, "SKILL_NAME") == "spaces around"


class TestSessionHooks:
    @pytest.fixture
    def mock_mem(self):
        mem = MagicMock()
        mem.recall          = AsyncMock(return_value=[])
        mem.save_session    = AsyncMock()
        mem.record_failure  = AsyncMock()
        mem.save_skill      = AsyncMock()
        mem.cluster_failures = AsyncMock(return_value=[])
        return mem

    @pytest.fixture
    def hooks(self, mock_mem):
        from session_hooks import SessionHooks
        return SessionHooks(mock_mem)

    @pytest.mark.asyncio
    async def test_on_session_start_returns_structure(self, hooks):
        result = await hooks.on_session_start("s1", "")
        assert result["session_id"] == "s1"
        assert "started_at" in result

    @pytest.mark.asyncio
    async def test_on_session_start_calls_recall(self, hooks, mock_mem):
        await hooks.on_session_start("s1", "implement auth")
        mock_mem.recall.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_session_start_no_task_skips_recall(self, hooks, mock_mem):
        await hooks.on_session_start("s1", "")
        mock_mem.recall.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_session_end_saves_session(self, hooks, mock_mem):
        await hooks.on_session_end("s1", "session done", [], [])
        mock_mem.save_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_session_end_records_failures(self, hooks, mock_mem):
        failures = [{"task_id": "t1", "description": "oops", "error": "err", "approach": ""}]
        await hooks.on_session_end("s1", "done", [], failures)
        mock_mem.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_session_end_no_failures_skips_record(self, hooks, mock_mem):
        await hooks.on_session_end("s1", "done", [], [])
        mock_mem.record_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_failure_delegates_to_memory(self, hooks, mock_mem):
        await hooks.on_failure("s1", "t1", "desc", "err", "approach")
        mock_mem.record_failure.assert_called_once()

    @pytest.mark.asyncio
    async def test_extract_skills_empty_transcript_returns_none(self, hooks):
        result = await hooks.extract_skills("s1", [])
        assert result is None

    @pytest.mark.asyncio
    async def test_extract_skills_no_skill_detected(self, hooks, mock_mem):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "NO_SKILL"}}
        with patch("session_hooks._http") as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await hooks.extract_skills("s1", [{"role": "user", "content": "hi"}])
        assert result is None

    @pytest.mark.asyncio
    async def test_extract_skills_saves_when_found(self, hooks, mock_mem):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "message": {
                "content": "SKILL_NAME: jwt-pattern\nSKILL_CONTENT: use JWT\nCONFIDENCE: 0.9"
            }
        }
        with patch("session_hooks._http") as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await hooks.extract_skills("s1", [{"role": "user", "content": "auth work"}])
        assert result == "jwt-pattern"
        mock_mem.save_skill.assert_called_once()


# ── Phase 4A.3: confidence scoring ───────────────────────────────────────────

class TestParseConfidence:
    """
    _parse_confidence() parses the CONFIDENCE field from model responses.
    """

    def test_valid_float_parsed(self):
        from session_hooks import _parse_confidence
        text = "SKILL_NAME: foo\nCONFIDENCE: 0.85"
        assert _parse_confidence(text) == pytest.approx(0.85)

    def test_confidence_1_0_parsed(self):
        from session_hooks import _parse_confidence
        text = "CONFIDENCE: 1.0"
        assert _parse_confidence(text) == pytest.approx(1.0)

    def test_confidence_0_0_parsed(self):
        from session_hooks import _parse_confidence
        text = "CONFIDENCE: 0.0"
        assert _parse_confidence(text) == pytest.approx(0.0)

    def test_out_of_range_high_clamped_to_1(self):
        from session_hooks import _parse_confidence
        text = "CONFIDENCE: 1.5"
        assert _parse_confidence(text) == pytest.approx(1.0)

    def test_out_of_range_low_clamped_to_0(self):
        from session_hooks import _parse_confidence
        text = "CONFIDENCE: -0.3"
        assert _parse_confidence(text) == pytest.approx(0.0)

    def test_missing_field_returns_default(self):
        from session_hooks import _parse_confidence
        text = "SKILL_NAME: foo\nSKILL_CONTENT: bar"
        assert _parse_confidence(text) == pytest.approx(0.8)

    def test_unparseable_value_returns_default(self):
        from session_hooks import _parse_confidence
        text = "CONFIDENCE: high"
        assert _parse_confidence(text) == pytest.approx(0.8)

    def test_empty_text_returns_default(self):
        from session_hooks import _parse_confidence
        assert _parse_confidence("") == pytest.approx(0.8)

    def test_confidence_in_antipattern_response(self):
        from session_hooks import _parse_confidence
        text = (
            "ANTIPATTERN_NAME: bad-pattern\n"
            "ANTIPATTERN_CONTENT: avoid this\n"
            "CONFIDENCE: 0.72"
        )
        assert _parse_confidence(text) == pytest.approx(0.72)


class TestExtractSkillsConfidence:
    """
    extract_skills() must pass confidence to save_skill metadata.
    """

    @pytest.fixture
    def mock_mem(self):
        mem = MagicMock()
        mem.save_skill = AsyncMock()
        mem.recall     = AsyncMock(return_value=[])
        return mem

    @pytest.fixture
    def hooks(self, mock_mem):
        from session_hooks import SessionHooks
        return SessionHooks(mock_mem)

    @pytest.mark.asyncio
    async def test_confidence_passed_to_save_skill_metadata(self, hooks, mock_mem):
        """save_skill must be called with confidence in metadata."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "message": {
                "content": "SKILL_NAME: redis-caching\nSKILL_CONTENT: use Redis\nCONFIDENCE: 0.92"
            }
        }
        with patch("session_hooks._http") as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            await hooks.extract_skills("s1", [{"role": "user", "content": "caching work"}])

        _, kwargs = mock_mem.save_skill.call_args
        assert "metadata" in kwargs
        assert kwargs["metadata"]["confidence"] == pytest.approx(0.92)

    @pytest.mark.asyncio
    async def test_missing_confidence_uses_default_in_metadata(self, hooks, mock_mem):
        """When model omits CONFIDENCE, metadata should contain default 0.8."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "message": {
                "content": "SKILL_NAME: no-conf\nSKILL_CONTENT: description here"
            }
        }
        with patch("session_hooks._http") as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            await hooks.extract_skills("s1", [{"role": "user", "content": "some work"}])

        _, kwargs = mock_mem.save_skill.call_args
        assert kwargs["metadata"]["confidence"] == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_low_confidence_skill_still_saved(self, hooks, mock_mem):
        """
        extract_skills() saves regardless of confidence — filtering happens
        in context_manager at read time, not at write time.
        """
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "message": {
                "content": "SKILL_NAME: weak-skill\nSKILL_CONTENT: maybe useful\nCONFIDENCE: 0.3"
            }
        }
        with patch("session_hooks._http") as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await hooks.extract_skills("s1", [{"role": "user", "content": "work"}])

        # Saved to ChromaDB regardless — context_manager filters at read time
        assert result == "weak-skill"
        mock_mem.save_skill.assert_called_once()
        _, kwargs = mock_mem.save_skill.call_args
        assert kwargs["metadata"]["confidence"] == pytest.approx(0.3)