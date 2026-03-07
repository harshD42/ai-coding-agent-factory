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
"""

import glob
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

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

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: F811  replaces the stub above
    """
    Ensure workspace is a git repo on startup.
    - If not yet a repo: init, add all existing files, make an initial commit.
    - If already a repo: ensure any untracked files are committed so
      git apply --check has a clean baseline to work against.
    - Normalize CRLF in all text files.
    """
    git = "/usr/bin/git"

    # Normalize CRLF first (Windows-written files break git apply)
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
        # Repo exists — ensure user identity is set (required for commits)
        subprocess.run([git, "config", "user.email", "agent@local"],  cwd=WORKSPACE_DIR, capture_output=True)
        subprocess.run([git, "config", "user.name",  "Agent"],        cwd=WORKSPACE_DIR, capture_output=True)
        log.info("git repo already exists at %s", WORKSPACE_DIR)

    # Stage and commit any untracked or modified files so git apply
    # always has a clean, committed baseline to apply patches against.
    # This is idempotent — if nothing changed, git commit does nothing.
    subprocess.run([git, "add", "-A"],                                 cwd=WORKSPACE_DIR, capture_output=True)
    result = subprocess.run(
        [git, "commit", "-m", "chore: baseline snapshot"],
        cwd=WORKSPACE_DIR, capture_output=True, text=True,
    )
    if result.returncode == 0:
        log.info("git baseline commit created")
    else:
        # "nothing to commit" is returncode 1 — that's fine
        msg = result.stdout.strip() or result.stderr.strip()
        log.info("git baseline: %s", msg)

    yield

app = FastAPI(title="Executor", version="1.0.0", lifespan=lifespan)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_path(raw: str) -> Path:
    """
    Resolve a path and assert it sits under WORKSPACE_DIR.
    Raises HTTPException 400 on directory traversal attempts.
    """
    p = (WORKSPACE_DIR / raw).resolve()
    if not str(p).startswith(str(WORKSPACE_DIR)):
        raise HTTPException(400, f"Path traversal rejected: {raw!r}")
    return p


def _parse_command(command: str) -> list[str]:
    """
    Split command string into argv list using basic shell-like splitting
    (no shell=True; avoids shell injection while supporting quoted args).
    """
    import shlex
    return shlex.split(command)


def _check_allowed(argv: list[str]) -> None:
    """
    Assert that the first token (the binary name) is in ALLOWED_COMMANDS.
    e.g. ["pytest", "tests/", "-v"] → "pytest" must be allowed.
    """
    binary = Path(argv[0]).name  # handles "/usr/bin/pytest" → "pytest"
    if binary not in ALLOWED_COMMANDS:
        raise HTTPException(
            400,
            f"Command {binary!r} not in allowed list. "
            f"Allowed: {sorted(ALLOWED_COMMANDS)}"
        )

# ── Schemas ───────────────────────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    command: str
    timeout: Optional[int] = None   # override CMD_TIMEOUT per-call
    cwd: Optional[str] = None       # sub-directory within workspace


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class ApplyPatchRequest(BaseModel):
    diff: str
    target: str = "live"            # "sandbox" (dry-run) or "live" (apply)


class ApplyPatchResponse(BaseModel):
    applied: bool
    message: str


class ReadFileRequest(BaseModel):
    path: str


class ReadFileResponse(BaseModel):
    path: str
    content: str
    size_bytes: int


class ListFilesRequest(BaseModel):
    pattern: str = "**/*"           # glob pattern relative to workspace


class ListFilesResponse(BaseModel):
    files: list[str]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE_DIR)}


@app.post("/execute", response_model=ExecuteResponse)
def execute(req: ExecuteRequest):
    """Run an allowed command inside the workspace."""
    argv = _parse_command(req.command)
    _check_allowed(argv)

    # Resolve working directory
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
            # Never pass a shell — keeps shell injection impossible
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


def _normalize_crlf(path: Path) -> None:
    """Convert CRLF to LF in-place. No-op if file is already LF or binary."""
    try:
        raw = path.read_bytes()
        if b"\r\n" in raw:
            path.write_bytes(raw.replace(b"\r\n", b"\n"))
    except Exception:
        pass


@app.post("/apply-patch", response_model=ApplyPatchResponse)
def apply_patch(req: ApplyPatchRequest):
    """
    Apply a unified diff to the workspace.

    With target='sandbox': runs `git apply --check` (dry-run, no changes).
    With target='live':    runs `git apply` (applies for real).

    The diff must be in unified diff format (same as `git diff` output).
    """
    if req.target not in ("sandbox", "live"):
        raise HTTPException(400, "target must be 'sandbox' or 'live'")

    # Basic sanity checks on the diff itself
    if len(req.diff) > 2_000_000:   # ~2 MB hard cap
        raise HTTPException(400, "Diff too large (>2 MB)")
    if not req.diff.strip():
        raise HTTPException(400, "Empty diff")

    # Count changed lines — reject oversized patches (spec: < 1000 lines)
    changed_lines = sum(
        1 for l in req.diff.splitlines()
        if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))
    )
    if changed_lines > 1000:
        raise HTTPException(
            400,
            f"Patch too large: {changed_lines} changed lines (limit 1000)"
        )

    # Reject binary content markers
    if "GIT binary patch" in req.diff or "Binary files" in req.diff:
        raise HTTPException(400, "Binary patches not allowed")

    # Reject chmod / permission changes
    if re.search(r"^(old|new) mode \d+", req.diff, re.MULTILINE):
        raise HTTPException(400, "Permission-change patches not allowed")

    # Normalize diff line endings (Windows CRLF → LF)
    clean_diff = req.diff.replace("\r\n", "\n").replace("\r", "\n")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, dir="/tmp", newline="\n"
    ) as f:
        f.write(clean_diff)
        patch_path = f.name

    # Normalize CRLF in all target files before applying
    for line in clean_diff.splitlines():
        if line.startswith("+++ b/"):
            target = _safe_path(line[6:].strip())
            if target.exists():
                _normalize_crlf(target)

    try:
        # --whitespace=fix: silently corrects trailing whitespace and blank-line
        # issues rather than hard-rejecting the patch. Safe for all use cases.
        if req.target == "sandbox":
            git_args = ["/usr/bin/git", "apply", "--check", "--whitespace=fix", patch_path]
        else:
            git_args = ["/usr/bin/git", "apply", "--whitespace=fix", patch_path]

        log.info("patch  target=%s  lines=%d  file=%s", req.target, changed_lines, patch_path)
        proc = subprocess.run(
            git_args,
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        os.unlink(patch_path)

    if proc.returncode != 0:
        # Log the full git error so we can debug without guessing
        log.warning("git apply failed (target=%s):\n%s", req.target, proc.stderr.strip())
        return ApplyPatchResponse(
            applied=False,
            message=f"git apply failed:\n{proc.stderr.strip()}"
        )

    verb = "Dry-run OK" if req.target == "sandbox" else "Applied"
    return ApplyPatchResponse(applied=True, message=f"{verb} ({changed_lines} changed lines)")


@app.post("/read-file", response_model=ReadFileResponse)
def read_file(req: ReadFileRequest):
    """Read a file from the workspace. Path is relative to WORKSPACE_DIR."""
    p = _safe_path(req.path)
    if not p.exists():
        raise HTTPException(404, f"File not found: {req.path!r}")
    if not p.is_file():
        raise HTTPException(400, f"Not a file: {req.path!r}")

    size = p.stat().st_size
    if size > 10_000_000:   # 10 MB guard — models can't use huge files anyway
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
    """
    Glob files within the workspace.
    Pattern is relative to WORKSPACE_DIR (e.g. '**/*.py').
    Symlinks pointing outside the workspace are silently excluded.
    """
    # Prevent absolute patterns or embedded traversal
    if req.pattern.startswith("/") or ".." in req.pattern:
        raise HTTPException(400, "Pattern must be relative and contain no '..'")

    matches = glob.glob(
        str(WORKSPACE_DIR / req.pattern),
        recursive=True,
    )

    safe = []
    for m in matches:
        try:
            p = Path(m).resolve()
            if str(p).startswith(str(WORKSPACE_DIR)) and p.is_file():
                safe.append(str(p.relative_to(WORKSPACE_DIR)))
        except Exception:
            pass

    return ListFilesResponse(files=sorted(safe))