"""
tests/unit/test_task_queue.py — DAG validation, ready-task logic, auto-patch.

Phase 4A.2 additions at bottom:
  - Task lease acquisition and release
  - Duplicate execution prevention via SETNX
  - Lease cleanup in load_plan
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import config


# ── DAG validation ─────────────────────────────────────────────────────────────

class TestValidateDag:
    def _dag(self, tasks):
        from task_queue import _validate_dag
        return _validate_dag(tasks)

    def test_linear_chain_passes(self):
        self._dag([
            {"id": "t1", "role": "coder",  "desc": "a", "deps": []},
            {"id": "t2", "role": "tester", "desc": "b", "deps": ["t1"]},
        ])

    def test_parallel_tasks_pass(self):
        self._dag([
            {"id": "t1", "role": "coder", "desc": "a", "deps": []},
            {"id": "t2", "role": "coder", "desc": "b", "deps": []},
        ])

    def test_no_deps_passes(self):
        self._dag([{"id": "t1", "role": "coder", "desc": "a", "deps": []}])

    def test_cycle_rejected(self):
        with pytest.raises(ValueError, match="cycle"):
            self._dag([
                {"id": "t1", "role": "coder", "desc": "a", "deps": ["t2"]},
                {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
            ])

    def test_self_reference_rejected(self):
        with pytest.raises(ValueError):
            self._dag([{"id": "t1", "role": "coder", "desc": "a", "deps": ["t1"]}])

    def test_missing_dep_rejected(self):
        with pytest.raises(ValueError, match="doesn't exist"):
            self._dag([{"id": "t1", "role": "coder", "desc": "a", "deps": ["ghost"]}])

    def test_three_way_cycle_rejected(self):
        with pytest.raises(ValueError, match="cycle"):
            self._dag([
                {"id": "t1", "role": "coder", "desc": "a", "deps": ["t3"]},
                {"id": "t2", "role": "coder", "desc": "b", "deps": ["t1"]},
                {"id": "t3", "role": "coder", "desc": "c", "deps": ["t2"]},
            ])

    def test_empty_task_list_passes(self):
        self._dag([])

    def test_diamond_dependency_passes(self):
        self._dag([
            {"id": "t1", "role": "coder",  "desc": "a", "deps": []},
            {"id": "t2", "role": "coder",  "desc": "b", "deps": ["t1"]},
            {"id": "t3", "role": "coder",  "desc": "c", "deps": ["t1"]},
            {"id": "t4", "role": "tester", "desc": "d", "deps": ["t2", "t3"]},
        ])


# ── Ready task logic ───────────────────────────────────────────────────────────

class TestTaskQueueReadyLogic:
    """
    Tests for get_ready_tasks() using a mock Redis backend.
    We bypass Redis by directly stubbing _all_tasks().
    """

    def _make_queue_with_tasks(self, tasks):
        from task_queue import TaskQueue
        tq = TaskQueue()
        tq._all_tasks = AsyncMock(return_value=tasks)
        return tq

    @pytest.mark.asyncio
    async def test_no_deps_all_ready(self):
        tq = self._make_queue_with_tasks([
            {"id": "t1", "role": "coder", "desc": "a", "deps": [], "status": "pending"},
            {"id": "t2", "role": "coder", "desc": "b", "deps": [], "status": "pending"},
        ])
        ready = await tq.get_ready_tasks("s1")
        assert {t["id"] for t in ready} == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_dep_not_complete_blocks_task(self):
        tq = self._make_queue_with_tasks([
            {"id": "t1", "role": "coder",  "desc": "a", "deps": [],     "status": "running"},
            {"id": "t2", "role": "tester", "desc": "b", "deps": ["t1"], "status": "pending"},
        ])
        ready = await tq.get_ready_tasks("s1")
        assert [t["id"] for t in ready] == []

    @pytest.mark.asyncio
    async def test_dep_complete_unblocks_task(self):
        tq = self._make_queue_with_tasks([
            {"id": "t1", "role": "coder",  "desc": "a", "deps": [],     "status": "complete"},
            {"id": "t2", "role": "tester", "desc": "b", "deps": ["t1"], "status": "pending"},
        ])
        ready = await tq.get_ready_tasks("s1")
        assert [t["id"] for t in ready] == ["t2"]

    @pytest.mark.asyncio
    async def test_all_complete_nothing_ready(self):
        tq = self._make_queue_with_tasks([
            {"id": "t1", "role": "coder", "desc": "a", "deps": [], "status": "complete"},
        ])
        ready = await tq.get_ready_tasks("s1")
        assert ready == []

    @pytest.mark.asyncio
    async def test_failed_task_blocks_dependents(self):
        tq = self._make_queue_with_tasks([
            {"id": "t1", "role": "coder",  "desc": "a", "deps": [],     "status": "failed"},
            {"id": "t2", "role": "tester", "desc": "b", "deps": ["t1"], "status": "pending"},
        ])
        ready = await tq.get_ready_tasks("s1")
        assert ready == []

    @pytest.mark.asyncio
    async def test_running_tasks_not_returned(self):
        tq = self._make_queue_with_tasks([
            {"id": "t1", "role": "coder", "desc": "a", "deps": [], "status": "running"},
        ])
        ready = await tq.get_ready_tasks("s1")
        assert ready == []

    @pytest.mark.asyncio
    async def test_diamond_second_wave_ready(self):
        tq = self._make_queue_with_tasks([
            {"id": "t1", "role": "coder", "deps": [], "status": "complete", "desc": "a"},
            {"id": "t2", "role": "coder", "deps": ["t1"], "status": "pending", "desc": "b"},
            {"id": "t3", "role": "coder", "deps": ["t1"], "status": "pending", "desc": "c"},
            {"id": "t4", "role": "coder", "deps": ["t2", "t3"], "status": "pending", "desc": "d"},
        ])
        ready = await tq.get_ready_tasks("s1")
        assert {t["id"] for t in ready} == {"t2", "t3"}


# ── Auto-apply patches ─────────────────────────────────────────────────────────

class TestAutoApplyPatches:
    def _make_queue(self, patch_queue=None):
        from task_queue import TaskQueue
        tq = TaskQueue()
        tq._patch_queue = patch_queue
        return tq

    @pytest.mark.asyncio
    async def test_no_patch_queue_skips(self):
        tq = self._make_queue()
        res = await tq._auto_apply_patches("s1", {"id": "t1", "role": "coder"}, "diff content")
        assert res == []

    @pytest.mark.asyncio
    async def test_non_patch_role_skips(self):
        tq = self._make_queue(patch_queue=MagicMock())
        res = await tq._auto_apply_patches("s1", {"id": "t1", "role": "architect"}, "diff")
        assert res == []

    @pytest.mark.asyncio
    async def test_no_diff_skips(self):
        tq = self._make_queue(patch_queue=MagicMock())
        res = await tq._auto_apply_patches("s1", {"id": "t1", "role": "coder"}, "no diff here")
        assert res == []

    @pytest.mark.asyncio
    async def test_enqueues_diff(self):
        pq   = MagicMock()
        mock_patch = MagicMock()
        mock_patch.to_dict.return_value = {"patch_id": "p1"}
        pq.enqueue = AsyncMock(return_value=mock_patch)
        tq = self._make_queue(patch_queue=pq)
        diff_output = (
            "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        res = await tq._auto_apply_patches("s1", {"id": "t1", "role": "coder"}, diff_output)
        assert len(res) == 1
        pq.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_enqueue_error_swallowed(self):
        pq = MagicMock()
        pq.enqueue = AsyncMock(side_effect=Exception("queue full"))
        tq = self._make_queue(patch_queue=pq)
        diff_output = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        res = await tq._auto_apply_patches("s1", {"id": "t1", "role": "coder"}, diff_output)
        assert res[0]["error"] == "queue full"

    @pytest.mark.asyncio
    async def test_tester_role_triggers(self):
        pq   = MagicMock()
        mock_patch = MagicMock()
        mock_patch.to_dict.return_value = {"patch_id": "p2"}
        pq.enqueue = AsyncMock(return_value=mock_patch)
        tq = self._make_queue(patch_queue=pq)
        diff_output = "--- a/test_foo.py\n+++ b/test_foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        res = await tq._auto_apply_patches("s1", {"id": "t1", "role": "tester"}, diff_output)
        assert len(res) == 1

    def test_set_patch_queue(self):
        from task_queue import TaskQueue
        tq = TaskQueue()
        pq = MagicMock()
        tq.set_patch_queue(pq)
        assert tq._patch_queue is pq


# ── Phase 4A.2: Task leasing ──────────────────────────────────────────────────

class TestTaskLeasing:
    """
    Tests for Redis SETNX-based task leasing added in Phase 4A.2.
    Prevents duplicate execution when orchestrator restarts mid-session.
    """

    def _make_queue_with_mock_redis(self):
        from task_queue import TaskQueue
        tq = TaskQueue()
        tq._redis = MagicMock()
        return tq

    @pytest.mark.asyncio
    async def test_acquire_lease_returns_true_on_setnx_success(self):
        tq = self._make_queue_with_mock_redis()
        tq._redis.set = AsyncMock(return_value=True)
        result = await tq._acquire_task_lease("s1", "t1", "worker-abc")
        assert result is True

    @pytest.mark.asyncio
    async def test_acquire_lease_returns_false_when_already_held(self):
        """SETNX returns None when key already exists."""
        tq = self._make_queue_with_mock_redis()
        tq._redis.set = AsyncMock(return_value=None)
        result = await tq._acquire_task_lease("s1", "t1", "worker-xyz")
        assert result is False

    @pytest.mark.asyncio
    async def test_acquire_lease_uses_correct_key_format(self):
        tq = self._make_queue_with_mock_redis()
        tq._redis.set = AsyncMock(return_value=True)
        await tq._acquire_task_lease("sess-1", "task-2", "worker-1")
        tq._redis.set.assert_called_once_with(
            "task:sess-1:task-2:lease",
            "worker-1",
            nx=True,
            ex=config.TASK_LEASE_TTL,
        )

    @pytest.mark.asyncio
    async def test_release_lease_deletes_key(self):
        tq = self._make_queue_with_mock_redis()
        tq._redis.delete = AsyncMock()
        await tq._release_task_lease("sess-1", "task-2")
        tq._redis.delete.assert_called_once_with("task:sess-1:task-2:lease")

    @pytest.mark.asyncio
    async def test_lease_key_helper_format(self):
        from task_queue import TaskQueue
        tq = TaskQueue()
        assert tq._lease_key("my-session", "t99") == "task:my-session:t99:lease"

    @pytest.mark.asyncio
    async def test_run_single_task_skips_when_lease_held(self):
        """
        If _acquire_task_lease returns False, _run_single_task must skip
        the task without calling agent_mgr.spawn_and_run.
        """
        from task_queue import TaskQueue
        tq      = TaskQueue()
        tq._redis = MagicMock()
        tq._redis.set    = AsyncMock(return_value=None)   # lease already held
        tq._redis.delete = AsyncMock()
        tq._patch_queue  = None

        agent_mgr = MagicMock()
        agent_mgr.spawn_and_run = AsyncMock()

        task   = {"id": "t1", "role": "coder", "desc": "do something", "deps": [], "status": "running"}
        result = await tq._run_single_task("s1", task, agent_mgr)

        assert result["status"] == "skipped"
        agent_mgr.spawn_and_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_single_task_releases_lease_on_success(self):
        """Lease must be released in finally block even on successful completion."""
        from task_queue import TaskQueue
        tq       = TaskQueue()
        tq._redis = MagicMock()
        tq._redis.set    = AsyncMock(return_value=True)
        tq._redis.delete = AsyncMock()
        tq._redis.get    = AsyncMock(return_value=None)
        tq._patch_queue  = None

        # Stub out update_status to avoid real Redis calls
        tq.update_status = AsyncMock()

        agent_mgr = MagicMock()
        agent_mgr.spawn_and_run = AsyncMock(return_value={
            "status": "done", "result": "output", "agent_id": "a1", "role": "coder"
        })

        task = {"id": "t1", "role": "coder", "desc": "do something", "deps": [], "status": "running"}
        await tq._run_single_task("s1", task, agent_mgr)

        tq._redis.delete.assert_called_with("task:s1:t1:lease")

    @pytest.mark.asyncio
    async def test_run_single_task_releases_lease_on_failure(self):
        """Lease must be released even when agent_mgr raises an exception."""
        from task_queue import TaskQueue
        tq       = TaskQueue()
        tq._redis = MagicMock()
        tq._redis.set    = AsyncMock(return_value=True)
        tq._redis.delete = AsyncMock()
        tq._patch_queue  = None
        tq.update_status = AsyncMock()

        agent_mgr = MagicMock()
        agent_mgr.spawn_and_run = AsyncMock(side_effect=Exception("model crash"))

        task = {"id": "t1", "role": "coder", "desc": "do something", "deps": [], "status": "running"}
        with pytest.raises(Exception, match="model crash"):
            await tq._run_single_task("s1", task, agent_mgr)

        tq._redis.delete.assert_called_with("task:s1:t1:lease")

    @pytest.mark.asyncio
    async def test_load_plan_clears_existing_leases(self):
        """load_plan must delete lease keys for old tasks to avoid stale leases."""
        from task_queue import TaskQueue
        tq       = TaskQueue()
        tq._redis = MagicMock()
        tq._redis.lrange = AsyncMock(return_value=["t1", "t2"])
        tq._redis.delete = AsyncMock()
        tq._redis.set    = AsyncMock()
        tq._redis.rpush  = AsyncMock()

        tasks = [
            {"id": "t3", "role": "coder", "desc": "new task", "deps": []},
        ]
        await tq.load_plan("s1", tasks)

        deleted_keys = [call.args[0] for call in tq._redis.delete.call_args_list]
        assert "task:s1:t1:lease" in deleted_keys
        assert "task:s1:t2:lease" in deleted_keys