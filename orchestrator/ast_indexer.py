"""
ast_indexer.py — Tree-sitter AST-based code chunking (Step 3.1).

Replaces line-based chunking for supported languages with function/class
boundary chunks so agents get complete, meaningful units of code rather
than arbitrary 100-line windows.

Supported languages (tree-sitter grammars installed in Dockerfile):
    Python, JavaScript, TypeScript, Go, Rust, Java, C, C++

Fallback:
    Any file whose language is unsupported or whose parse fails falls back
    to the existing line-based chunker from utils (via _line_chunk).

Each chunk dict:
    {
        "content":     str,   # source text of the symbol
        "symbol":      str,   # function/class name or "" for module-level
        "symbol_type": str,   # "function" | "class" | "method" | "module"
        "start_line":  int,   # 1-based
        "end_line":    int,   # 1-based inclusive
        "language":    str,   # "python" | "javascript" | ...
    }
"""

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("ast_indexer")

# ── Language detection ────────────────────────────────────────────────────────

_EXT_TO_LANG: dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".go":   "go",
    ".rs":   "rust",
    ".java": "java",
    ".c":    "c",
    ".cpp":  "cpp",
    ".h":    "c",
    ".hpp":  "cpp",
}

# Tree-sitter node types that represent top-level symbols per language
_SYMBOL_NODES: dict[str, list[str]] = {
    "python":     ["function_definition", "class_definition",
                   "decorated_definition", "async_function_definition"],
    "javascript": ["function_declaration", "class_declaration",
                   "method_definition", "arrow_function",
                   "lexical_declaration", "variable_declaration"],
    "typescript": ["function_declaration", "class_declaration",
                   "method_definition", "arrow_function",
                   "interface_declaration", "type_alias_declaration"],
    "go":         ["function_declaration", "method_declaration",
                   "type_declaration"],
    "rust":       ["function_item", "impl_item", "struct_item",
                   "enum_item", "trait_item"],
    "java":       ["class_declaration", "method_declaration",
                   "interface_declaration", "enum_declaration"],
    "c":          ["function_definition", "struct_specifier",
                   "declaration"],
    "cpp":        ["function_definition", "class_specifier",
                   "struct_specifier", "namespace_definition"],
}

# ── Tree-sitter setup ─────────────────────────────────────────────────────────

_parsers: dict[str, object] = {}  # lang → Parser (lazy-loaded)
_ts_available = False

try:
    from tree_sitter import Language, Parser
    import tree_sitter_python
    import tree_sitter_javascript
    import tree_sitter_typescript
    import tree_sitter_go
    import tree_sitter_rust
    import tree_sitter_java
    import tree_sitter_c
    import tree_sitter_cpp

    _LANG_MODULES = {
        "python":     tree_sitter_python,
        "javascript": tree_sitter_javascript,
        "typescript": tree_sitter_typescript.language_typescript,
        "go":         tree_sitter_go,
        "rust":       tree_sitter_rust,
        "java":       tree_sitter_java,
        "c":          tree_sitter_c,
        "cpp":        tree_sitter_cpp,
    }
    _ts_available = True
    log.info("ast_indexer: tree-sitter available")
except ImportError:
    log.warning("ast_indexer: tree-sitter not installed — falling back to line chunking")


def _get_parser(lang: str) -> Optional[object]:
    """Return a cached Parser for the given language, or None if unavailable."""
    if not _ts_available:
        return None
    if lang in _parsers:
        return _parsers[lang]
    try:
        mod = _LANG_MODULES.get(lang)
        if mod is None:
            return None
        # tree-sitter-languages API: callable or .language attribute
        if callable(mod):
            lang_obj = Language(mod())
        elif hasattr(mod, "language"):
            lang_obj = Language(mod.language())
        else:
            lang_obj = Language(mod)
        parser = Parser(lang_obj)
        _parsers[lang] = parser
        return parser
    except Exception as e:
        log.warning("ast_indexer: failed to load parser for %s: %s", lang, e)
        return None


# ── Name extraction helpers ───────────────────────────────────────────────────

