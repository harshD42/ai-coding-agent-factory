"""
executor_client.py — HTTP client wrapper for the Executor container.

All filesystem operations (execute commands, apply patches, read/list files)
go through the executor. The orchestrator never touches the workspace directly.
"""

import logging

import httpx

import config

log = logging.getLogger("executor_client")

_client = httpx.AsyncClient(timeout=180.0)


async def execute(command: str, timeout: int = None, cwd: str = None) -> dict:
    """
    Run an allowed command in the workspace sandbox.
    Returns {"stdout": ..., "stderr": ..., "exit_code": ...}
    """
    body: dict = {"command": command}
    if timeout is not None:
        body["timeout"] = timeout
    if cwd is not None:
        body["cwd"] = cwd

    resp = await _client.post(f"{config.EXECUTOR_URL}/execute", json=body)
    resp.raise_for_status()
    return resp.json()


async def apply_patch(diff: str, target: str = "live") -> dict:
    """
    Apply a unified diff to the workspace.
    target="sandbox" → dry-run only (git apply --check)
    target="live"    → actually apply
    Returns {"applied": bool, "message": str}
    """
    resp = await _client.post(
        f"{config.EXECUTOR_URL}/apply-patch",
        json={"diff": diff, "target": target},
    )
    resp.raise_for_status()
    return resp.json()


async def read_file(path: str) -> dict:
    """
    Read a file from the workspace.
    Returns {"path": ..., "content": ..., "size_bytes": ...}
    """
    resp = await _client.post(
        f"{config.EXECUTOR_URL}/read-file",
        json={"path": path},
    )
    resp.raise_for_status()
    return resp.json()


async def list_files(pattern: str = "**/*") -> list[str]:
    """
    Glob files in the workspace.
    Returns a sorted list of relative file paths.
    """
    resp = await _client.post(
        f"{config.EXECUTOR_URL}/list-files",
        json={"pattern": pattern},
    )
    resp.raise_for_status()
    return resp.json().get("files", [])


async def health() -> bool:
    """Return True if executor is reachable and healthy."""
    try:
        resp = await _client.get(f"{config.EXECUTOR_URL}/health", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


# ── Step 2.2: Test runner ─────────────────────────────────────────────────────

async def run_tests(pattern: str = "tests/", timeout: int = 120) -> dict:
    """
    Run pytest inside the executor sandbox.

    Calls POST /execute with:  pytest {pattern} -v --tb=short --no-header

    Returns a normalized result dict:
        {
            "passed":    bool,     # True only if exit_code == 0
            "exit_code": int,
            "stdout":    str,
            "stderr":    str,
            "summary":   str,      # last non-empty stdout line (pytest summary line)
        }

    Never raises — errors are returned as {"passed": False, "error": str, ...}
    so the caller can always inspect the result without a try/except.
    """
    command = f"pytest {pattern} -v --tb=short --no-header"
    log.info("run_tests: %s (timeout=%ds)", command, timeout)
    try:
        result = await execute(command=command, timeout=timeout)
        stdout    = result.get("stdout", "")
        stderr    = result.get("stderr", "")
        exit_code = result.get("exit_code", -1)

        # Extract pytest's summary line (last non-blank line of stdout)
        lines   = [l for l in stdout.splitlines() if l.strip()]
        summary = lines[-1] if lines else ""

        passed = (exit_code == 0)
        log.info("run_tests: exit_code=%d  passed=%s  summary=%r",
                 exit_code, passed, summary)
        return {
            "passed":    passed,
            "exit_code": exit_code,
            "stdout":    stdout,
            "stderr":    stderr,
            "summary":   summary,
        }
    except Exception as exc:
        log.error("run_tests error: %s", exc)
        return {
            "passed":    False,
            "exit_code": -1,
            "stdout":    "",
            "stderr":    "",
            "summary":   "",
            "error":     str(exc),
        }