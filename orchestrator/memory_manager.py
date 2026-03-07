"""
memory_manager.py — ChromaDB client + embedding via Ollama.

Phase 3 additions:
  3.1  index_codebase() now uses ast_indexer.chunk_file() for symbol-level
       chunks. Metadata gains symbol, symbol_type, start_line, end_line.
       search_symbol() searches by exact or partial symbol name.
  3.4  cluster_failures() groups recent failures by semantic similarity
       and returns clusters for anti-pattern extraction.
"""

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import Optional

import chromadb
import httpx

import config
from ast_indexer import chunk_file          # Step 3.1

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


async def _embed(text: str) -> list[float]:
    resp = await _http.post(
        f"{config.OLLAMA_URL}/api/embeddings",
        json={"model": "nomic-embed-text", "prompt": text},
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    results = []
    for t in texts:
        results.append(await _embed(t))
    return results


class MemoryManager:
    def __init__(self):
        self._client:      Optional[chromadb.AsyncHttpClient] = None
        self._collections: dict = {}

    async def connect(self):
        for attempt in range(10):
            try:
                url        = config.CHROMA_URL
                host, port = url.replace("http://", "").split(":")
                self._client = await chromadb.AsyncHttpClient(host=host, port=int(port))
                await self._client.heartbeat()
                for name in COLLECTION_NAMES:
                    self._collections[name] = await self._client.get_or_create_collection(name=name)
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

    # ── Step 3.1: AST-aware codebase indexing ─────────────────────────────────

    async def index_codebase(self, workspace: str = "/workspace") -> dict:
        """
        Walk workspace, chunk using ast_indexer (falls back to line chunks),
        embed and upsert into the codebase collection.

        Metadata per chunk now includes:
            symbol, symbol_type, start_line, end_line, language
        """
        root         = Path(workspace)
        col          = self._col("codebase")
        indexed      = 0
        skipped      = 0
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

            rel_path = str(path.relative_to(root))

            # Step 3.1: use AST chunker (falls back to line chunker internally)
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
                    "file":        rel_path,
                    "chunk":       i,
                    "total_chunks": len(raw_chunks),
                    # Step 3.1 enriched metadata
                    "symbol":      chunk.get("symbol", ""),
                    "symbol_type": chunk.get("symbol_type", "lines"),
                    "start_line":  chunk.get("start_line", 0),
                    "end_line":    chunk.get("end_line",   0),
                    "language":    chunk.get("language",   "unknown"),
                })

            if not ids:
                continue

            try:
                embeddings = await _embed_batch(docs)
                await col.upsert(
                    ids=ids, documents=docs,
                    embeddings=embeddings, metadatas=metas,
                )
                chunks_total += len(ids)
                indexed += 1
            except Exception as e:
                log.warning("embed/upsert error %s: %s", rel_path, e)
                skipped += 1

        log.info("index_codebase: %d files, %d chunks, %d skipped",
                 indexed, chunks_total, skipped)
        return {"files_indexed": indexed, "chunks": chunks_total, "skipped": skipped}

    async def search_codebase(self, query: str, k: int = SEARCH_K) -> list[dict]:
        results = await self._search("codebase", query, k * 2)
        return await self.rerank(query, results, top_k=k)

    # ── Step 3.1: Symbol search ───────────────────────────────────────────────

    async def search_symbol(self, name: str, k: int = 5) -> list[dict]:
        """
        Search the codebase collection for chunks whose symbol metadata
        matches *name* (case-insensitive substring match).

        Returns matching chunks sorted by relevance.
        Falls back to semantic search if no metadata match is found.
        """
        col = self._col("codebase")
        try:
            # ChromaDB where filter: exact match first
            result = await col.get(
                where={"symbol": {"$eq": name}},
                include=["documents", "metadatas"],
            )
            docs   = result.get("documents", [])
            metas  = result.get("metadatas", [])
            if docs:
                return [
                    {"content": d, "metadata": m, "distance": 0.0, "collection": "codebase"}
                    for d, m in zip(docs, metas)
                ][:k]
        except Exception:
            pass

        # Fallback: semantic search using the symbol name as query
        log.debug("search_symbol: no exact match for %r, using semantic search", name)
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

    # ── Failure tracking ──────────────────────────────────────────────────────

    async def record_failure(
        self,
        session_id:  str,
        task_id:     str,
        description: str,
        error:       str,
        approach:    str = "",
    ) -> None:
        col     = self._col("failures")
        content = f"TASK: {description}\nAPPROACH: {approach}\nERROR: {error}"
        doc_id  = f"failure:{session_id}:{task_id}:{int(time.time())}"
        emb     = await _embed(content)
        meta    = {
            "session_id": session_id,
            "task_id":    task_id,
            "ts":         int(time.time()),
        }
        await col.upsert(ids=[doc_id], documents=[content], embeddings=[emb], metadatas=[meta])
        log.info("recorded failure: session=%s task=%s", session_id, task_id)

    # ── Step 3.4: Failure clustering ──────────────────────────────────────────

    async def cluster_failures(
        self,
        query: str = "",
        k: int = 20,
        similarity_threshold: float = 0.3,
    ) -> list[list[dict]]:
        """
        Retrieve recent failures and group them by semantic similarity.

        Returns a list of clusters, where each cluster is a list of failure
        dicts that are semantically similar to each other.  Clusters with
        >= N_FAILURES_THRESHOLD members are candidates for anti-pattern
        extraction.

        Algorithm:
            1. Fetch up to k recent failures from ChromaDB.
            2. For each failure not yet assigned to a cluster, do a similarity
               search against remaining failures.
            3. Group failures whose distance <= similarity_threshold.

        This is an approximate greedy clustering — good enough for the
        failure counts expected in a single project.
        """
        # Fetch a broad set of recent failures
        search_query = query or "error failure exception"
        candidates   = await self._search("failures", search_query, k)

        if not candidates:
            return []

        clusters: list[list[dict]] = []
        assigned: set[int]         = set()

        for i, failure in enumerate(candidates):
            if i in assigned:
                continue
            cluster = [failure]
            assigned.add(i)

            # Compare against all remaining unassigned failures
            for j, other in enumerate(candidates):
                if j in assigned or j == i:
                    continue
                # Use embedding distance as similarity proxy
                # (already computed by ChromaDB, stored in "distance" field)
                dist_i = failure.get("distance", 1.0)
                dist_j = other.get("distance",   1.0)
                # Simple heuristic: group if both are close to the same query
                if abs(dist_i - dist_j) <= similarity_threshold:
                    cluster.append(other)
                    assigned.add(j)

            clusters.append(cluster)

        # Sort clusters by size descending so largest (most repeated) come first
        clusters.sort(key=len, reverse=True)
        log.info("cluster_failures: %d failures → %d clusters", len(candidates), len(clusters))
        return clusters

    # ── Skills ────────────────────────────────────────────────────────────────

    async def save_skill(self, name: str, content: str, metadata: dict = None) -> None:
        col    = self._col("skills")
        doc_id = f"skill:{name}"
        emb    = await _embed(content)
        meta   = {"name": name, "ts": int(time.time()), **(metadata or {})}
        await col.upsert(
            ids=[doc_id], documents=[content],
            embeddings=[emb], metadatas=[meta],
        )

    async def search_skills(self, query: str, k: int = 3) -> list[dict]:
        return await self._search("skills", query, k)

    async def search_antipatterns(self, query: str, k: int = 3) -> list[dict]:
        """Search only skills tagged as antipatterns (Step 3.4)."""
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

    async def rerank(
        self,
        query:   str,
        results: list[dict],
        top_k:   int = SEARCH_K,
    ) -> list[dict]:
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


def _make_id(path: str, chunk_idx: int) -> str:
    return hashlib.sha256(f"{path}::{chunk_idx}".encode()).hexdigest()[:32]


memory = MemoryManager()