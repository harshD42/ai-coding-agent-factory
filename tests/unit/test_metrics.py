import pytest
import time
from metrics import Metrics, parse_usage


class TestMetrics:
    def _m(self):
        return Metrics()

    def test_empty_summary(self):
        m = self._m()
        s = m.get_summary()
        assert s["total_requests"] == 0
        assert s["avg_latency_ms"] == 0.0

    def test_record_and_summarize(self):
        m = self._m()
        m.record_request("a1", "coder", tokens_in=100, tokens_out=50,
                         latency_ms=200.0, session_id="s1")
        m.record_request("a2", "coder", tokens_in=200, tokens_out=80,
                         latency_ms=400.0, session_id="s1")
        s = m.get_summary()
        assert s["total_requests"]   == 2
        assert s["total_tokens_in"]  == 300
        assert s["total_tokens_out"] == 130
        assert s["avg_latency_ms"]   == 300.0

    def test_by_role_breakdown(self):
        m = self._m()
        m.record_request("a1", "coder",    tokens_in=10, tokens_out=5,  latency_ms=100)
        m.record_request("a2", "reviewer", tokens_in=20, tokens_out=10, latency_ms=200)
        s = m.get_summary()
        assert "coder"    in s["by_role"]
        assert "reviewer" in s["by_role"]
        assert s["by_role"]["coder"]["requests"] == 1

    def test_session_summary(self):
        m = self._m()
        m.record_request("a1", "coder", tokens_in=10, tokens_out=5,
                         latency_ms=100, session_id="sess-x")
        m.record_request("a2", "coder", tokens_in=20, tokens_out=8,
                         latency_ms=200, session_id="sess-y")
        s = m.get_session_summary("sess-x")
        assert s["requests"] == 1
        assert s["tokens_in"] == 10

    def test_reset(self):
        m = self._m()
        m.record_request("a1", "coder", tokens_in=5, tokens_out=3, latency_ms=50)
        m.reset()
        assert m.get_summary()["total_requests"] == 0

    def test_failed_status_counted(self):
        m = self._m()
        m.record_request("a1", "coder", tokens_in=0, tokens_out=0,
                         latency_ms=50, status="failed")
        s = m.get_summary()
        assert s["by_role"]["coder"]["failed"] == 1


class TestParseUsage:
    def test_openai_style(self):
        resp = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        assert parse_usage(resp) == (100, 50)

    def test_ollama_flat(self):
        resp = {"prompt_eval_count": 80, "eval_count": 40}
        assert parse_usage(resp) == (80, 40)

    def test_empty(self):
        assert parse_usage({}) == (0, 0)

    def test_partial_openai(self):
        resp = {"usage": {"prompt_tokens": 30}}
        tin, tout = parse_usage(resp)
        assert tin == 30 and tout == 0

    def test_openai_wins_over_flat(self):
        # If both present, OpenAI usage block takes priority
        resp = {
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "prompt_eval_count": 999, "eval_count": 999,
        }
        assert parse_usage(resp) == (10, 5)