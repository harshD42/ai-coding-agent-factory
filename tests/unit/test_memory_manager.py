"""tests/unit/test_memory_manager.py — Memory manager helpers (no ChromaDB needed)."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import pytest
# _chunk_text moved to ast_indexer as _line_chunk in Phase 3
from ast_indexer import _line_chunk
from memory_manager import _make_id, INDEXABLE_EXTENSIONS, SKIP_DIRS


class TestChunkText:
    """Tests for line-based chunking — now lives in ast_indexer._line_chunk."""

    def test_small_text_single_chunk(self):
        text   = "line1\nline2\nline3\n"
        chunks = _line_chunk("f.py", text, size=10, overlap=2)
        assert len(chunks) >= 1
        assert "line1" in chunks[0]["content"]

    def test_large_text_multiple_chunks(self):
        lines  = [f"line {i}\n" for i in range(200)]
        text   = "".join(lines)
        chunks = _line_chunk("f.py", text, size=50, overlap=5)
        assert len(chunks) > 1

    def test_overlap_means_lines_repeated(self):
        lines  = [f"line{i}\n" for i in range(20)]
        text   = "".join(lines)
        chunks = _line_chunk("f.py", text, size=10, overlap=3)
        if len(chunks) > 1:
            last_of_c0  = chunks[0]["content"].splitlines()[-3:]
            first_of_c1 = chunks[1]["content"].splitlines()[:3]
            assert last_of_c0 == first_of_c1

    def test_empty_text_returns_one_chunk(self):
        chunks = _line_chunk("f.py", "", size=10, overlap=2)
        assert len(chunks) == 1

    def test_exact_size_text(self):
        text   = "".join(["x\n"] * 10)
        chunks = _line_chunk("f.py", text, size=10, overlap=0)
        assert len(chunks) == 1

    def test_chunk_content_coverage(self):
        lines  = [f"unique_line_{i}\n" for i in range(30)]
        text   = "".join(lines)
        chunks = _line_chunk("f.py", text, size=10, overlap=2)
        all_content = "".join(c["content"] for c in chunks)
        for i in range(30):
            assert f"unique_line_{i}" in all_content

    def test_chunk_has_metadata_keys(self):
        chunks = _line_chunk("test.py", "line1\nline2\n", size=10, overlap=0)
        for c in chunks:
            assert "content"     in c
            assert "symbol_type" in c
            assert "start_line"  in c
            assert "end_line"    in c
            assert "language"    in c


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

import pytest
import asyncio
import hashlib
from unittest.mock import AsyncMock, patch as mock_patch, MagicMock


class TestLRUEmbedCache:
    """Phase 3.5: LRU cache evicts oldest entry when full."""

    def _make_cache(self, size=3):
        from memory_manager import _LRUEmbedCache
        return _LRUEmbedCache(max_size=size)

    def test_set_and_get(self):
        c = self._make_cache()
        c.set("k1", [1.0, 2.0])
        assert c.get("k1") == [1.0, 2.0]

    def test_miss_returns_none(self):
        c = self._make_cache()
        assert c.get("missing") is None

    def test_evicts_oldest_when_full(self):
        c = self._make_cache(size=3)
        c.set("k1", [1.0]); c.set("k2", [2.0]); c.set("k3", [3.0])
        c.set("k4", [4.0])   # should evict k1
        assert c.get("k1") is None
        assert c.get("k4") == [4.0]

    def test_access_refreshes_lru_order(self):
        c = self._make_cache(size=3)
        c.set("k1", [1.0]); c.set("k2", [2.0]); c.set("k3", [3.0])
        c.get("k1")           # k1 now most recently used
        c.set("k4", [4.0])   # should evict k2, not k1
        assert c.get("k1") is not None
        assert c.get("k2") is None

    def test_len(self):
        c = self._make_cache(size=5)
        c.set("a", [1.0]); c.set("b", [2.0])
        assert len(c) == 2

    def test_update_existing_moves_to_end(self):
        c = self._make_cache(size=2)
        c.set("k1", [1.0]); c.set("k2", [2.0])
        c.set("k1", [9.0])   # update k1 — k2 should be evicted next
        c.set("k3", [3.0])   # evicts k2
        assert c.get("k1") == [9.0]
        assert c.get("k2") is None


class TestEmbedCacheIntegration:
    """Phase 3.5: _embed uses LRU cache; _embed_batch is parallel."""

    @pytest.mark.asyncio
    async def test_embed_cache_hit_skips_ollama(self):
        """Second call for same text must not call Ollama."""
        from memory_manager import _embed, _embed_cache
        _embed_cache._cache.clear()

        call_count = 0
        async def fake_embed_raw(text):
            nonlocal call_count
            call_count += 1
            return [0.1, 0.2, 0.3]

        with mock_patch("memory_manager._embed_raw", fake_embed_raw):
            r1 = await _embed("hello world")
            r2 = await _embed("hello world")   # cache hit

        assert r1 == r2
        assert call_count == 1   # Ollama called only once

    @pytest.mark.asyncio
    async def test_embed_batch_runs_parallel(self):
        """_embed_batch must complete in roughly single-embed time (parallel)."""
        import time as _time
        from memory_manager import _embed_batch

        delay = 0.05   # 50ms per embed call

        async def slow_embed_raw(text):
            await asyncio.sleep(delay)
            return [float(ord(c)) for c in text[:3]]

        texts = ["abc", "def", "ghi", "jkl"]
        with mock_patch("memory_manager._embed_raw", slow_embed_raw):
            # clear cache so all calls hit the mock
            from memory_manager import _embed_cache
            _embed_cache._cache.clear()
            t0     = _time.monotonic()
            result = await _embed_batch(texts)
            elapsed = _time.monotonic() - t0

        assert len(result) == 4
        # Parallel: should finish in ~delay, not delay * len(texts)
        assert elapsed < delay * len(texts) * 0.8, \
            f"_embed_batch took {elapsed:.2f}s — appears sequential"


class TestChromaURLParsing:
    """Phase 3.5: connect() uses urlparse not str.split(':')."""

    @pytest.mark.asyncio
    async def test_standard_url_parsed(self):
        from memory_manager import MemoryManager
        m = MemoryManager()
        mock_client = AsyncMock()
        mock_client.heartbeat = AsyncMock()
        mock_client.get_or_create_collection = AsyncMock(return_value=MagicMock())

        with mock_patch("memory_manager.config") as cfg, \
             mock_patch("chromadb.AsyncHttpClient", return_value=mock_client) as client_cls:
            cfg.CHROMA_URL   = "http://chromadb:8000"
            cfg.OLLAMA_URL   = "http://ollama:11434"
            await m.connect()
            call_kwargs = client_cls.call_args.kwargs
            assert call_kwargs["host"] == "chromadb"
            assert call_kwargs["port"] == 8000

    @pytest.mark.asyncio
    async def test_non_standard_port_parsed(self):
        from memory_manager import MemoryManager
        m = MemoryManager()
        mock_client = AsyncMock()
        mock_client.heartbeat = AsyncMock()
        mock_client.get_or_create_collection = AsyncMock(return_value=MagicMock())

        with mock_patch("memory_manager.config") as cfg, \
             mock_patch("chromadb.AsyncHttpClient", return_value=mock_client) as client_cls:
            cfg.CHROMA_URL   = "http://my-chroma.internal:9999"
            cfg.OLLAMA_URL   = "http://ollama:11434"
            await m.connect()
            call_kwargs = client_cls.call_args.kwargs
            assert call_kwargs["port"] == 9999


class TestFailureDeduplication:
    """Phase 3.5: same error → same doc_id → ChromaDB deduplicates."""

    @pytest.mark.asyncio
    async def test_same_failure_same_id(self):
        from memory_manager import MemoryManager
        m = MemoryManager()
        upserted_ids = []
        mock_col = AsyncMock()
        mock_col.upsert = AsyncMock(side_effect=lambda **kw: upserted_ids.extend(kw["ids"]))
        m._collections = {"failures": mock_col}

        embed_val = [0.1] * 768
        with mock_patch("memory_manager._embed", AsyncMock(return_value=embed_val)):
            await m.record_failure("s1", "t1", "divide fn", "ZeroDivisionError", "approach A")
            await m.record_failure("s2", "t2", "divide fn", "ZeroDivisionError", "approach A")

        # Both calls must produce the same doc_id (content is identical)
        assert len(upserted_ids) == 2
        assert upserted_ids[0] == upserted_ids[1]

    @pytest.mark.asyncio
    async def test_different_failure_different_id(self):
        from memory_manager import MemoryManager
        m = MemoryManager()
        upserted_ids = []
        mock_col = AsyncMock()
        mock_col.upsert = AsyncMock(side_effect=lambda **kw: upserted_ids.extend(kw["ids"]))
        m._collections = {"failures": mock_col}

        embed_val = [0.1] * 768
        with mock_patch("memory_manager._embed", AsyncMock(return_value=embed_val)):
            await m.record_failure("s1", "t1", "task A", "ErrorOne")
            await m.record_failure("s1", "t2", "task B", "ErrorTwo")

        assert upserted_ids[0] != upserted_ids[1]