def _extract_name(node, source_bytes: bytes) -> str:
    """Best-effort extraction of a symbol name from a tree-sitter node."""
    # Most languages put the name in a child node of type 'identifier' or 'name'
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier",
                          "type_identifier", "field_identifier"):
            return source_bytes[child.start_byte:child.end_byte].decode(errors="replace")
    return ""


def _node_symbol_type(node_type: str) -> str:
    if "function" in node_type or "method" in node_type or "arrow" in node_type:
        return "function"
    if "class" in node_type or "struct" in node_type or "impl" in node_type:
        return "class"
    if "interface" in node_type or "trait" in node_type or "type" in node_type:
        return "interface"
    return "declaration"


# ── Core chunking ─────────────────────────────────────────────────────────────

def chunk_file(path: str, text: str) -> list[dict]:
    """
    Chunk a source file into symbol-level pieces using tree-sitter.

    Returns a list of chunk dicts (see module docstring).
    Falls back to line-based chunking if:
      - tree-sitter is not installed
      - the file extension has no grammar
      - parsing produces no useful nodes
    """
    ext  = Path(path).suffix.lower()
    lang = _EXT_TO_LANG.get(ext)

    if lang:
        chunks = _ast_chunk(path, text, lang)
        if chunks:
            return chunks
        log.debug("ast_indexer: AST parse yielded no chunks for %s, using line fallback", path)

    return _line_chunk(path, text)


def _ast_chunk(path: str, text: str, lang: str) -> list[dict]:
    """Parse with tree-sitter and extract symbol-level chunks."""
    parser = _get_parser(lang)
    if parser is None:
        return []

    source_bytes = text.encode("utf-8", errors="replace")
    try:
        tree = parser.parse(source_bytes)
    except Exception as e:
        log.warning("ast_indexer: parse error %s: %s", path, e)
        return []

    target_types = set(_SYMBOL_NODES.get(lang, []))
    if not target_types:
        return []

    chunks  = []
    root    = tree.root_node
    lines   = text.splitlines(keepends=True)

    def visit(node, depth=0):
        if node.type in target_types:
            start  = node.start_point[0]   # 0-based row
            end    = node.end_point[0]      # 0-based row
            symbol = _extract_name(node, source_bytes)
            stype  = _node_symbol_type(node.type)
            content = "".join(lines[start: end + 1])
            if content.strip():
                chunks.append({
                    "content":     content,
                    "symbol":      symbol,
                    "symbol_type": stype,
                    "start_line":  start + 1,
                    "end_line":    end + 1,
                    "language":    lang,
                })
            # Don't recurse into children — we want top-level symbols only
            # (methods inside classes will be captured by the class node's text)
            return
        for child in node.children:
            visit(child, depth + 1)

    visit(root)

    # If the file has top-level statements not captured by any symbol node,
    # add a module-level chunk for them so nothing is lost.
    covered_lines: set[int] = set()
    for c in chunks:
        covered_lines.update(range(c["start_line"], c["end_line"] + 1))

    uncovered = [
        (i + 1, line) for i, line in enumerate(lines)
        if (i + 1) not in covered_lines and line.strip()
    ]
    if uncovered:
        content = "".join(line for _, line in uncovered)
        chunks.append({
            "content":     content,
            "symbol":      "",
            "symbol_type": "module",
            "start_line":  uncovered[0][0],
            "end_line":    uncovered[-1][0],
            "language":    lang,
        })

    return chunks


def _line_chunk(path: str, text: str, size: int = 100, overlap: int = 10) -> list[dict]:
    """
    Fallback: split into overlapping line-based windows.
    Returns chunk dicts with minimal metadata.
    """
    lines = text.splitlines(keepends=True)
    chunks = []
    i = 0
    while i < len(lines):
        window = "".join(lines[i: i + size])
        if window.strip():
            chunks.append({
                "content":     window,
                "symbol":      "",
                "symbol_type": "lines",
                "start_line":  i + 1,
                "end_line":    min(i + size, len(lines)),
                "language":    _EXT_TO_LANG.get(Path(path).suffix.lower(), "unknown"),
            })
        i += size - overlap
    return chunks or [{
        "content": text, "symbol": "", "symbol_type": "lines",
        "start_line": 1, "end_line": len(lines), "language": "unknown",
    }]