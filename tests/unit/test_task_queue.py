"""tests/unit/test_task_queue.py — DAG validation and ready-task logic tests."""

import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

from task_queue import _validate_dag, TaskQueue


class TestValidateDag:
    def test_linear_chain_passes(self):
        tasks = [
            {"id": "t1", "role": "coder",  "desc": "a", "deps": []},
            {"id": "t2", "role": "tester", "desc": "b", "deps": ["t1"]},
            {"id": "t3", "role": "coder",  "desc": "c", "deps": ["t2"]},
        ]
        _validate_dag(tasks)

    def test_parallel_tasks_pass(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": []},
            {"id": "t2", "role": "coder", "desc": "b", "deps": []},
            {"id": "t3", "role": "coder", "desc": "c", "deps": ["t1", "t2"]},
        ]
        _validate_dag(tasks)

    def test_no_deps_passes(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": []},
            {"id": "t2", "role": "coder", "desc": "b", "deps": []},
        ]
        _validate_dag(tasks)

    def test_cycle_rejected(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": ["t2"]},
            {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
        ]
        with pytest.raises(ValueError, match="cycle"):
            _validate_dag(tasks)

    def test_self_reference_rejected(self):
        with pytest.raises(ValueError, match="cycle"):
            _validate_dag([{"id": "t1", "role": "coder", "desc": "a", "deps": ["t1"]}])

    def test_missing_dep_rejected(self):
        with pytest.raises(ValueError, match="t99"):
            _validate_dag([{"id": "t1", "role": "coder", "desc": "a", "deps": ["t99"]}])

    def test_three_way_cycle_rejected(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": ["t3"]},
            {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
            {"id": "t3", "role": "coder", "desc": "c", "deps": ["t2"]},
        ]
        with pytest.raises(ValueError, match="cycle"):
            _validate_dag(tasks)

    def test_empty_task_list_passes(self):
        _validate_dag([])

    def test_diamond_dependency_passes(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": []},
            {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
            {"id": "t3", "role": "coder", "desc": "c", "deps": ["t1"]},
            {"id": "t4", "role": "coder", "desc": "d", "deps": ["t2", "t3"]},
        ]
        _validate_dag(tasks)


class TestTaskQueueReadyLogic:
    """Test get_ready_tasks logic using mocked Redis."""

    def _make_tasks(self, specs):
        """specs: list of (id, deps, status)"""
        return [
            {"id": tid, "role": "coder", "desc": "x", "deps": deps, "status": status}
            for tid, deps, status in specs
        ]

    @pytest.mark.asyncio
    async def test_no_deps_all_ready(self):
        q = TaskQueue()
        tasks = self._make_tasks([
            ("t1", [], "pending"),
            ("t2", [], "pending"),
        ])

        async def fake_all_tasks(sid):
            return tasks

        q._all_tasks = fake_all_tasks
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 2

    @pytest.mark.asyncio
    async def test_dep_not_complete_blocks_task(self):
        q = TaskQueue()
        tasks = self._make_tasks([
            ("t1", [],     "pending"),
            ("t2", ["t1"], "pending"),
        ])

        async def fake_all_tasks(sid):
            return tasks

        q._all_tasks = fake_all_tasks
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 1
        assert ready[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_dep_complete_unblocks_task(self):
        q = TaskQueue()
        tasks = self._make_tasks([
            ("t1", [],     "complete"),
            ("t2", ["t1"], "pending"),
        ])

        async def fake_all_tasks(sid):
            return tasks

        q._all_tasks = fake_all_tasks
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 1
        assert ready[0]["id"] == "t2"

    @pytest.mark.asyncio
    async def test_all_complete_nothing_ready(self):
        q = TaskQueue()
        tasks = self._make_tasks([
            ("t1", [], "complete"),
            ("t2", [], "complete"),
        ])

        async def fake_all_tasks(sid):
            return tasks

        q._all_tasks = fake_all_tasks
        ready = await q.get_ready_tasks("sess")
        assert ready == []

    @pytest.mark.asyncio
    async def test_failed_task_blocks_dependents(self):
        q = TaskQueue()
        tasks = self._make_tasks([
            ("t1", [],     "failed"),
            ("t2", ["t1"], "pending"),
        ])

        async def fake_all_tasks(sid):
            return tasks

        q._all_tasks = fake_all_tasks
        ready = await q.get_ready_tasks("sess")
        # t2 depends on t1 which failed — t1 is not "complete" so t2 not ready
        assert ready == []

    @pytest.mark.asyncio
    async def test_running_tasks_not_returned(self):
        q = TaskQueue()
        tasks = self._make_tasks([
            ("t1", [], "running"),
            ("t2", [], "pending"),
        ])

        async def fake_all_tasks(sid):
            return tasks

        q._all_tasks = fake_all_tasks
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 1
        assert ready[0]["id"] == "t2"

    @pytest.mark.asyncio
    async def test_diamond_second_wave_ready(self):
        q = TaskQueue()
        tasks = self._make_tasks([
            ("t1", [],         "complete"),
            ("t2", ["t1"],     "complete"),
            ("t3", ["t1"],     "complete"),
            ("t4", ["t2","t3"],"pending"),
        ])

        async def fake_all_tasks(sid):
            return tasks

        q._all_tasks = fake_all_tasks
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 1
        assert ready[0]["id"] == "t4"
"""tests/unit/test_task_queue.py — DAG validation unit tests."""

import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

from task_queue import _validate_dag


class TestValidateDag:
    def test_linear_chain_passes(self):
        tasks = [
            {"id": "t1", "role": "coder",  "desc": "a", "deps": []},
            {"id": "t2", "role": "tester", "desc": "b", "deps": ["t1"]},
            {"id": "t3", "role": "coder",  "desc": "c", "deps": ["t2"]},
        ]
        _validate_dag(tasks)  # should not raise

    def test_parallel_tasks_pass(self):
        tasks = [
            {"id": "t1", "role": "coder",  "desc": "a", "deps": []},
            {"id": "t2", "role": "coder",  "desc": "b", "deps": []},
            {"id": "t3", "role": "coder",  "desc": "c", "deps": ["t1", "t2"]},
        ]
        _validate_dag(tasks)

    def test_no_deps_passes(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": []},
            {"id": "t2", "role": "coder", "desc": "b", "deps": []},
        ]
        _validate_dag(tasks)

    def test_cycle_rejected(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": ["t2"]},
            {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
        ]
        with pytest.raises(ValueError, match="cycle"):
            _validate_dag(tasks)

    def test_self_reference_rejected(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": ["t1"]},
        ]
        with pytest.raises(ValueError, match="cycle"):
            _validate_dag(tasks)

    def test_missing_dep_rejected(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": ["t99"]},
        ]
        with pytest.raises(ValueError, match="t99"):
            _validate_dag(tasks)

    def test_three_way_cycle_rejected(self):
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": ["t3"]},
            {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
            {"id": "t3", "role": "coder", "desc": "c", "deps": ["t2"]},
        ]
        with pytest.raises(ValueError, match="cycle"):
            _validate_dag(tasks)

    def test_empty_task_list_passes(self):
        _validate_dag([])  # should not raise

    def test_diamond_dependency_passes(self):
        # t1 → t2, t1 → t3, t2+t3 → t4 (valid diamond)
        tasks = [
            {"id": "t1", "role": "coder", "desc": "a", "deps": []},
            {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
            {"id": "t3", "role": "coder", "desc": "c", "deps": ["t1"]},
            {"id": "t4", "role": "coder", "desc": "d", "deps": ["t2", "t3"]},
        ]
        _validate_dag(tasks)