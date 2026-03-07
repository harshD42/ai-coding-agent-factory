"""
tests/conftest.py — Shared pytest fixtures.

Unit tests run without Docker. They mock all external dependencies
(ChromaDB, Redis, Ollama, Executor) so they run in plain CI.
"""

import sys
import os
import pytest

# Add orchestrator to path so modules can be imported directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "executor"))


# ── Minimal config overrides for testing ─────────────────────────────────────

os.environ.setdefault("PROFILE",            "laptop")
os.environ.setdefault("OLLAMA_URL",         "http://localhost:11434")
os.environ.setdefault("OLLAMA_MODEL",       "qwen2.5-coder:7b")
os.environ.setdefault("CHROMA_URL",         "http://localhost:8100")
os.environ.setdefault("REDIS_URL",          "redis://localhost:6379")
os.environ.setdefault("EXECUTOR_URL",       "http://localhost:9001")
os.environ.setdefault("MAX_CONTEXT_TOKENS", "24000")
os.environ.setdefault("MAX_DEBATE_ROUNDS",  "3")
os.environ.setdefault("MAX_AGENT_RUNTIME",  "300")
os.environ.setdefault("WORKSPACE_DIR",      "/tmp/test_workspace")


@pytest.fixture
def sample_diff():
    return (
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1 +1,2 @@\n"
        "-def hello(): pass\n"
        "+def hello():\n"
        "+    print('hello')\n"
    )


@pytest.fixture
def sample_messages():
    return [
        {"role": "system",    "content": "You are a coding assistant."},
        {"role": "user",      "content": "Write a hello world function."},
        {"role": "assistant", "content": "def hello(): print('hello world')"},
        {"role": "user",      "content": "Add a docstring."},
    ]


@pytest.fixture
def sample_tasks():
    return [
        {"id": "t1", "role": "coder",   "desc": "Write add function",  "deps": []},
        {"id": "t2", "role": "tester",  "desc": "Write tests for add", "deps": ["t1"]},
        {"id": "t3", "role": "coder",   "desc": "Write sub function",  "deps": []},
        {"id": "t4", "role": "documenter", "desc": "Write README",     "deps": ["t1", "t3"]},
    ]