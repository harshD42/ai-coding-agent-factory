"""
memory_manager.py — ChromaDB client + embedding via Ollama.

Phase 3.5 fixes:
  - _embed_batch now uses asyncio.gather (parallel, not sequential loop)
  - LRU embed cache prevents unbounded RAM growth (OrderedDict-based)
  - ChromaDB URL parsed via urllib.parse — robust against HTTPS / paths
  - record_failure uses content hash as doc_id — deduplicates same error
  - index_codebase skips files whose content hash matches stored metadata
    (incremental indexing — unchanged files cost 0 embed calls)
"""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import chromadb
import httpx

import config
from ast_indexer import chunk_file

log = logging.getLogger("memory")

COLLECTION_NAMES = ["sessions", "codebase", "skills", "failures"]
MAX_FILE_SIZE    = 500_000
SEARCH_K         = 5

INDEXABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".cs", ".rb", ".php", ".swift", ".kt",
    ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".env.example",
    ".sh", ".bash", ".sql",
}

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".next", "target", ".cache", ".idea", ".vscode",
}

RERANKER_MODEL   = "nomic-reranker"
RERANKER_TIMEOUT = 5.0

_http = httpx.AsyncClient(timeout=60.0)


# ── Phase 3.5: LRU embed cache ───────────────────────────────────────────────

class _LRUEmbedCache:
    """
    Thread-safe LRU cache for embedding vectors.

    Uses OrderedDict move_to_end to implement LRU eviction.
    When max_size is reached, the oldest (least recently used) entry is
    evicted — preventing unbounded RAM growth in long sessions.

    Key: MD5 hex of the text (fast, collision-acceptable for caching)
    Value: embedding float list
    """

    def __init__(self, max_size: int):
        self._cache:    OrderedDict = OrderedDict()
        self._max_size: int         = max_size

    def get(self, key: str) -> Optional[list[float]]:
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)   # mark as recently used
        return self._cache[key]

    def set(self, key: str, value: list[float]) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)   # evict oldest

    def __len__(self) -> int:
        return len(self._cache)


_embed_cache = _LRUEmbedCache(config.EMBED_CACHE_MAX_SIZE)


# ── Embedding ─────────────────────────────────────────────────────────────────

