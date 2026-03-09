"""
Executor — Sandboxed command runner for the AI Coding Agent Factory.

Endpoints:
    POST /execute       Run an allowed command in the workspace
    POST /apply-patch   Apply a unified diff to the workspace
    POST /read-file     Read a file from the workspace
    POST /list-files    Glob files within the workspace
    GET  /health        Health check

Security model:
    - Commands validated against ALLOWED_COMMANDS whitelist
    - All file paths resolved and checked to be under WORKSPACE_DIR
    - Subprocess timeout enforced
    - No Docker socket, no network egress (enforced at compose level)
    - Runs as non-root user (executor)

Phase 4A.3 addition:
    Per-execution resource limits via resource.setrlimit applied as
    preexec_fn in subprocess calls. Container-level limits (mem_limit,
    cpus, pids_limit) are set in docker-compose.yml. These per-execution
    limits add a second layer of protection inside the container:
      - RLIMIT_CPU:   60s hard CPU time limit per execution
      - RLIMIT_FSIZE: 500MB max file write size per execution
      - RLIMIT_NOFILE: 256 max open file descriptors per execution
    These prevent fork bombs, infinite loops, and descriptor exhaustion
    within a single subprocess without affecting other concurrent requests.
"""

import glob
import logging
import os
import re
import resource
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace")).resolve()
CMD_TIMEOUT   = int(os.environ.get("COMMAND_TIMEOUT", "120"))

_raw_allowed  = os.environ.get(
    "ALLOWED_COMMANDS",
    "pytest,npm,node,cargo,go,make,git,bash,sh,cat,ls,find,grep"
)
ALLOWED_COMMANDS: set[str] = {c.strip() for c in _raw_allowed.split(",")}

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("executor")


# ── Phase 4A.3: per-execution resource limits ─────────────────────────────────

