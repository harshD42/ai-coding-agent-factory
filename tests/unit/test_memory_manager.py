"""tests/unit/test_memory_manager.py — Memory manager helpers (no ChromaDB needed)."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import pytest
from memory_manager import _chunk_text, _make_id, INDEXABLE_EXTENSIONS, SKIP_DIRS


class TestChunkText:
    def test_small_text_single_chunk(self):
        text = "line1\nline2\nline3\n"
        chunks = _chunk_text(text, size=10, overlap=2)
        assert len(chunks) >= 1
        assert "line1" in chunks[0]

    def test_large_text_multiple_chunks(self):
        lines  = [f"line {i}\n" for i in range(200)]
        text   = "".join(lines)
        chunks = _chunk_text(text, size=50, overlap=5)
        assert len(chunks) > 1

    def test_overlap_means_lines_repeated(self):
        lines  = [f"line{i}\n" for i in range(20)]
        text   = "".join(lines)
        chunks = _chunk_text(text, size=10, overlap=3)
        if len(chunks) > 1:
            # Last 3 lines of chunk 0 should appear in chunk 1
            last_of_c0  = chunks[0].splitlines()[-3:]
            first_of_c1 = chunks[1].splitlines()[:3]
            assert last_of_c0 == first_of_c1

    def test_empty_text_returns_one_chunk(self):
        chunks = _chunk_text("", size=10, overlap=2)
        assert len(chunks) == 1

    def test_whitespace_only_still_returns_chunk(self):
        chunks = _chunk_text("   \n   \n", size=10, overlap=2)
        assert len(chunks) >= 1

    def test_exact_size_text(self):
        lines  = [f"x\n"] * 10
        text   = "".join(lines)
        chunks = _chunk_text(text, size=10, overlap=0)
        assert len(chunks) == 1

    def test_chunk_content_coverage(self):
        # All content should appear in at least one chunk
        lines = [f"unique_line_{i}\n" for i in range(30)]
        text  = "".join(lines)
        chunks = _chunk_text(text, size=10, overlap=2)
        all_content = "".join(chunks)
        for i in range(30):
            assert f"unique_line_{i}" in all_content


class TestMakeId:
    def test_returns_string(self):
        assert isinstance(_make_id("path/to/file.py", 0), str)

    def test_length_is_32(self):
        assert len(_make_id("file.py", 0)) == 32

    def test_stable(self):
        assert _make_id("file.py", 0) == _make_id("file.py", 0)

    def test_different_paths_different_ids(self):
        assert _make_id("a.py", 0) != _make_id("b.py", 0)

    def test_different_chunks_different_ids(self):
        assert _make_id("file.py", 0) != _make_id("file.py", 1)

    def test_hex_characters_only(self):
        id_ = _make_id("file.py", 0)
        assert all(c in "0123456789abcdef" for c in id_)


class TestIndexableExtensions:
    def test_python_indexed(self):
        assert ".py" in INDEXABLE_EXTENSIONS

    def test_javascript_indexed(self):
        assert ".js" in INDEXABLE_EXTENSIONS

    def test_markdown_indexed(self):
        assert ".md" in INDEXABLE_EXTENSIONS

    def test_binary_not_indexed(self):
        assert ".png" not in INDEXABLE_EXTENSIONS
        assert ".jpg" not in INDEXABLE_EXTENSIONS
        assert ".exe" not in INDEXABLE_EXTENSIONS
        assert ".zip" not in INDEXABLE_EXTENSIONS

    def test_common_languages_covered(self):
        for ext in (".py", ".js", ".ts", ".go", ".rs", ".java", ".cpp"):
            assert ext in INDEXABLE_EXTENSIONS, f"{ext} should be indexable"


class TestSkipDirs:
    def test_git_skipped(self):
        assert ".git" in SKIP_DIRS

    def test_node_modules_skipped(self):
        assert "node_modules" in SKIP_DIRS

    def test_venv_skipped(self):
        assert ".venv" in SKIP_DIRS
        assert "venv" in SKIP_DIRS

    def test_build_dirs_skipped(self):
        assert "dist" in SKIP_DIRS
        assert "build" in SKIP_DIRS

    def test_pycache_skipped(self):
        assert "__pycache__" in SKIP_DIRS