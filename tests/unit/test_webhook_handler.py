import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import pytest
import hmac
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch as mock_patch
from webhook_handler import (
    verify_signature, WebhookSignatureError,
    handle_workflow_run, handle_issue_opened,
)


class TestVerifySignature:
    def _make_sig(self, body: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_valid_signature_passes(self):
        body   = b'{"test": true}'
        secret = "mysecret"
        sig    = self._make_sig(body, secret)
        with mock_patch("webhook_handler.config") as cfg:
            cfg.GITHUB_WEBHOOK_SECRET = secret
            verify_signature(body, sig)   # should not raise

    def test_invalid_signature_raises(self):
        body = b'{"test": true}'
        with mock_patch("webhook_handler.config") as cfg:
            cfg.GITHUB_WEBHOOK_SECRET = "mysecret"
            with pytest.raises(WebhookSignatureError):
                verify_signature(body, "sha256=badhash")

    def test_missing_signature_raises(self):
        with mock_patch("webhook_handler.config") as cfg:
            cfg.GITHUB_WEBHOOK_SECRET = "secret"
            with pytest.raises(WebhookSignatureError):
                verify_signature(b"body", "")

    def test_missing_secret_raises(self):
        with mock_patch("webhook_handler.config") as cfg:
            cfg.GITHUB_WEBHOOK_SECRET = ""
            with pytest.raises(WebhookSignatureError):
                verify_signature(b"body", "sha256=anything")

    def test_missing_prefix_raises(self):
        body = b"data"
        with mock_patch("webhook_handler.config") as cfg:
            cfg.GITHUB_WEBHOOK_SECRET = "s"
            with pytest.raises(WebhookSignatureError):
                verify_signature(body, "noprefixhere")


class TestHandleWorkflowRun:
    def _payload(self, action="completed", conclusion="failure"):
        return {
            "action": action,
            "workflow_run": {
                "id": 12345,
                "name": "CI",
                "conclusion": conclusion,
                "head_branch": "main",
            },
        }

    @pytest.mark.asyncio
    async def test_skips_non_failure(self):
        payload = self._payload(conclusion="success")
        result  = await handle_workflow_run(payload, MagicMock(), MagicMock())
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_skips_non_completed(self):
        payload = self._payload(action="requested")
        result  = await handle_workflow_run(payload, MagicMock(), MagicMock())
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_failure_spawns_agent(self):
        payload   = self._payload()
        agent_mgr = MagicMock()
        agent_mgr.spawn_and_run = AsyncMock(return_value={
            "result": "```diff\n--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,2 @@\n x=1\n+y=2\n```",
            "status": "done",
        })
        pq = MagicMock()
        pq.enqueue = AsyncMock(return_value=MagicMock(to_dict=lambda: {}))

        with mock_patch("webhook_handler._fetch_failed_job_logs",
                        AsyncMock(return_value="FAIL: test_foo")):
            result = await handle_workflow_run(payload, agent_mgr, pq)

        assert result["event"] == "workflow_run"
        agent_mgr.spawn_and_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_diff_in_output(self):
        payload   = self._payload()
        agent_mgr = MagicMock()
        agent_mgr.spawn_and_run = AsyncMock(return_value={
            "result": "I couldn't figure out the fix.",
            "status": "done",
        })
        pq = MagicMock()
        pq.enqueue = AsyncMock()

        with mock_patch("webhook_handler._fetch_failed_job_logs",
                        AsyncMock(return_value="logs")):
            result = await handle_workflow_run(payload, agent_mgr, pq)

        assert result["diffs_found"] == 0
        pq.enqueue.assert_not_awaited()


class TestHandleIssueOpened:
    def _payload(self, action="opened"):
        return {
            "action": action,
            "issue": {
                "number": 42,
                "title": "Add multiply function",
                "body":  "Please add a multiply(a, b) function to hello.py",
            },
        }

    @pytest.mark.asyncio
    async def test_skips_non_open(self):
        payload = self._payload(action="closed")
        result  = await handle_issue_opened(payload, MagicMock(), MagicMock())
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_opened_decomposes_plan(self):
        payload   = self._payload()
        agent_mgr = MagicMock()
        agent_mgr.spawn_and_run = AsyncMock(return_value={
            "result": "Plan: add multiply function",
            "status": "done",
        })
        agent_mgr.decompose_plan_to_tasks = AsyncMock(return_value=[
            {"id": "t1", "role": "coder", "desc": "add multiply", "deps": []}
        ])
        tq = MagicMock()
        tq.load_plan = AsyncMock(return_value={"tasks_loaded": 1})

        result = await handle_issue_opened(payload, agent_mgr, tq)

        assert result["event"] == "issues"
        assert result["issue_number"] == 42
        assert result["tasks_loaded"] == 1
        tq.load_plan.assert_awaited_once()