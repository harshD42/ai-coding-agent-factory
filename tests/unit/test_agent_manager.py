"""tests/unit/test_agent_manager.py — Agent lifecycle and prompt loading tests."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from agent_manager import Agent, AgentManager, _load_agent_prompt, _validate_task_dag


class TestAgent:
    def test_initial_state(self):
        a = Agent("arch-001", "architect", "sess-1")
        assert a.agent_id  == "arch-001"
        assert a.role      == "architect"
        assert a.session_id == "sess-1"
        assert a.status    == "idle"
        assert a.result    is None
        assert a.error     is None
        assert a._history  == []

    def test_to_dict(self):
        a = Agent("arch-001", "architect", "sess-1")
        d = a.to_dict()
        assert d["agent_id"]   == "arch-001"
        assert d["role"]       == "architect"
        assert d["status"]     == "idle"
        assert "task" in d
        assert "created_at" in d

    def test_to_dict_excludes_history(self):
        a = Agent("arch-001", "architect", "sess-1")
        a._history = [{"role": "user", "content": "secret"}]
        d = a.to_dict()
        assert "_history" not in d
        assert "history" not in d


class TestLoadAgentPrompt:
    def test_fallback_when_file_missing(self):
        prompt = _load_agent_prompt("nonexistent_role")
        assert "nonexistent_role" in prompt
        assert len(prompt) > 10

    def test_loads_from_file_when_exists(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "coder.md").write_text("# Custom coder prompt")
        with patch("agent_manager.Path") as mock_path:
            mock_p = MagicMock()
            mock_p.exists.return_value = True
            mock_p.read_text.return_value = "# Custom coder prompt"
            mock_path.return_value = mock_p
            prompt = _load_agent_prompt("coder")
            assert "Custom coder prompt" in prompt


class TestValidateTaskDag:
    def test_valid_chain(self):
        tasks = [
            {"id": "t1", "role": "coder",  "desc": "a", "deps": []},
            {"id": "t2", "role": "tester", "desc": "b", "deps": ["t1"]},
        ]
        result = _validate_task_dag(tasks)
        assert len(result) == 2
        assert result[0]["status"] == "pending"

    def test_unknown_role_normalized_to_coder(self):
        tasks = [{"id": "t1", "role": "wizard", "desc": "a", "deps": []}]
        result = _validate_task_dag(tasks)
        assert result[0]["role"] == "coder"

    def test_valid_roles_preserved(self):
        for role in ("architect", "coder", "reviewer", "tester", "documenter"):
            tasks = [{"id": "t1", "role": role, "desc": "a", "deps": []}]
            result = _validate_task_dag(tasks)
            assert result[0]["role"] == role

    def test_deps_with_missing_ref_silently_dropped(self):
        # _validate_task_dag only includes deps whose IDs appear in seen_ids
        # Missing deps are silently dropped (not raise — DAG validation is in task_queue)
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": ["t99"]},
        ]
        result = _validate_task_dag(tasks)
        assert result[0]["deps"] == []  # t99 not seen, dropped

    def test_empty_list(self):
        assert _validate_task_dag([]) == []

    def test_non_dict_items_skipped(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": []},
            "not a dict",
            None,
        ]
        result = _validate_task_dag(tasks)
        assert len(result) == 1

    def test_status_set_to_pending(self):
        tasks = [{"id": "t1", "role": "coder", "desc": "a", "deps": [], "status": "complete"}]
        result = _validate_task_dag(tasks)
        assert result[0]["status"] == "pending"


class TestAgentManagerStatus:
    def setup_method(self):
        mock_mem = MagicMock()
        self.mgr = AgentManager(mock_mem)

    def test_empty_status(self):
        s = self.mgr.get_status()
        assert s["total"]   == 0
        assert s["running"] == 0
        assert s["done"]    == 0
        assert s["failed"]  == 0

    def test_list_agents_empty(self):
        assert self.mgr.list_agents() == []

    def test_get_agent_missing(self):
        assert self.mgr.get_agent("nonexistent") is None

    def test_status_counts(self):
        # Manually inject agents with different statuses
        for status, aid in [("running", "a1"), ("done", "a2"), ("failed", "a3"), ("killed", "a4")]:
            a = Agent(aid, "coder", "sess")
            a.status = status
            self.mgr._agents[aid] = a
        s = self.mgr.get_status()
        assert s["total"]   == 4
        assert s["running"] == 1
        assert s["done"]    == 1
        assert s["failed"]  == 2  # failed + killed both count as failed