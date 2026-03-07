"""tests/unit/test_debate_engine.py — Debate engine logic tests."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from debate_engine import DebateEngine, _signals_approval, APPROVE_SIGNALS


class TestSignalsApproval:
    def test_approve_keyword(self):
        assert _signals_approval("APPROVE — this looks good") is True

    def test_approved_keyword(self):
        assert _signals_approval("The plan is approved.") is True

    def test_lgtm(self):
        assert _signals_approval("LGTM, ship it") is True

    def test_looks_good(self):
        assert _signals_approval("This looks good to me.") is True

    def test_no_further_changes(self):
        assert _signals_approval("No further changes needed.") is True

    def test_case_insensitive(self):
        assert _signals_approval("approve") is True
        assert _signals_approval("APPROVE") is True
        assert _signals_approval("Approve") is True

    def test_rejection_returns_false(self):
        assert _signals_approval("I have concerns about the architecture.") is False
        assert _signals_approval("This needs major revision.") is False
        assert _signals_approval("Missing error handling.") is False

    def test_empty_string(self):
        assert _signals_approval("") is False

    def test_approve_in_context(self):
        # "approve" embedded in a larger critique should still trigger
        long_text = "After reviewing all concerns, I APPROVE this plan with minor suggestions."
        assert _signals_approval(long_text) is True


class TestDebateEngine:
    def _make_engine(self):
        mock_mgr = MagicMock()
        return DebateEngine(mock_mgr), mock_mgr

    @pytest.mark.asyncio
    async def test_early_consensus_stops_debate(self):
        engine, mock_mgr = self._make_engine()

        call_count = 0
        async def fake_spawn(role, task, session_id):
            nonlocal call_count
            call_count += 1
            if role == "reviewer":
                return {"status": "done", "result": "APPROVE — looks great"}
            return {"status": "done", "result": "Here is my plan: use Redis."}

        mock_mgr.spawn_and_run = fake_spawn

        result = await engine.run(
            topic="Design a cache", session_id="test-1", max_rounds=3
        )
        assert result["consensus"] is True
        assert result["rounds"] == 1
        # architect (init) + reviewer = 2 calls, no revision needed
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_max_rounds_enforced(self):
        engine, mock_mgr = self._make_engine()

        async def fake_spawn(role, task, session_id):
            if role == "reviewer":
                return {"status": "done", "result": "I have many concerns. Needs revision."}
            return {"status": "done", "result": "Here is a revised plan."}

        mock_mgr.spawn_and_run = fake_spawn

        result = await engine.run(
            topic="Design a cache", session_id="test-2", max_rounds=2
        )
        assert result["consensus"] is False
        assert result["rounds"] == 2

    @pytest.mark.asyncio
    async def test_transcript_contains_all_entries(self):
        engine, mock_mgr = self._make_engine()

        async def fake_spawn(role, task, session_id):
            if role == "reviewer":
                return {"status": "done", "result": "Needs work."}
            return {"status": "done", "result": "Updated plan."}

        mock_mgr.spawn_and_run = fake_spawn

        result = await engine.run(
            topic="Design a cache", session_id="test-3", max_rounds=1
        )
        roles  = [t["role"] for t in result["transcript"]]
        phases = [t["phase"] for t in result["transcript"]]
        assert "architect" in roles
        assert "reviewer" in roles
        assert "initial_plan" in phases
        assert "critique" in phases

    @pytest.mark.asyncio
    async def test_initial_plan_skipped_when_provided(self):
        engine, mock_mgr = self._make_engine()

        spawn_calls = []
        async def fake_spawn(role, task, session_id):
            spawn_calls.append(role)
            if role == "reviewer":
                return {"status": "done", "result": "APPROVE"}
            return {"status": "done", "result": "Revised."}

        mock_mgr.spawn_and_run = fake_spawn

        result = await engine.run(
            topic="topic",
            session_id="test-4",
            initial_plan="I already have a plan.",
            max_rounds=1,
        )
        # Should NOT call architect for initial plan
        assert "architect" not in spawn_calls
        assert result["consensus"] is True

    @pytest.mark.asyncio
    async def test_final_plan_is_latest_revision(self):
        engine, mock_mgr = self._make_engine()
        revision_text = "This is the revised plan v2."

        call_count = 0
        async def fake_spawn(role, task, session_id):
            nonlocal call_count
            call_count += 1
            if role == "reviewer":
                if call_count <= 2:
                    return {"status": "done", "result": "Needs improvement."}
                return {"status": "done", "result": "APPROVE"}
            return {"status": "done", "result": revision_text}

        mock_mgr.spawn_and_run = fake_spawn

        result = await engine.run(
            topic="topic", session_id="test-5", max_rounds=3
        )
        assert result["final_plan"] == revision_text