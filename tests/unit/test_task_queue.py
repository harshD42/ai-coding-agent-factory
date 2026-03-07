"""tests/unit/test_task_queue.py — DAG validation, ready-task logic, and Phase 2 auto-patch tests."""

import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

from unittest.mock import AsyncMock, MagicMock
from task_queue import _validate_dag, TaskQueue


# ── Shared test fixture ───────────────────────────────────────────────────────

_SIMPLE_DIFF = """\
--- a/hello.py
+++ b/hello.py
@@ -1,4 +1,8 @@
 def hello():
     return "hello"
+
+def multiply(a, b):
+    return a * b
"""


# ── DAG validation ────────────────────────────────────────────────────────────

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


# ── Ready-task logic ──────────────────────────────────────────────────────────

class TestTaskQueueReadyLogic:
    def _make_tasks(self, specs):
        return [
            {"id": tid, "role": "coder", "desc": "x", "deps": deps, "status": status}
            for tid, deps, status in specs
        ]

    @pytest.mark.asyncio
    async def test_no_deps_all_ready(self):
        q = TaskQueue()
        tasks = self._make_tasks([("t1", [], "pending"), ("t2", [], "pending")])
        q._all_tasks = AsyncMock(return_value=tasks)
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 2

    @pytest.mark.asyncio
    async def test_dep_not_complete_blocks_task(self):
        q = TaskQueue()
        tasks = self._make_tasks([("t1", [], "pending"), ("t2", ["t1"], "pending")])
        q._all_tasks = AsyncMock(return_value=tasks)
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 1
        assert ready[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_dep_complete_unblocks_task(self):
        q = TaskQueue()
        tasks = self._make_tasks([("t1", [], "complete"), ("t2", ["t1"], "pending")])
        q._all_tasks = AsyncMock(return_value=tasks)
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 1
        assert ready[0]["id"] == "t2"

    @pytest.mark.asyncio
    async def test_all_complete_nothing_ready(self):
        q = TaskQueue()
        tasks = self._make_tasks([("t1", [], "complete"), ("t2", [], "complete")])
        q._all_tasks = AsyncMock(return_value=tasks)
        assert await q.get_ready_tasks("sess") == []

    @pytest.mark.asyncio
    async def test_failed_task_blocks_dependents(self):
        q = TaskQueue()
        tasks = self._make_tasks([("t1", [], "failed"), ("t2", ["t1"], "pending")])
        q._all_tasks = AsyncMock(return_value=tasks)
        assert await q.get_ready_tasks("sess") == []

    @pytest.mark.asyncio
    async def test_running_tasks_not_returned(self):
        q = TaskQueue()
        tasks = self._make_tasks([("t1", [], "running"), ("t2", [], "pending")])
        q._all_tasks = AsyncMock(return_value=tasks)
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 1
        assert ready[0]["id"] == "t2"

    @pytest.mark.asyncio
    async def test_diamond_second_wave_ready(self):
        q = TaskQueue()
        tasks = self._make_tasks([
            ("t1", [],          "complete"),
            ("t2", ["t1"],      "complete"),
            ("t3", ["t1"],      "complete"),
            ("t4", ["t2","t3"], "pending"),
        ])
        q._all_tasks = AsyncMock(return_value=tasks)
        ready = await q.get_ready_tasks("sess")
        assert len(ready) == 1
        assert ready[0]["id"] == "t4"


# ── Phase 2 — Step 2.1: Auto-patch ───────────────────────────────────────────

def _make_tq():
    tq = TaskQueue()
    tq._redis = AsyncMock()
    return tq


def _make_pq():
    pq = MagicMock()
    pq.enqueue = AsyncMock(return_value=MagicMock(to_dict=lambda: {"patch_id": "p1"}))
    return pq


class TestAutoApplyPatches:
    @pytest.mark.asyncio
    async def test_no_patch_queue_skips(self):
        tq   = _make_tq()
        task = {"id": "t1", "role": "coder", "desc": "x"}
        assert await tq._auto_apply_patches("s", task, f"```diff\n{_SIMPLE_DIFF}\n```") == []

    @pytest.mark.asyncio
    async def test_non_patch_role_skips(self):
        tq = _make_tq()
        tq.set_patch_queue(_make_pq())
        task = {"id": "t1", "role": "architect", "desc": "x"}
        assert await tq._auto_apply_patches("s", task, f"```diff\n{_SIMPLE_DIFF}\n```") == []

    @pytest.mark.asyncio
    async def test_no_diff_skips(self):
        tq = _make_tq()
        tq.set_patch_queue(_make_pq())
        task = {"id": "t1", "role": "coder", "desc": "x"}
        assert await tq._auto_apply_patches("s", task, "no diff here") == []

    @pytest.mark.asyncio
    async def test_enqueues_diff(self):
        tq = _make_tq()
        pq = _make_pq()
        tq.set_patch_queue(pq)
        task   = {"id": "t1", "role": "coder", "desc": "x"}
        result = await tq._auto_apply_patches("s", task, f"```diff\n{_SIMPLE_DIFF}\n```")
        assert len(result) == 1
        pq.enqueue.assert_awaited_once()
        # enqueue uses keyword args — check kwargs not positional args
        call_kwargs = pq.enqueue.call_args.kwargs
        assert "@@" in call_kwargs.get("diff", "")

    @pytest.mark.asyncio
    async def test_enqueue_error_swallowed(self):
        tq = _make_tq()
        pq = _make_pq()
        pq.enqueue = AsyncMock(side_effect=RuntimeError("boom"))
        tq.set_patch_queue(pq)
        task   = {"id": "t1", "role": "coder", "desc": "x"}
        result = await tq._auto_apply_patches("s", task, f"```diff\n{_SIMPLE_DIFF}\n```")
        assert "error" in result[0]

    @pytest.mark.asyncio
    async def test_tester_role_triggers(self):
        tq = _make_tq()
        pq = _make_pq()
        tq.set_patch_queue(pq)
        task   = {"id": "t2", "role": "tester", "desc": "fix"}
        result = await tq._auto_apply_patches("s", task, f"```diff\n{_SIMPLE_DIFF}\n```")
        assert len(result) == 1

    def test_set_patch_queue(self):
        tq = TaskQueue()
        pq = _make_pq()
        tq.set_patch_queue(pq)
        assert tq._patch_queue is pq