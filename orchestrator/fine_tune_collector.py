"""
fine_tune_collector.py — Training data collection for future LoRA fine-tuning (Step 3.2).

Every time a patch is successfully applied AND all tests pass, a training
record is written to /app/memory/training_data.jsonl in the Alpaca format:

    {
        "instruction": "<task description>",
        "input":       "<relevant codebase context>",
        "output":      "<unified diff that solved the task>",
        "metadata": {
            "session_id": "...",
            "agent_id":   "...",
            "ts":         1234567890,
            "tokens_in":  229,
            "tokens_out": 112,
        }
    }

On laptop profile this is pure data collection.
The JSONL file can be exported via GET /v1/finetune/export and used
offline for LoRA fine-tuning with tools like LLaMA-Factory or Axolotl.

Design rules:
    - Only successful (patch applied + tests passed) examples are recorded.
    - Records are appended atomically using a file lock.
    - The collector never raises — failures are logged and swallowed.
    - Data is stored at TRAINING_DATA_PATH (configurable via env).
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger("finetune")

TRAINING_DATA_PATH = Path(
    getattr(config, "TRAINING_DATA_PATH", "/app/memory/training_data.jsonl")
)

_write_lock = asyncio.Lock()


# ── Public API ────────────────────────────────────────────────────────────────

async def record_success(
    session_id:  str,
    agent_id:    str,
    task:        str,
    diff:        str,
    context:     str = "",
    tokens_in:   int = 0,
    tokens_out:  int = 0,
) -> bool:
    """
    Append one training record to the JSONL file.

    Returns True if the record was written, False if it was skipped or
    an error occurred.  Never raises.
    """
    if not task.strip() or not diff.strip():
        log.debug("finetune: skipping empty task or diff")
        return False

    record = {
        "instruction": task.strip(),
        "input":       context.strip(),
        "output":      diff.strip(),
        "metadata": {
            "session_id": session_id,
            "agent_id":   agent_id,
            "ts":         int(time.time()),
            "tokens_in":  tokens_in,
            "tokens_out": tokens_out,
        },
    }

    try:
        async with _write_lock:
            TRAINING_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            with TRAINING_DATA_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info(
            "finetune: recorded  session=%s  task_preview=%r",
            session_id, task[:60],
        )
        return True
    except Exception as e:
        log.warning("finetune: write failed: %s", e)
        return False


def get_stats() -> dict:
    """Return stats about the collected training data."""
    if not TRAINING_DATA_PATH.exists():
        return {"records": 0, "size_bytes": 0, "path": str(TRAINING_DATA_PATH)}
    try:
        size    = TRAINING_DATA_PATH.stat().st_size
        records = sum(1 for _ in TRAINING_DATA_PATH.open(encoding="utf-8"))
        return {
            "records":    records,
            "size_bytes": size,
            "path":       str(TRAINING_DATA_PATH),
        }
    except Exception as e:
        return {"records": -1, "size_bytes": -1, "error": str(e),
                "path": str(TRAINING_DATA_PATH)}


def read_records(limit: int = None) -> list[dict]:
    """
    Read training records from the JSONL file.
    Returns up to *limit* records (all if limit is None).
    Malformed lines are skipped.
    """
    if not TRAINING_DATA_PATH.exists():
        return []
    records = []
    try:
        with TRAINING_DATA_PATH.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("finetune: skipping malformed line")
                if limit and len(records) >= limit:
                    break
    except Exception as e:
        log.warning("finetune: read failed: %s", e)
    return records


def clear_records() -> int:
    """Delete all training records. Returns the number of records deleted."""
    if not TRAINING_DATA_PATH.exists():
        return 0
    count = get_stats().get("records", 0)
    TRAINING_DATA_PATH.unlink()
    log.info("finetune: cleared %d records", count)
    return count