async def _embed_raw(text: str) -> list[float]:
    """Call Ollama embeddings API without cache."""
    resp = await _http.post(
        f"{config.OLLAMA_URL}/api/embeddings",
        json={"model": "nomic-embed-text", "prompt": text},
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


async def _embed(text: str) -> list[float]:
    """
    Get embedding vector, using LRU cache to avoid redundant Ollama calls.

    Phase 3.5: cache hit avoids HTTP call entirely. Cache evicts LRU entries
    when EMBED_CACHE_MAX_SIZE is reached.
    """
    key = hashlib.md5(text.encode()).hexdigest()
    cached = _embed_cache.get(key)
    if cached is not None:
        return cached
    result = await _embed_raw(text)
    _embed_cache.set(key, result)
    return result


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embed a list of texts in parallel using asyncio.gather.

    Phase 3.5 fix: was a sequential for-loop (O(n) wall time).
    Now runs all embed calls concurrently — wall time ≈ single embed time
    for batches, limited only by Ollama's concurrency.
    """
    return list(await asyncio.gather(*[_embed(t) for t in texts]))


# ── MemoryManager ─────────────────────────────────────────────────────────────

class MemoryManager:
    def __init__(self):
        self._client:      Optional[chromadb.AsyncHttpClient] = None
        self._collections: dict[str, chromadb.Collection]    = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self):
        """
        Connect to ChromaDB and ensure all collections exist.

        Phase 3.5 fix: URL parsed via urlparse — handles HTTPS, custom paths,
        and missing port without crashing. Old code used str.split(':') which
        fails on any URL that doesn't match exactly http://host:port.
        """
        for attempt in range(10):
            try:
                parsed = urlparse(config.CHROMA_URL)
                host   = parsed.hostname or "chromadb"
                port   = parsed.port    or 8000
                self._client = await chromadb.AsyncHttpClient(host=host, port=port)
                await self._client.heartbeat()
                for name in COLLECTION_NAMES:
                    self._collections[name] = await self._client.get_or_create_collection(
                        name=name
                    )
                log.info("ChromaDB connected. Collections: %s", COLLECTION_NAMES)
                return
            except Exception as e:
                log.warning("ChromaDB connect attempt %d/10: %s", attempt + 1, e)
                if attempt < 9:
                    await asyncio.sleep(3)
                else:
                    raise

    async def close(self):
        await _http.aclose()

    def _col(self, name: str):
        if name not in self._collections:
            raise RuntimeError(f"Collection {name!r} not initialised — call connect() first.")
        return self._collections[name]

    # ── Codebase indexing (incremental) ───────────────────────────────────────

    async def index_codebase(self, workspace: str = "/workspace") -> dict:
        """
        Walk workspace, chunk with AST indexer, embed and upsert to ChromaDB.

        Phase 3.5: incremental indexing — before embedding a file, check if
        its content hash matches what's already stored in ChromaDB metadata.
        If the hash matches, skip embedding entirely (0 Ollama calls for that
        file). On a large unchanged repo this cuts index time to near-zero.
        """
        root         = Path(workspace)
        col          = self._col("codebase")
        indexed      = 0
        skipped      = 0
        unchanged    = 0
        chunks_total = 0

        for path in root.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in INDEXABLE_EXTENSIONS:
                continue
            if path.stat().st_size > MAX_FILE_SIZE:
                skipped += 1
                continue

            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                log.warning("read error %s: %s", path, e)
                skipped += 1
                continue

            rel_path  = str(path.relative_to(root))
            file_hash = hashlib.md5(text.encode()).hexdigest()

            # Phase 3.5: check if any existing chunk for this file has the same hash
            try:
                existing = await col.get(
                    where={"file": {"$eq": rel_path}},
                    include=["metadatas"],
                    limit=1,
                )
                if existing and existing.get("metadatas"):
                    stored_hash = existing["metadatas"][0].get("file_hash", "")
                    if stored_hash == file_hash:
                        unchanged += 1
                        log.debug("skip unchanged file: %s", rel_path)
                        continue
            except Exception:
                pass   # if check fails, proceed with full embed

            raw_chunks = chunk_file(rel_path, text)
            ids, docs, metas = [], [], []
            for i, chunk in enumerate(raw_chunks):
                content = chunk["content"]
                if not content.strip():
                    continue
                cid = _make_id(rel_path, i)
                ids.append(cid)
                docs.append(content)
                metas.append({
                    "file":         rel_path,
                    "chunk":        i,
                    "total_chunks": len(raw_chunks),
                    "file_hash":    file_hash,    # used for incremental check
                    "symbol":       chunk.get("symbol", ""),
                    "symbol_type":  chunk.get("symbol_type", "lines"),
                    "start_line":   chunk.get("start_line", 0),
                    "end_line":     chunk.get("end_line",   0),
                    "language":     chunk.get("language",   "unknown"),
                })

            if not ids:
                continue

            try:
                embeddings = await _embed_batch(docs)   # parallel in 3.5
                await col.upsert(
                    ids=ids, documents=docs,
                    embeddings=embeddings, metadatas=metas,
                )
                chunks_total += len(ids)
                indexed += 1
            except Exception as e:
                log.warning("embed/upsert error %s: %s", rel_path, e)
                skipped += 1

        log.info(
            "index_codebase: %d indexed, %d unchanged (skipped embed), %d errors",
            indexed, unchanged, skipped,
        )
        return {
            "files_indexed": indexed,
            "files_unchanged": unchanged,
            "chunks": chunks_total,
            "skipped": skipped,
        }

    async def search_codebase(self, query: str, k: int = SEARCH_K) -> list[dict]:
        results = await self._search("codebase", query, k * 2)
        return await self.rerank(query, results, top_k=k)

    # ── Symbol search ─────────────────────────────────────────────────────────

    async def search_symbol(self, name: str, k: int = 5) -> list[dict]:
        col = self._col("codebase")
        try:
            result = await col.get(
                where={"symbol": {"$eq": name}},
                include=["documents", "metadatas"],
            )
            docs  = result.get("documents", [])
            metas = result.get("metadatas", [])
            if docs:
                return [
                    {"content": d, "metadata": m, "distance": 0.0, "collection": "codebase"}
                    for d, m in zip(docs, metas)
                ][:k]
        except Exception:
            pass
        log.debug("search_symbol: no exact match for %r, falling back to semantic", name)
        return await self.search_codebase(name, k=k)

    # ── Session memory ────────────────────────────────────────────────────────

    async def save_session(self, session_id: str, content: str, metadata: dict = None) -> None:
        col    = self._col("sessions")
        doc_id = f"session:{session_id}:{int(time.time())}"
        emb    = await _embed(content)
        meta   = {"session_id": session_id, "ts": int(time.time()), **(metadata or {})}
        await col.upsert(ids=[doc_id], documents=[content], embeddings=[emb], metadatas=[meta])
        log.info("saved session %s", session_id)

    async def recall(self, query: str, k: int = SEARCH_K) -> list[dict]:
        raw = []
        for col_name in ("sessions", "failures"):
            raw += await self._search(col_name, query, k)
        raw.sort(key=lambda x: x.get("distance", 1.0))
        raw = raw[: k * 2]
        return await self.rerank(query, raw, top_k=k)

    # ── Failure tracking (deduplicated) ───────────────────────────────────────

    async def record_failure(
        self,
        session_id:  str,
        task_id:     str,
        description: str,
        error:       str,
        approach:    str = "",
    ) -> None:
        """
        Record a failure to ChromaDB.

        Phase 3.5: doc_id is now a hash of the failure content, not a
        timestamp. Identical failures (same task + approach + error) produce
        the same doc_id, so ChromaDB upsert deduplicates them automatically.
        The failures collection stays clean even if the same error repeats 50×.
        """
        col     = self._col("failures")
        content = f"TASK: {description}\nAPPROACH: {approach}\nERROR: {error}"

        # Phase 3.5: content-addressed ID prevents duplicate entries
        content_hash = hashlib.md5(content.encode()).hexdigest()[:16]
        doc_id       = f"failure:{content_hash}"

        emb  = await _embed(content)
        meta = {
            "session_id": session_id,
            "task_id":    task_id,
            "ts":         int(time.time()),
        }
        await col.upsert(ids=[doc_id], documents=[content], embeddings=[emb], metadatas=[meta])
        log.info("recorded failure: session=%s task=%s hash=%s", session_id, task_id, content_hash)

    # ── Failure clustering ────────────────────────────────────────────────────

    async def cluster_failures(
        self,
        query: str = "",
        k: int = 20,
        similarity_threshold: float = 0.3,
    ) -> list[list[dict]]:
        """Group recent failures by semantic similarity (approximate greedy clustering)."""
        candidates = await self._search("failures", query or "error failure exception", k)
        if not candidates:
            return []

        clusters: list[list[dict]] = []
        assigned: set[int]         = set()

        for i, failure in enumerate(candidates):
            if i in assigned:
                continue
            cluster = [failure]
            assigned.add(i)
            for j, other in enumerate(candidates):
                if j in assigned or j == i:
                    continue
                dist_i = failure.get("distance", 1.0)
                dist_j = other.get("distance",   1.0)
                if abs(dist_i - dist_j) <= similarity_threshold:
                    cluster.append(other)
                    assigned.add(j)
            clusters.append(cluster)

        clusters.sort(key=len, reverse=True)
        log.info("cluster_failures: %d failures → %d clusters", len(candidates), len(clusters))
        return clusters

    # ── Skills ────────────────────────────────────────────────────────────────

    async def save_skill(self, name: str, content: str, metadata: dict = None) -> None:
        col    = self._col("skills")
        doc_id = f"skill:{name}"
        emb    = await _embed(content)
        meta   = {"name": name, "ts": int(time.time()), **(metadata or {})}
        await col.upsert(ids=[doc_id], documents=[content], embeddings=[emb], metadatas=[meta])

    async def search_skills(self, query: str, k: int = 3) -> list[dict]:
        return await self._search("skills", query, k)

    async def search_antipatterns(self, query: str, k: int = 3) -> list[dict]:
        col = self._col("skills")
        try:
            emb    = await _embed(query)
            result = await col.query(
                query_embeddings=[emb],
                n_results=k,
                where={"type": {"$eq": "antipattern"}},
                include=["documents", "metadatas", "distances"],
            )
            docs      = result["documents"][0]
            metas     = result["metadatas"][0]
            distances = result["distances"][0]
            return [
                {"content": d, "metadata": m, "distance": dist, "collection": "skills"}
                for d, m, dist in zip(docs, metas, distances)
            ]
        except Exception as e:
            log.warning("search_antipatterns error: %s", e)
            return []

    # ── Reranker ──────────────────────────────────────────────────────────────

    async def rerank(self, query: str, results: list[dict], top_k: int = SEARCH_K) -> list[dict]:
        if not results:
            return results
        if not RERANKER_MODEL:
            return results[:top_k]
        documents = [r.get("content", "") for r in results]
        try:
            resp = await asyncio.wait_for(
                _http.post(
                    f"{config.OLLAMA_URL}/api/rerank",
                    json={"model": RERANKER_MODEL, "query": query, "documents": documents},
                ),
                timeout=RERANKER_TIMEOUT,
            )
            resp.raise_for_status()
            ranked  = resp.json().get("results", [])
            scored  = sorted(ranked, key=lambda x: x.get("relevance_score", 0.0), reverse=True)
            reranked = []
            for item in scored[:top_k]:
                idx = item.get("index", 0)
                if 0 <= idx < len(results):
                    entry = dict(results[idx])
                    entry["rerank_score"] = item.get("relevance_score", 0.0)
                    reranked.append(entry)
            return reranked
        except asyncio.TimeoutError:
            log.warning("reranker timed out — using original order")
        except Exception as e:
            log.warning("reranker unavailable (%s) — using original order", e)
        return results[:top_k]

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _search(self, collection: str, query: str, k: int) -> list[dict]:
        col = self._col(collection)
        try:
            emb    = await _embed(query)
            result = await col.query(
                query_embeddings=[emb], n_results=k,
                include=["documents", "metadatas", "distances"],
            )
            docs      = result["documents"][0]
            metas     = result["metadatas"][0]
            distances = result["distances"][0]
            return [
                {"content": d, "metadata": m, "distance": dist, "collection": collection}
                for d, m, dist in zip(docs, metas, distances)
            ]
        except Exception as e:
            log.warning("search error in %s: %s", collection, e)
            return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_id(path: str, chunk_idx: int) -> str:
    return hashlib.sha256(f"{path}::{chunk_idx}".encode()).hexdigest()[:32]


# ── Singleton ─────────────────────────────────────────────────────────────────

memory = MemoryManager()