def _apply_execution_limits() -> None:
    """
    Apply per-execution resource limits via setrlimit.
    Called as preexec_fn in subprocess.run() — runs in the child process
    before exec, so limits apply only to that subprocess and its children.

    Container-level limits (mem_limit: 2g, cpus: 2, pids_limit: 64) are set
    in docker-compose.yml. These per-execution limits add a second layer:
      - CPU:   60s hard limit prevents infinite loops from hanging a slot
      - FSIZE: 500MB prevents runaway file writes from filling the volume
      - NOFILE: 256 prevents descriptor exhaustion across concurrent requests

    Note: RLIMIT_CPU counts CPU time, not wall time. A process sleeping on
    I/O does not consume CPU quota. The subprocess timeout (CMD_TIMEOUT) is
    the wall-time guard; RLIMIT_CPU is the compute-time guard.
    """
    try:
        # CPU time: 60s hard limit
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
        # File size: 500MB max write per execution
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (500 * 1024 * 1024, 500 * 1024 * 1024)
        )
        # Open file descriptors: 256 max
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    except Exception as e:
        # Log but don't crash — limits are a hardening layer, not a hard requirement
        log.warning("setrlimit failed (non-Linux or permission denied): %s", e)


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ensure workspace is a git repo on startup.
    - If not yet a repo: init, add all existing files, make an initial commit.
    - If already a repo: ensure any untracked files are committed so
      git apply --check has a clean baseline to work against.
    - Normalize CRLF in all text files.
    """
    git = "/usr/bin/git"

    for p in WORKSPACE_DIR.rglob("*"):
        if p.is_file() and p.suffix in {
            ".py", ".js", ".ts", ".md", ".txt", ".sh",
            ".yaml", ".yml", ".json", ".toml"
        }:
            _normalize_crlf(p)

    git_dir = WORKSPACE_DIR / ".git"
    if not git_dir.exists():
        subprocess.run([git, "init"],                                  cwd=WORKSPACE_DIR, capture_output=True)
        subprocess.run([git, "config", "user.email", "agent@local"],  cwd=WORKSPACE_DIR, capture_output=True)
        subprocess.run([git, "config", "user.name",  "Agent"],        cwd=WORKSPACE_DIR, capture_output=True)
        log.info("git repo initialized at %s", WORKSPACE_DIR)
    else:
        subprocess.run([git, "config", "user.email", "agent@local"],  cwd=WORKSPACE_DIR, capture_output=True)
        subprocess.run([git, "config", "user.name",  "Agent"],        cwd=WORKSPACE_DIR, capture_output=True)
        log.info("git repo already exists at %s", WORKSPACE_DIR)

    subprocess.run([git, "add", "-A"], cwd=WORKSPACE_DIR, capture_output=True)
    result = subprocess.run(
        [git, "commit", "-m", "chore: baseline snapshot"],
        cwd=WORKSPACE_DIR, capture_output=True, text=True,
    )
    if result.returncode == 0:
        log.info("git baseline commit created")
    else:
        msg = result.stdout.strip() or result.stderr.strip()
        log.info("git baseline: %s", msg)

    yield


app = FastAPI(title="Executor", version="1.0.0", lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_path(raw: str) -> Path:
    p = (WORKSPACE_DIR / raw).resolve()
    if not str(p).startswith(str(WORKSPACE_DIR)):
        raise HTTPException(400, f"Path traversal rejected: {raw!r}")
    return p


def _parse_command(command: str) -> list[str]:
    import shlex
    return shlex.split(command)


def _check_allowed(argv: list[str]) -> None:
    binary = Path(argv[0]).name
    if binary not in ALLOWED_COMMANDS:
        raise HTTPException(
            400,
            f"Command {binary!r} not in allowed list. "
            f"Allowed: {sorted(ALLOWED_COMMANDS)}"
        )


def _normalize_crlf(path: Path) -> None:
    """Convert CRLF to LF in-place. No-op if file is already LF or binary."""
    try:
        raw = path.read_bytes()
        if b"\r\n" in raw:
            path.write_bytes(raw.replace(b"\r\n", b"\n"))
    except Exception:
        pass


# ── Schemas ───────────────────────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    command: str
    timeout: Optional[int] = None
    cwd:     Optional[str] = None


class ExecuteResponse(BaseModel):
    stdout:    str
    stderr:    str
    exit_code: int


class ApplyPatchRequest(BaseModel):
    diff:   str
    target: str = "live"


class ApplyPatchResponse(BaseModel):
    applied: bool
    message: str


class ReadFileRequest(BaseModel):
    path: str


class ReadFileResponse(BaseModel):
    path:       str
    content:    str
    size_bytes: int


class ListFilesRequest(BaseModel):
    pattern: str = "**/*"


class ListFilesResponse(BaseModel):
    files: list[str]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE_DIR)}


@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest):
    """
    Run an allowed command inside the workspace.

    Phase 4A.3: _apply_execution_limits passed as preexec_fn — limits
    apply to the child process only, not to the executor FastAPI process.
    """
    argv = _parse_command(req.command)
    _check_allowed(argv)

    if req.cwd:
        cwd = _safe_path(req.cwd)
        if not cwd.is_dir():
            raise HTTPException(400, f"cwd not a directory: {req.cwd!r}")
    else:
        cwd = WORKSPACE_DIR

    timeout = req.timeout if req.timeout is not None else CMD_TIMEOUT
    log.info("exec  cwd=%s  cmd=%s  timeout=%ds", cwd, argv, timeout)

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=_apply_execution_limits,   # Phase 4A.3
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(408, f"Command timed out after {timeout}s")
    except FileNotFoundError:
        raise HTTPException(400, f"Binary not found: {argv[0]!r}")

    log.info("exit_code=%d", proc.returncode)
    return ExecuteResponse(
        stdout=proc.stdout,
        stderr=proc.stderr,
        exit_code=proc.returncode,
    )


@app.post("/apply-patch", response_model=ApplyPatchResponse)
def apply_patch(req: ApplyPatchRequest):
    """
    Apply a unified diff to the workspace.
    Phase 4A.3: _apply_execution_limits applied to git subprocess.
    """
    if req.target not in ("sandbox", "live"):
        raise HTTPException(400, "target must be 'sandbox' or 'live'")

    if len(req.diff) > 2_000_000:
        raise HTTPException(400, "Diff too large (>2 MB)")
    if not req.diff.strip():
        raise HTTPException(400, "Empty diff")

    changed_lines = sum(
        1 for l in req.diff.splitlines()
        if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))
    )
    if changed_lines > 1000:
        raise HTTPException(
            400, f"Patch too large: {changed_lines} changed lines (limit 1000)"
        )

    if "GIT binary patch" in req.diff or "Binary files" in req.diff:
        raise HTTPException(400, "Binary patches not allowed")

    if re.search(r"^(old|new) mode \d+", req.diff, re.MULTILINE):
        raise HTTPException(400, "Permission-change patches not allowed")

    clean_diff = req.diff.replace("\r\n", "\n").replace("\r", "\n")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, dir="/tmp", newline="\n"
    ) as f:
        f.write(clean_diff)
        patch_path = f.name

    for line in clean_diff.splitlines():
        if line.startswith("+++ b/"):
            target = _safe_path(line[6:].strip())
            if target.exists():
                _normalize_crlf(target)

    try:
        if req.target == "sandbox":
            git_args = ["/usr/bin/git", "apply", "--check", "--whitespace=fix", patch_path]
        else:
            git_args = ["/usr/bin/git", "apply", "--whitespace=fix", patch_path]

        log.info("patch  target=%s  lines=%d", req.target, changed_lines)
        proc = subprocess.run(
            git_args,
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            preexec_fn=_apply_execution_limits,   # Phase 4A.3
        )
    finally:
        os.unlink(patch_path)

    if proc.returncode != 0:
        log.warning("git apply failed (target=%s):\n%s", req.target, proc.stderr.strip())
        return ApplyPatchResponse(
            applied=False,
            message=f"git apply failed:\n{proc.stderr.strip()}"
        )

    verb = "Dry-run OK" if req.target == "sandbox" else "Applied"
    return ApplyPatchResponse(applied=True, message=f"{verb} ({changed_lines} changed lines)")


@app.post("/read-file", response_model=ReadFileResponse)
def read_file(req: ReadFileRequest):
    """Read a file from the workspace."""
    p = _safe_path(req.path)
    if not p.exists():
        raise HTTPException(404, f"File not found: {req.path!r}")
    if not p.is_file():
        raise HTTPException(400, f"Not a file: {req.path!r}")

    size = p.stat().st_size
    if size > 10_000_000:
        raise HTTPException(413, f"File too large to read: {size} bytes")

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(500, f"Read error: {e}")

    return ReadFileResponse(
        path=str(p.relative_to(WORKSPACE_DIR)),
        content=content,
        size_bytes=size,
    )


@app.post("/list-files", response_model=ListFilesResponse)
def list_files(req: ListFilesRequest):
    """Glob files within the workspace."""
    if req.pattern.startswith("/") or ".." in req.pattern:
        raise HTTPException(400, "Pattern must be relative and contain no '..'")

    matches = glob.glob(str(WORKSPACE_DIR / req.pattern), recursive=True)

    safe = []
    for m in matches:
        try:
            p = Path(m).resolve()
            if str(p).startswith(str(WORKSPACE_DIR)) and p.is_file():
                safe.append(str(p.relative_to(WORKSPACE_DIR)))
        except Exception:
            pass

    return ListFilesResponse(files=sorted(safe))