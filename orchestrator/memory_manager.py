"""
memory_manager.py — ChromaDB client + embedding via Ollama.

Collections:
    sessions   — conversation history, decisions, outcomes per session
    codebase   — embedded file chunks from the project
    skills     — learned patterns extracted from sessions
    failures   — failed patches, test failures, rejected approaches

Embedding:
    Uses Ollama's /api/embeddings endpoint with nomic-embed-text.
    Falls back gracefully if Ollama is unreachable during startup.
"""

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

import chromadb
import httpx

import config

log = logging.getLogger("memory")

# ── Constants ─────────────────────────────────────────────────────────────────

COLLECTION_NAMES = ["sessions", "codebase", "skills", "failures"]
CHUNK_SIZE       = 100   # lines per chunk when indexing files
CHUNK_OVERLAP    = 10    # lines of overlap between chunks
MAX_FILE_SIZE    = 500_000  # bytes — skip files larger than this
SEARCH_K         = 5        # default number of results to return

# File extensions to index
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

# ── Embedding client ──────────────────────────────────────────────────────────

_http = httpx.AsyncClient(timeout=60.0)

async def _embed(text: str) -> list[float]:
    """
    Get embedding vector from Ollama nomic-embed-text.
    Returns a 768-dim float list.
    """
    resp = await _http.post(
        f"{config.OLLAMA_URL}/api/embeddings",
        json={"model": "nomic-embed-text", "prompt": text},
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts sequentially (Ollama has no batch endpoint)."""
    results = []
    for t in texts:
        results.append(await _embed(t))
    return results


# ── MemoryManager ─────────────────────────────────────────────────────────────

class MemoryManager:
    def __init__(self):
        self._client: Optional[chromadb.AsyncHttpClient] = None
        self._collections: dict = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self):
        """Connect to ChromaDB and ensure all collections exist. Retries up to 10x."""
        for attempt in range(10):
            try:
                url = config.CHROMA_URL
                host, port = url.replace("http://", "").split(":")
                self._client = await chromadb.AsyncHttpClient(host=host, port=int(port))
                await self._client.heartbeat()
                for name in COLLECTION_NAMES:
                    self._collections[name] = await self._client.get_or_create_collection(name=name)
                log.info("ChromaDB connected. Collections: %s", COLLECTION_NAMES)
                return
            except Exception as e:
                log.warning("ChromaDB connect attempt %d/10 failed: %s", attempt + 1, e)
                if attempt < 9:
                    await asyncio.sleep(3)
                else:
                    raise

    async def close(self):
        await _http.aclose()

    def _col(self, name: str):
        if name not in self._collections:
            raise RuntimeError(f"Collection {name!r} not initialised. Call connect() first.")
        return self._collections[name]

    # ── Codebase indexing ─────────────────────────────────────────────────────

    async def index_codebase(self, workspace: str = "/workspace") -> dict:
        """
        Walk the workspace, chunk all indexable files, embed and upsert
        into the 'codebase' collection. Returns a summary dict.
        """
        root    = Path(workspace)
        col     = self._col("codebase")
        indexed = 0
        skipped = 0
        chunks_total = 0

        for path in root.rglob("*"):
            # Skip directories and hidden/build dirs
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in INDEXABLE_EXTENSIONS:
                continue
            if path.stat().st_size > MAX_FILE_SIZE:
                log.debug("skip large file: %s", path)
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
                chunk_id = _make_id(rel_path, i)
                ids.append(chunk_id)
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
        """Semantic search over indexed codebase chunks."""
        return await self._search("codebase", query, k)

    # ── Session memory ────────────────────────────────────────────────────────

    async def save_session(self, session_id: str, content: str, metadata: dict = None) -> None:
        """Persist a session summary to ChromaDB."""
        col = self._col("sessions")
        doc_id = f"session:{session_id}:{int(time.time())}"
        emb    = await _embed(content)
        meta   = {"session_id": session_id, "ts": int(time.time()), **(metadata or {})}
        await col.upsert(ids=[doc_id], documents=[content], embeddings=[emb], metadatas=[meta])
        log.info("saved session %s", session_id)

    async def recall(self, query: str, k: int = SEARCH_K) -> list[dict]:
        """Search across sessions + failures for relevant past context."""
        results = []
        for col_name in ("sessions", "failures"):
            results += await self._search(col_name, query, k)
        # Sort by distance (lower = more relevant) and return top-k overall
        results.sort(key=lambda x: x.get("distance", 1.0))
        return results[:k]

    # ── Failure tracking ──────────────────────────────────────────────────────

    async def record_failure(
        self,
        session_id: str,
        task_id: str,
        description: str,
        error: str,
        approach: str = "",
    ) -> None:
        """Record a failure so agents can avoid repeating the same mistakes."""
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

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _search(self, collection: str, query: str, k: int) -> list[dict]:
        col = self._col(collection)
        try:
            emb    = await _embed(query)
            result = await col.query(query_embeddings=[emb], n_results=k, include=["documents", "metadatas", "distances"])
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
    """Split text into overlapping line-based chunks."""
    lines  = text.splitlines(keepends=True)
    chunks = []
    i = 0
    while i < len(lines):
        chunk = "".join(lines[i : i + size])
        if chunk.strip():
            chunks.append(chunk)
        i += size - overlap
    return chunks or [text]  # always return at least one chunk


def _make_id(path: str, chunk_idx: int) -> str:
    """Stable, collision-resistant chunk ID."""
    raw = f"{path}::{chunk_idx}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Module-level singleton ────────────────────────────────────────────────────

memory = MemoryManager()