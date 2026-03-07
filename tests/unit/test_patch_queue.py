"""tests/unit/test_patch_queue.py — Patch validation and queue logic tests."""

import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

from patch_queue import (
    validate_patch, PatchValidationError, normalize_diff,
    PatchQueue, Patch, extract_file_paths_from_diff
)
from utils import count_diff_lines


VALID_DIFF = (
    "--- a/hello.py\n"
    "+++ b/hello.py\n"
    "@@ -1 +1,2 @@\n"
    "-def hello(): pass\n"
    "+def hello():\n"
    "+    print('hello')\n"
)


class TestValidatePatch:
    def test_valid_diff_passes(self):
        validate_patch(VALID_DIFF)

    def test_empty_diff_rejected(self):
        with pytest.raises(PatchValidationError, match="Empty"):
            validate_patch("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(PatchValidationError, match="Empty"):
            validate_patch("   \n  ")

    def test_no_hunk_header_rejected(self):
        with pytest.raises(PatchValidationError, match="unified diff"):
            validate_patch("--- a/f.py\n+++ b/f.py\nno hunk here\n")

    def test_binary_patch_rejected(self):
        with pytest.raises(PatchValidationError, match="Binary"):
            validate_patch(VALID_DIFF + "\nGIT binary patch\nliteral 100\n")

    def test_binary_files_marker_rejected(self):
        with pytest.raises(PatchValidationError, match="Binary"):
            validate_patch("Binary files a/img.png and b/img.png differ\n")

    def test_permission_change_rejected(self):
        with pytest.raises(PatchValidationError, match="Permission"):
            validate_patch("old mode 100644\nnew mode 100755\n" + VALID_DIFF)

    def test_oversized_diff_rejected(self):
        big = VALID_DIFF + "".join(f"+line {i}\n" for i in range(1001))
        with pytest.raises(PatchValidationError, match="large"):
            validate_patch(big)

    def test_exactly_1000_lines_passes(self):
        diff = (
            "--- a/f.py\n+++ b/f.py\n@@ -1 +1,1000 @@\n-old\n"
            + "".join(f"+line {i}\n" for i in range(999))
        )
        validate_patch(diff)

    def test_oversized_raw_bytes_rejected(self):
        huge = VALID_DIFF + "+" + "x" * 4_000_000
        with pytest.raises(PatchValidationError, match="4 MB"):
            validate_patch(huge)

    def test_new_file_diff_passes(self):
        diff = (
            "--- /dev/null\n"
            "+++ b/newfile.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+def new_func():\n"
            "+    pass\n"
        )
        validate_patch(diff)

    def test_delete_file_diff_passes(self):
        diff = (
            "--- a/oldfile.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-def old():\n"
            "-    pass\n"
        )
        validate_patch(diff)


class TestNormalizeDiff:
    def test_crlf_normalized_to_lf(self):
        crlf = "--- a/f.py\r\n+++ b/f.py\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"
        result = normalize_diff(crlf)
        assert "\r" not in result
        assert "--- a/f.py\n" in result

    def test_lf_unchanged(self):
        assert normalize_diff(VALID_DIFF) == VALID_DIFF

    def test_bare_cr_normalized(self):
        cr = "--- a/f.py\r+++ b/f.py\r@@ -1 +1 @@\r-old\r+new\r"
        assert "\r" not in normalize_diff(cr)

    def test_mixed_endings_normalized(self):
        mixed = "--- a/f.py\r\n+++ b/f.py\n@@ -1 +1 @@\r\n-old\n+new\r\n"
        result = normalize_diff(mixed)
        assert "\r" not in result


class TestPatch:
    def test_initial_state(self):
        p = Patch(VALID_DIFF, "agent-1", "t1", "sess-1", "Add docstring")
        assert p.status      == "pending"
        assert p.retries     == 0
        assert p.error       is None
        assert p.agent_id    == "agent-1"
        assert p.task_id     == "t1"
        assert p.session_id  == "sess-1"
        assert p.description == "Add docstring"

    def test_to_dict_structure(self):
        p = Patch(VALID_DIFF, "agent-1", "t1", "sess-1")
        d = p.to_dict()
        for key in ("patch_id", "agent_id", "task_id", "session_id",
                    "status", "retries", "error", "files", "diff_lines", "created_at"):
            assert key in d

    def test_to_dict_excludes_raw_diff(self):
        p = Patch(VALID_DIFF, "agent-1", "t1", "sess-1")
        d = p.to_dict()
        assert "diff" not in d

    def test_files_extracted_from_diff(self):
        p = Patch(VALID_DIFF, "agent-1", "t1", "sess-1")
        d = p.to_dict()
        assert "hello.py" in d["files"]

    def test_normalize_diff_strips_crlf(self):
        # normalize_diff() is called in enqueue(), not in Patch.__init__
        # Test the function directly
        crlf_diff = VALID_DIFF.replace("\n", "\r\n")
        result = normalize_diff(crlf_diff)
        assert "\r" not in result
        assert result == VALID_DIFF


class TestPatchQueueLogic:
    def test_queue_depth_empty(self):
        q = PatchQueue()
        d = q.queue_depth()
        assert d["total"]   == 0
        assert d["pending"] == 0
        assert d["applied"] == 0

    def test_list_patches_empty(self):
        q = PatchQueue()
        assert q.list_patches() == []

    def test_get_patch_missing(self):
        q = PatchQueue()
        assert q.get_patch("nonexistent") is None

    def test_list_patches_by_session(self):
        q = PatchQueue()
        # Manually inject patches
        p1 = Patch(VALID_DIFF, "a", "t1", "sess-A")
        p2 = Patch(VALID_DIFF, "b", "t2", "sess-B")
        q._patches = {"p1": p1, "p2": p2}
        assert len(q.list_patches("sess-A")) == 1
        assert len(q.list_patches("sess-B")) == 1
        assert len(q.list_patches())         == 2

    def test_queue_depth_counts(self):
        q = PatchQueue()
        statuses = ["pending", "applied", "rejected", "conflict", "applied"]
        for i, s in enumerate(statuses):
            p = Patch(VALID_DIFF, "a", f"t{i}", "sess")
            p.status = s
            q._queue.append(p)
            q._patches[p.patch_id] = p
        d = q.queue_depth()
        assert d["total"]    == 5
        assert d["pending"]  == 1
        assert d["applied"]  == 2
        assert d["rejected"] == 1
        assert d["conflict"] == 1
"""tests/unit/test_patch_queue.py — Patch validation unit tests."""

import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

from patch_queue import validate_patch, PatchValidationError, normalize_diff


VALID_DIFF = (
    "--- a/hello.py\n"
    "+++ b/hello.py\n"
    "@@ -1 +1,2 @@\n"
    "-def hello(): pass\n"
    "+def hello():\n"
    "+    print('hello')\n"
)


class TestValidatePatch:
    def test_valid_diff_passes(self):
        validate_patch(VALID_DIFF)  # should not raise

    def test_empty_diff_rejected(self):
        with pytest.raises(PatchValidationError, match="Empty"):
            validate_patch("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(PatchValidationError, match="Empty"):
            validate_patch("   \n  ")

    def test_no_hunk_header_rejected(self):
        with pytest.raises(PatchValidationError, match="unified diff"):
            validate_patch("--- a/f.py\n+++ b/f.py\nno hunk here\n")

    def test_binary_patch_rejected(self):
        with pytest.raises(PatchValidationError, match="Binary"):
            validate_patch(VALID_DIFF + "\nGIT binary patch\nliteral 100\n")

    def test_binary_files_marker_rejected(self):
        with pytest.raises(PatchValidationError, match="Binary"):
            validate_patch("Binary files a/img.png and b/img.png differ\n")

    def test_permission_change_rejected(self):
        with pytest.raises(PatchValidationError, match="Permission"):
            validate_patch("old mode 100644\nnew mode 100755\n" + VALID_DIFF)

    def test_oversized_diff_rejected(self):
        big = VALID_DIFF + "".join(f"+line {i}\n" for i in range(1001))
        with pytest.raises(PatchValidationError, match="large"):
            validate_patch(big)

    def test_exactly_1000_lines_passes(self):
        # 1000 changed lines should be accepted
        diff = (
            "--- a/f.py\n+++ b/f.py\n@@ -1 +1,1000 @@\n-old\n"
            + "".join(f"+line {i}\n" for i in range(999))
        )
        validate_patch(diff)  # should not raise

    def test_oversized_raw_bytes_rejected(self):
        huge = VALID_DIFF + "+" + "x" * 4_000_000
        with pytest.raises(PatchValidationError, match="4 MB"):
            validate_patch(huge)


class TestNormalizeDiff:
    def test_crlf_normalized_to_lf(self):
        crlf = "--- a/f.py\r\n+++ b/f.py\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"
        result = normalize_diff(crlf)
        assert "\r" not in result
        assert "--- a/f.py\n" in result

    def test_lf_unchanged(self):
        result = normalize_diff(VALID_DIFF)
        assert result == VALID_DIFF

    def test_bare_cr_normalized(self):
        cr = "--- a/f.py\r+++ b/f.py\r@@ -1 +1 @@\r-old\r+new\r"
        result = normalize_diff(cr)
        assert "\r" not in result


import pytest
from unittest.mock import AsyncMock, MagicMock, patch as mock_patch

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

class TestTestFixLoop:
    """Tests for patch_queue.test_fix_loop() (Step 2.2)."""

    def _make_patch(self, diff=None):
        from patch_queue import Patch
        d = diff or _SIMPLE_DIFF
        p = Patch(diff=d, agent_id="a", task_id="t", session_id="s")
        return p

    def _make_pq(self):
        from patch_queue import PatchQueue
        pq = PatchQueue()
        return pq

    @pytest.mark.asyncio
    async def test_pass_on_first_attempt(self):
        pq    = self._make_pq()
        patch = self._make_patch()
        agent_mgr = MagicMock()

        pq._apply_patch = AsyncMock(return_value={"action": "applied", **patch.to_dict()})
        mock_tests = {"passed": True, "exit_code": 0, "stdout": "1 passed",
                      "stderr": "", "summary": "1 passed"}

        with mock_patch("executor_client.run_tests", AsyncMock(return_value=mock_tests)):
            result = await pq.test_fix_loop(patch, agent_mgr, max_attempts=3)

        assert result["test_passed"] is True
        assert result["attempts"] == 1

    @pytest.mark.asyncio
    async def test_apply_rejected_skips_tests(self):
        pq    = self._make_pq()
        patch = self._make_patch()
        agent_mgr = MagicMock()

        pq._apply_patch = AsyncMock(return_value={"action": "rejected", **patch.to_dict()})

        with mock_patch("executor_client.run_tests", AsyncMock()) as mock_tests:
            result = await pq.test_fix_loop(patch, agent_mgr, max_attempts=3)

        mock_tests.assert_not_awaited()
        assert result["test_passed"] is False

    @pytest.mark.asyncio
    async def test_fix_loop_retries(self):
        pq    = self._make_pq()
        patch = self._make_patch()

        apply_results = [
            {"action": "applied", **patch.to_dict()},
            {"action": "applied", **patch.to_dict()},
        ]
        pq._apply_patch = AsyncMock(side_effect=apply_results)
        pq.enqueue = AsyncMock(return_value=self._make_patch())

        test_results = [
            {"passed": False, "exit_code": 1, "stdout": "FAIL", "stderr": "err", "summary": "0 passed"},
            {"passed": True,  "exit_code": 0, "stdout": "PASS", "stderr": "",    "summary": "1 passed"},
        ]
        agent_mgr = MagicMock()
        agent_mgr.spawn_and_run = AsyncMock(return_value={
            "result": f"```diff\n{_SIMPLE_DIFF}\n```",
            "status": "done",
        })

        with mock_patch("executor_client.run_tests", AsyncMock(side_effect=test_results)):
            result = await pq.test_fix_loop(patch, agent_mgr, max_attempts=3)

        assert result["test_passed"] is True
        assert result["attempts"] == 2

    @pytest.mark.asyncio
    async def test_exhausted_attempts_flags_review(self):
        pq    = self._make_pq()
        patch = self._make_patch()

        pq._apply_patch = AsyncMock(return_value={"action": "applied", **patch.to_dict()})
        pq.enqueue = AsyncMock(return_value=self._make_patch())

        always_fail = {"passed": False, "exit_code": 1, "stdout": "", "stderr": "err", "summary": ""}
        agent_mgr   = MagicMock()
        agent_mgr.spawn_and_run = AsyncMock(return_value={
            "result": f"```diff\n{_SIMPLE_DIFF}\n```",
            "status": "done",
        })

        with mock_patch("executor_client.run_tests", AsyncMock(return_value=always_fail)):
            result = await pq.test_fix_loop(patch, agent_mgr, max_attempts=2)

        assert result["test_passed"] is False
        assert result["action"] == "needs_review"
        assert result["attempts"] == 2