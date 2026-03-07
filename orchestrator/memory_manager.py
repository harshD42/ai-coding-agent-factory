"""
memory_manager.py — ChromaDB client + embedding via Ollama.

Step 2.5: rerank(query, results) added.  Called after embedding search
          in recall() and search_codebase() to improve precision.
          Uses the Ollama /api/rerank endpoint (nomic-embed-text family
          or Qwen3-Reranker-0.6B if available).  Falls back gracefully
          to the original order if the reranker is unreachable.

Collections:
    sessions   — conversation history, decisions, outcomes per session
    codebase   — embedded file chunks from the project
    skills     — learned patterns extracted from sessions
    failures   — failed patches, test failures, rejected approaches
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

log = logging.getLogger("memory")

COLLECTION_NAMES = ["sessions", "codebase", "skills", "failures"]
CHUNK_SIZE       = 100
CHUNK_OVERLAP    = 10
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

# ── Reranker config ───────────────────────────────────────────────────────────

# Reranker model name sent to Ollama's /api/rerank endpoint.
# On the laptop profile nomic-reranker may not be available; the method
# falls back silently.  Set RERANKER_MODEL="" to disable entirely.
RERANKER_MODEL   = "nomic-reranker"
RERANKER_TIMEOUT = 5.0   # seconds — skip reranking if model is slow

# ── Embedding client ──────────────────────────────────────────────────────────

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


# ── MemoryManager ─────────────────────────────────────────────────────────────

class MemoryManager:
    def __init__(self):
        self._client:      Optional[chromadb.AsyncHttpClient] = None
        self._collections: dict = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self):
        for attempt in range(10):
            try:
                url  = config.CHROMA_URL
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

    # ── Codebase indexing ─────────────────────────────────────────────────────

    async def index_codebase(self, workspace: str = "/workspace") -> dict:
        root        = Path(workspace)
        col         = self._col("codebase")
        indexed     = 0
        skipped     = 0
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
            chunks   = _chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)
            ids, docs, metas = [], [], []
            for i, chunk in enumerate(chunks):
                cid = _make_id(rel_path, i)
                ids.append(cid)
                docs.append(chunk)
                metas.append({"file": rel_path, "chunk": i, "total_chunks": len(chunks)})

            if not ids:
                continue

            try:
                embeddings = await _embed_batch(docs)
                await col.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metas)
                chunks_total += len(ids)
                indexed += 1
            except Exception as e:
                log.warning("embed/upsert error %s: %s", rel_path, e)
                skipped += 1

        log.info("index_codebase: %d files, %d chunks, %d skipped", indexed, chunks_total, skipped)
        return {"files_indexed": indexed, "chunks": chunks_total, "skipped": skipped}

    async def search_codebase(self, query: str, k: int = SEARCH_K) -> list[dict]:
        """Semantic search over indexed codebase chunks, with reranking (Step 2.5)."""
        results = await self._search("codebase", query, k * 2)   # over-fetch for reranker
        return await self.rerank(query, results, top_k=k)

    # ── Session memory ────────────────────────────────────────────────────────

    async def save_session(self, session_id: str, content: str, metadata: dict = None) -> None:
        col    = self._col("sessions")
        doc_id = f"session:{session_id}:{int(time.time())}"
        emb    = await _embed(content)
        meta   = {"session_id": session_id, "ts": int(time.time()), **(metadata or {})}
        await col.upsert(ids=[doc_id], documents=[content], embeddings=[emb], metadatas=[meta])
        log.info("saved session %s", session_id)

    async def recall(self, query: str, k: int = SEARCH_K) -> list[dict]:
        """Search sessions + failures, rerank, return top-k (Step 2.5)."""
        raw = []
        for col_name in ("sessions", "failures"):
            raw += await self._search(col_name, query, k)
        raw.sort(key=lambda x: x.get("distance", 1.0))
        raw = raw[: k * 2]   # over-fetch before reranking
        return await self.rerank(query, raw, top_k=k)

    # ── Failure tracking ──────────────────────────────────────────────────────

    async def record_failure(
        self,
        session_id: str,
        task_id:    str,
        description: str,
        error:      str,
        approach:   str = "",
    ) -> None:
        col     = self._col("failures")
        content = f"TASK: {description}\nAPPROACH: {approach}\nERROR: {error}"
        doc_id  = f"failure:{session_id}:{task_id}:{int(time.time())}"
        emb     = await _embed(content)
        meta    = {"session_id": session_id, "task_id": task_id, "ts": int(time.time())}
        await col.upsert(ids=[doc_id], documents=[content], embeddings=[emb], metadatas=[meta])
        log.info("recorded failure: session=%s task=%s", session_id, task_id)

    # ── Skills ────────────────────────────────────────────────────────────────

    async def save_skill(self, name: str, content: str) -> None:
        col    = self._col("skills")
        doc_id = f"skill:{name}"
        emb    = await _embed(content)
        await col.upsert(
            ids=[doc_id], documents=[content], embeddings=[emb],
            metadatas=[{"name": name, "ts": int(time.time())}],
        )

    async def search_skills(self, query: str, k: int = 3) -> list[dict]:
        return await self._search("skills", query, k)

    # ── Step 2.5: Reranker ────────────────────────────────────────────────────

    async def rerank(
        self,
        query:   str,
        results: list[dict],
        top_k:   int = SEARCH_K,
    ) -> list[dict]:
        """
        Re-score *results* using Ollama's /api/rerank endpoint and return
        the top_k highest-scoring items.

        Falls back to the original embedding-distance order if:
          - RERANKER_MODEL is empty
          - the reranker endpoint is unavailable
          - any error occurs

        Each result dict gains a "rerank_score" key (higher = more relevant).
        """
        if not results:
            return results

        if not RERANKER_MODEL:
            return results[:top_k]

        documents = [r.get("content", "") for r in results]
        try:
            resp = await asyncio.wait_for(
                _http.post(
                    f"{config.OLLAMA_URL}/api/rerank",
                    json={
                        "model":     RERANKER_MODEL,
                        "query":     query,
                        "documents": documents,
                    },
                ),
                timeout=RERANKER_TIMEOUT,
            )
            resp.raise_for_status()
            data    = resp.json()
            # Ollama rerank response: {"results": [{"index": N, "relevance_score": F}, ...]}
            ranked  = data.get("results", [])
            scored  = sorted(ranked, key=lambda x: x.get("relevance_score", 0.0), reverse=True)

            reranked = []
            for item in scored[:top_k]:
                idx = item.get("index", 0)
                if 0 <= idx < len(results):
                    entry = dict(results[idx])
                    entry["rerank_score"] = item.get("relevance_score", 0.0)
                    reranked.append(entry)

            log.debug("rerank: %d → %d results for query %r", len(results), len(reranked), query[:60])
            return reranked

        except asyncio.TimeoutError:
            log.warning("reranker timed out (%.1fs) — using original order", RERANKER_TIMEOUT)
        except Exception as e:
            log.warning("reranker unavailable (%s) — using original order", e)

        # Fallback: return top_k by embedding distance
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

def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
    lines  = text.splitlines(keepends=True)
    chunks = []
    i = 0
    while i < len(lines):
        chunk = "".join(lines[i: i + size])
        if chunk.strip():
            chunks.append(chunk)
        i += size - overlap
    return chunks or [text]


def _make_id(path: str, chunk_idx: int) -> str:
    return hashlib.sha256(f"{path}::{chunk_idx}".encode()).hexdigest()[:32]


# ── Singleton ─────────────────────────────────────────────────────────────────

memory = MemoryManager()