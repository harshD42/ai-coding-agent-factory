"""tests/unit/test_command_parser.py — Command parsing unit tests."""

import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

from command_parser import parse, _extract_text


def msgs(*contents):
    """Helper to build a messages list with the last entry as a user message."""
    return [{"role": "user", "content": c} for c in contents]


class TestExtractText:
    def test_string_passthrough(self):
        assert _extract_text("hello") == "hello"

    def test_list_of_strings(self):
        assert _extract_text(["hello", " ", "world"]) == "hello   world"

    def test_list_of_blocks(self):
        blocks = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
        assert _extract_text(blocks) == "hello world"

    def test_mixed_list(self):
        mixed = [{"type": "text", "text": "hi"}, "there"]
        assert _extract_text(mixed) == "hi there"

    def test_none_returns_empty(self):
        assert _extract_text(None) == ""

    def test_empty_list(self):
        assert _extract_text([]) == ""


class TestParse:
    def test_no_messages_returns_none(self):
        assert parse([]) is None

    def test_no_user_message_returns_none(self):
        assert parse([{"role": "assistant", "content": "hi"}]) is None

    def test_non_command_returns_none(self):
        assert parse(msgs("hello world")) is None

    def test_architect_command(self):
        cmd = parse(msgs("/architect build a rate limiter"))
        assert cmd is not None
        assert cmd.name == "architect"
        assert cmd.args == "build a rate limiter"

    def test_debate_command(self):
        cmd = parse(msgs("/debate use Redis or Postgres"))
        assert cmd is not None
        assert cmd.name == "debate"
        assert cmd.args == "use Redis or Postgres"

    def test_status_no_args(self):
        cmd = parse(msgs("/status"))
        assert cmd is not None
        assert cmd.name == "status"
        assert cmd.args == ""

    def test_execute_no_args(self):
        cmd = parse(msgs("/execute"))
        assert cmd.name == "execute"

    def test_index_no_args(self):
        cmd = parse(msgs("/index"))
        assert cmd.name == "index"

    def test_memory_with_query(self):
        cmd = parse(msgs("/memory rate limiting patterns"))
        assert cmd.name == "memory"
        assert cmd.args == "rate limiting patterns"

    def test_unknown_command(self):
        cmd = parse(msgs("/badcommand"))
        assert cmd is not None
        assert cmd.name == "unknown"

    def test_cline_list_format(self):
        """Cline sends content as list of blocks."""
        messages = [{"role": "user", "content": [{"type": "text", "text": "/status"}]}]
        cmd = parse(messages)
        assert cmd is not None
        assert cmd.name == "status"

    def test_command_case_insensitive(self):
        cmd = parse(msgs("/ARCHITECT build something"))
        assert cmd is not None
        assert cmd.name == "architect"

    def test_last_user_message_checked_first(self):
        messages = [
            {"role": "user",      "content": "/architect old task"},
            {"role": "assistant", "content": "Here is the plan..."},
            {"role": "user",      "content": "/status"},
        ]
        cmd = parse(messages)
        assert cmd.name == "status"

    def test_command_buried_in_long_message_ignored(self):
        """A /command buried after paragraphs of text should NOT trigger."""
        long_msg = "Here is some context.\n\nPlease help me.\n\n/status"
        cmd = parse(msgs(long_msg))
        # First line is not a command — should be None
        assert cmd is None