"""
webhook_handler.py — GitHub webhook receiver (Step 3.3).

Handles two event types:
    workflow_run   — failed CI → fetch logs → coder agent proposes fix
    issues         — new issue opened → architect decomposes into task DAG

Security:
    Every request is validated against the GITHUB_WEBHOOK_SECRET HMAC-SHA256
    signature in the X-Hub-Signature-256 header.  Requests with missing or
    invalid signatures are rejected with 401.

Configuration (config.py / .env):
    GITHUB_WEBHOOK_SECRET   — shared secret set in the GitHub webhook settings
    GITHUB_TOKEN            — PAT or fine-grained token with:
                              repo:read, actions:read, issues:read
    GITHUB_REPO             — "owner/repo" e.g. "harshd42/ai-coding-agent-factory"

Flow — workflow_run (failed CI):
    1. Validate signature
    2. Check event action == "completed" and conclusion == "failure"
    3. Fetch failed job logs via GitHub API
    4. Spawn coder agent with: task description + truncated log as context
    5. Extract diffs from agent output → enqueue patches
    6. Return summary

Flow — issues (opened):
    1. Validate signature
    2. Spawn architect with issue title + body as task
    3. Decompose plan into task DAG → load into Redis
    4. Return session_id and tasks_loaded
"""

import hashlib
import hmac
import logging
import uuid

import httpx

import config
from utils import extract_diffs_from_result

log = logging.getLogger("webhook")

_gh_client = httpx.AsyncClient(
    base_url="https://api.github.com",
    headers={
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
    timeout=30.0,
)


# ── Signature validation ──────────────────────────────────────────────────────

class WebhookSignatureError(Exception):
    pass


def verify_signature(body: bytes, signature_header: str) -> None:
    """
    Validate X-Hub-Signature-256 header against GITHUB_WEBHOOK_SECRET.
    Raises WebhookSignatureError if invalid or secret not configured.
    """
    secret = config.GITHUB_WEBHOOK_SECRET
    if not secret:
        raise WebhookSignatureError(
            "GITHUB_WEBHOOK_SECRET not configured — rejecting all webhook requests"
        )
    if not signature_header or not signature_header.startswith("sha256="):
        raise WebhookSignatureError("Missing or malformed X-Hub-Signature-256 header")

    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise WebhookSignatureError("Signature mismatch — request rejected")


# ── GitHub API helpers ────────────────────────────────────────────────────────

async def _gh_get(path: str) -> dict | list:
    """GET from GitHub API with auth token."""
    token = config.GITHUB_TOKEN
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = await _gh_client.get(path, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def _fetch_failed_job_logs(run_id: int) -> str:
    """
    Fetch the log text for failed jobs in a workflow run.
    Returns up to 8000 chars of combined log output.
    """
    repo = config.GITHUB_REPO
    if not repo:
        return "No GITHUB_REPO configured — log fetch skipped."
    try:
        jobs_data = await _gh_get(f"/repos/{repo}/actions/runs/{run_id}/jobs")
        failed_jobs = [
            j for j in jobs_data.get("jobs", [])
            if j.get("conclusion") == "failure"
        ]
        if not failed_jobs:
            return "No failed jobs found in this workflow run."

        log_parts = []
        for job in failed_jobs[:2]:   # cap at 2 jobs to stay within context
            job_id   = job["id"]
            job_name = job.get("name", str(job_id))
            try:
                token   = config.GITHUB_TOKEN
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                resp    = await _gh_client.get(
                    f"/repos/{repo}/actions/jobs/{job_id}/logs",
                    headers=headers,
                    follow_redirects=True,
                )
                log_text = resp.text[:4000]
                log_parts.append(f"### Job: {job_name}\n{log_text}")
            except Exception as e:
                log_parts.append(f"### Job: {job_name}\n(log fetch failed: {e})")

        return "\n\n".join(log_parts)
    except Exception as e:
        log.warning("_fetch_failed_job_logs error: %s", e)
        return f"Log fetch failed: {e}"


# ── Event handlers ────────────────────────────────────────────────────────────

async def handle_workflow_run(payload: dict, agent_mgr, patch_queue_obj) -> dict:
    """
    Handle a workflow_run webhook event.

    Triggered when a CI workflow completes.  Only acts on failures.
    Spawns a coder agent with the failure logs and auto-enqueues fix diffs.
    """
    action     = payload.get("action", "")
    run        = payload.get("workflow_run", {})
    conclusion = run.get("conclusion", "")
    run_id     = run.get("id", 0)
    run_name   = run.get("name", "unknown")
    branch     = run.get("head_branch", "unknown")

    if action != "completed" or conclusion != "failure":
        return {"skipped": True, "reason": f"action={action} conclusion={conclusion}"}

    log.info("webhook: CI failure  run=%s  branch=%s  run_id=%s", run_name, branch, run_id)

    # Fetch logs
    logs = await _fetch_failed_job_logs(run_id)

    session_id = f"ci-fix-{run_id}"
    task = (
        f"The CI workflow '{run_name}' failed on branch '{branch}'.\n"
        f"Analyze the following failure logs and produce a unified diff that fixes "
        f"the failing tests or build errors. Output ONLY the diff in a ```diff block.\n\n"
        f"## Failure Logs\n```\n{logs[:6000]}\n```"
    )

    result     = await agent_mgr.spawn_and_run(
        role="coder", task=task, session_id=session_id
    )
    output     = result.get("result", "") or ""
    diffs      = extract_diffs_from_result(output)

    enqueued   = 0
    for diff in diffs:
        try:
            await patch_queue_obj.enqueue(
                diff=diff,
                agent_id=f"ci-fix-{run_id}",
                task_id=f"ci-{run_id}",
                session_id=session_id,
                description=f"Auto-fix for failed CI run {run_id} on {branch}",
            )
            enqueued += 1
        except Exception as e:
            log.warning("webhook: patch enqueue failed: %s", e)

    log.info("webhook: workflow_run handled  diffs_found=%d  enqueued=%d", len(diffs), enqueued)
    return {
        "event":      "workflow_run",
        "run_id":     run_id,
        "session_id": session_id,
        "diffs_found": len(diffs),
        "enqueued":   enqueued,
        "agent_status": result.get("status"),
    }


async def handle_issue_opened(payload: dict, agent_mgr, task_queue_obj) -> dict:
    """
    Handle an issues webhook event (action: opened).

    Spawns an architect to decompose the issue into a task DAG and loads
    it into Redis so `/execute` can run it.
    """
    action = payload.get("action", "")
    issue  = payload.get("issue", {})

    if action != "opened":
        return {"skipped": True, "reason": f"action={action}"}

    issue_number = issue.get("number", 0)
    title        = issue.get("title", "")
    body         = issue.get("body", "") or ""
    session_id   = f"issue-{issue_number}"

    log.info("webhook: issue opened  #%d  %r", issue_number, title)

    task = (
        f"GitHub Issue #{issue_number}: {title}\n\n"
        f"{body[:3000]}\n\n"
        f"Produce an implementation plan for this issue."
    )

    plan_result = await agent_mgr.spawn_and_run(
        role="architect", task=task, session_id=session_id
    )
    plan = plan_result.get("result", "") or ""

    tasks = await agent_mgr.decompose_plan_to_tasks(plan, session_id=session_id)
    if tasks:
        await task_queue_obj.load_plan(session_id, tasks)
        log.info("webhook: issue #%d → %d tasks loaded", issue_number, len(tasks))

    return {
        "event":        "issues",
        "issue_number": issue_number,
        "session_id":   session_id,
        "tasks_loaded": len(tasks),
        "plan_preview": plan[:300],
    }