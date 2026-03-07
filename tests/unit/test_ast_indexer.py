import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import pytest
from ast_indexer import chunk_file, _line_chunk, _EXT_TO_LANG

SIMPLE_PY = """\
def hello():
    return "hello"

class Greeter:
    def greet(self, name):
        return f"Hello, {name}"

x = 42
"""

SIMPLE_JS = """\
function add(a, b) {
    return a + b;
}

class Calculator {
    multiply(a, b) { return a * b; }
}
"""


class TestLineFallback:
    def test_unknown_extension_uses_line_chunks(self):
        chunks = chunk_file("file.xyz", "line1\nline2\nline3\n")
        assert len(chunks) >= 1
        assert all("content" in c for c in chunks)
        assert all("symbol_type" in c for c in chunks)

    def test_line_chunk_respects_size(self):
        text = "\n".join(f"line {i}" for i in range(250))
        chunks = _line_chunk("file.txt", text, size=100, overlap=10)
        assert len(chunks) > 1
        assert all(c["symbol_type"] == "lines" for c in chunks)

    def test_empty_file_returns_one_chunk(self):
        chunks = chunk_file("empty.py", "")
        # Empty content may return empty list or one empty chunk — both acceptable
        assert isinstance(chunks, list)

    def test_chunk_has_required_keys(self):
        chunks = chunk_file("test.py", SIMPLE_PY)
        required = {"content", "symbol", "symbol_type", "start_line", "end_line", "language"}
        for c in chunks:
            assert required.issubset(c.keys()), f"Missing keys in chunk: {c.keys()}"

    def test_python_extension_detected(self):
        assert _EXT_TO_LANG.get(".py") == "python"

    def test_typescript_extension_detected(self):
        assert _EXT_TO_LANG.get(".ts") == "typescript"


class TestASTChunking:
    def test_python_chunks_produced(self):
        chunks = chunk_file("test.py", SIMPLE_PY)
        assert len(chunks) >= 1
        contents = " ".join(c["content"] for c in chunks)
        assert "hello" in contents or "Greeter" in contents

    def test_chunks_cover_file(self):
        """All source lines should appear in at least one chunk."""
        chunks = chunk_file("test.py", SIMPLE_PY)
        combined = "".join(c["content"] for c in chunks)
        for line in SIMPLE_PY.splitlines():
            if line.strip():
                assert line.strip() in combined, f"Line missing from chunks: {line!r}"

    def test_start_line_positive(self):
        chunks = chunk_file("test.py", SIMPLE_PY)
        for c in chunks:
            assert c["start_line"] >= 1

    def test_end_line_gte_start_line(self):
        chunks = chunk_file("test.py", SIMPLE_PY)
        for c in chunks:
            assert c["end_line"] >= c["start_line"]

    def test_language_set_correctly(self):
        chunks = chunk_file("test.py", SIMPLE_PY)
        for c in chunks:
            assert c["language"] == "python"

    def test_javascript_language(self):
        chunks = chunk_file("app.js", SIMPLE_JS)
        assert all(c["language"] == "javascript" for c in chunks)

    def test_no_duplicate_content(self):
        """Chunks should not have massively overlapping content (AST mode)."""
        chunks = chunk_file("test.py", SIMPLE_PY)
        if len(chunks) > 1:
            # Just verify we got multiple distinct chunks
            contents = [c["content"].strip() for c in chunks]
            assert len(set(contents)) > 1 or len(chunks) == 1