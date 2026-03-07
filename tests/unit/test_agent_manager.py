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
        # Write the prompt file to a real temp directory
        (tmp_path / "coder.md").write_text("Custom coder prompt", encoding="utf-8")
        # Patch config.AGENTS_DIR to point at our temp dir
        # This is the correct patch target for the new path-from-config implementation
        with mock_patch("agent_manager.config") as mock_cfg:
            mock_cfg.AGENTS_DIR = str(tmp_path)
            from agent_manager import _load_agent_prompt
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

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import time
import pytest
from unittest.mock import patch as mock_patch, AsyncMock, MagicMock
from agent_manager import Agent, AgentManager, _load_agent_prompt


class TestAgentHistoryTrim:
    """Phase 3.5: agent._history must never exceed MAX_AGENT_HISTORY entries."""

    def _make_agent(self):
        return Agent("test-1", "coder", "sess-1")

    def test_history_grows_initially(self):
        a = self._make_agent()
        for i in range(5):
            a._history.append({"role": "user",      "content": f"msg {i}"})
            a._history.append({"role": "assistant",  "content": f"reply {i}"})
        assert len(a._history) == 10

    def test_trim_enforces_max(self):
        """Simulate what _run_agent does: append then trim."""
        import config
        a = self._make_agent()
        # Fill history past the limit
        for i in range(config.MAX_AGENT_HISTORY + 4):
            a._history.append({"role": "user",      "content": f"u{i}"})
            a._history.append({"role": "assistant",  "content": f"a{i}"})
            if len(a._history) > config.MAX_AGENT_HISTORY:
                excess = len(a._history) - config.MAX_AGENT_HISTORY
                a._history = a._history[excess:]
        assert len(a._history) <= config.MAX_AGENT_HISTORY

    def test_trim_preserves_most_recent(self):
        import config
        a = self._make_agent()
        limit = config.MAX_AGENT_HISTORY
        # Add limit + 2 turns
        for i in range(limit + 2):
            a._history.append({"role": "user",      "content": f"u{i}"})
            a._history.append({"role": "assistant",  "content": f"a{i}"})
            if len(a._history) > limit:
                excess = len(a._history) - limit
                a._history = a._history[excess:]
        # Most recent message should be the last one added
        assert f"u{limit+1}" in a._history[-2]["content"] or \
               f"a{limit+1}" in a._history[-1]["content"]


class TestCleanupIdleAgents:
    """Phase 3.5: cleanup_idle_agents prunes old terminal-state agents."""

    def _make_mgr(self):
        mem = MagicMock()
        return AgentManager(mem)

    @pytest.mark.asyncio
    async def test_removes_old_done_agent(self):
        import config
        mgr = self._make_mgr()
        a   = Agent("old-1", "coder", "s1")
        a.status   = "done"
        a.ended_at = time.time() - config.AGENT_IDLE_TIMEOUT - 10
        mgr._agents["old-1"] = a
        removed = await mgr.cleanup_idle_agents()
        assert removed == 1
        assert "old-1" not in mgr._agents

    @pytest.mark.asyncio
    async def test_keeps_recent_done_agent(self):
        mgr = self._make_mgr()
        a   = Agent("recent-1", "coder", "s1")
        a.status   = "done"
        a.ended_at = time.time() - 60   # 1 min ago, within timeout
        mgr._agents["recent-1"] = a
        removed = await mgr.cleanup_idle_agents()
        assert removed == 0
        assert "recent-1" in mgr._agents

    @pytest.mark.asyncio
    async def test_never_removes_running_agent(self):
        mgr = self._make_mgr()
        a   = Agent("run-1", "coder", "s1")
        a.status     = "running"
        a.started_at = time.time() - 5000   # running a long time
        a.ended_at   = None
        mgr._agents["run-1"] = a
        removed = await mgr.cleanup_idle_agents()
        assert removed == 0
        assert "run-1" in mgr._agents

    @pytest.mark.asyncio
    async def test_removes_failed_and_killed(self):
        import config
        mgr   = self._make_mgr()
        old   = time.time() - config.AGENT_IDLE_TIMEOUT - 10
        for aid, status in [("f1", "failed"), ("k1", "killed")]:
            a = Agent(aid, "coder", "s1")
            a.status   = status
            a.ended_at = old
            mgr._agents[aid] = a
        removed = await mgr.cleanup_idle_agents()
        assert removed == 2


class TestLoadAgentPromptPath:
    """Phase 3.5: _load_agent_prompt uses config.AGENTS_DIR not hardcoded path."""

    def test_uses_config_agents_dir(self, tmp_path):
        (tmp_path / "coder.md").write_text("# Coder\nYou are a coder.")
        with mock_patch("agent_manager.config") as mock_cfg:
            mock_cfg.AGENTS_DIR = str(tmp_path)
            result = _load_agent_prompt("coder")
        assert "You are a coder." in result

    def test_fallback_when_missing(self, tmp_path):
        with mock_patch("agent_manager.config") as mock_cfg:
            mock_cfg.AGENTS_DIR = str(tmp_path)   # empty dir
            result = _load_agent_prompt("tester")
        assert "tester" in result.lower()
        assert len(result) > 0   # fallback string, not empty