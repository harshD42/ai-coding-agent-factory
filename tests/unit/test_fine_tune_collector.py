import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch as mock_patch


@pytest.fixture
def tmp_path_collector(tmp_path):
    """Patch TRAINING_DATA_PATH to a temp file for each test."""
    import fine_tune_collector as ftc
    orig = ftc.TRAINING_DATA_PATH
    ftc.TRAINING_DATA_PATH = tmp_path / "training_data.jsonl"
    yield ftc
    ftc.TRAINING_DATA_PATH = orig


class TestFineTuneCollector:
    @pytest.mark.asyncio
    async def test_record_writes_jsonl(self, tmp_path_collector):
        ftc = tmp_path_collector
        ok  = await ftc.record_success(
            session_id="s1", agent_id="a1",
            task="add multiply", diff="--- a/f.py\n+++ b/f.py\n@@ -1 +1,2 @@\n x\n+y\n",
        )
        assert ok is True
        lines = ftc.TRAINING_DATA_PATH.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["instruction"] == "add multiply"
        assert "output" in record
        assert record["metadata"]["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_empty_task_skipped(self, tmp_path_collector):
        ftc = tmp_path_collector
        ok  = await ftc.record_success(
            session_id="s1", agent_id="a1", task="", diff="some diff"
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_empty_diff_skipped(self, tmp_path_collector):
        ftc = tmp_path_collector
        ok  = await ftc.record_success(
            session_id="s1", agent_id="a1", task="do something", diff=""
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_multiple_records_appended(self, tmp_path_collector):
        ftc = tmp_path_collector
        for i in range(3):
            await ftc.record_success(
                session_id=f"s{i}", agent_id="a",
                task=f"task {i}", diff="--- a/f\n+++ b/f\n@@ -1 +1 @@\n x\n",
            )
        records = ftc.read_records()
        assert len(records) == 3

    def test_get_stats_empty(self, tmp_path_collector):
        ftc   = tmp_path_collector
        stats = ftc.get_stats()
        assert stats["records"] == 0

    @pytest.mark.asyncio
    async def test_get_stats_after_write(self, tmp_path_collector):
        ftc = tmp_path_collector
        await ftc.record_success(
            session_id="s1", agent_id="a",
            task="task", diff="--- a/f\n+++ b/f\n@@ -1 +1 @@\n x\n",
        )
        stats = ftc.get_stats()
        assert stats["records"] == 1
        assert stats["size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_clear_deletes_records(self, tmp_path_collector):
        ftc = tmp_path_collector
        await ftc.record_success(
            session_id="s1", agent_id="a",
            task="task", diff="--- a/f\n+++ b/f\n@@ -1 +1 @@\n x\n",
        )
        count = ftc.clear_records()
        assert count == 1
        assert not ftc.TRAINING_DATA_PATH.exists()

    def test_read_records_limit(self, tmp_path_collector):
        ftc = tmp_path_collector
        # Write 5 records synchronously
        for i in range(5):
            ftc.TRAINING_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            with ftc.TRAINING_DATA_PATH.open("a") as f:
                f.write(json.dumps({"instruction": f"t{i}", "input": "",
                                    "output": "diff", "metadata": {}}) + "\n")
        records = ftc.read_records(limit=3)
        assert len(records) == 3