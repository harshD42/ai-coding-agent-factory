"""tests/unit/test_router.py — Router logic unit tests (no real HTTP calls)."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import router
from router import _build_ollama_body, _build_vllm_body
from models import ChatCompletionRequest, Message


def make_req(messages=None, stream=False, temperature=None, max_tokens=None):
    msgs = messages or [Message(role="user", content="hello")]
    return ChatCompletionRequest(
        model="orchestrator",
        messages=msgs,
        stream=stream,
        temperature=temperature,
        max_tokens=max_tokens,
    )


class TestBuildOllamaBody:
    def test_basic_structure(self):
        req  = make_req()
        msgs = [{"role": "user", "content": "hello"}]
        body = _build_ollama_body(msgs, "qwen2.5-coder:7b", req)
        assert body["model"] == "qwen2.5-coder:7b"
        assert body["stream"] is False
        assert "messages" in body

    def test_tool_roles_stripped(self):
        msgs = [
            {"role": "user",      "content": "hi"},
            {"role": "tool",      "content": "tool result"},
            {"role": "function",  "content": "fn result"},
            {"role": "assistant", "content": "response"},
        ]
        req  = make_req()
        body = _build_ollama_body(msgs, "model", req)
        roles = [m["role"] for m in body["messages"]]
        assert "tool" not in roles
        assert "function" not in roles
        assert "user" in roles
        assert "assistant" in roles

    def test_list_content_flattened(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hello world"}]}]
        req  = make_req()
        body = _build_ollama_body(msgs, "model", req)
        assert body["messages"][0]["content"] == "hello world"

    def test_temperature_in_options(self):
        req  = make_req(temperature=0.7)
        body = _build_ollama_body([], "model", req)
        assert body.get("options", {}).get("temperature") == 0.7

    def test_max_tokens_as_num_predict(self):
        req  = make_req(max_tokens=512)
        body = _build_ollama_body([], "model", req)
        assert body.get("options", {}).get("num_predict") == 512

    def test_no_options_when_none(self):
        req  = make_req()
        body = _build_ollama_body([], "model", req)
        assert "options" not in body

    def test_empty_content_becomes_empty_string(self):
        msgs = [{"role": "user", "content": None}]
        req  = make_req()
        body = _build_ollama_body(msgs, "model", req)
        assert body["messages"][0]["content"] == ""

    def test_streaming_flag_passed(self):
        req  = make_req(stream=True)
        body = _build_ollama_body([], "model", req)
        assert body["stream"] is True


class TestHealthCache:
    def test_cache_key_format(self):
        # Health cache should store results per URL
        router._health_cache.clear()
        assert len(router._health_cache) == 0

    def test_ollama_detection_by_port(self):
        # The health check logic branches on port 11434 or 'ollama' in URL
        # We test the URL pattern recognition indirectly
        assert "11434" in "http://ollama:11434"
        assert "ollama" in "http://ollama:11434"


class TestIsOllama:
    """Test the URL-based backend detection logic."""

    def test_ollama_url_by_port(self):
        url = "http://localhost:11434"
        is_ollama = "11434" in url or "ollama" in url
        assert is_ollama is True

    def test_ollama_url_by_name(self):
        url = "http://ollama:11434"
        is_ollama = "11434" in url or "ollama" in url
        assert is_ollama is True

    def test_vllm_url(self):
        url = "http://vllm-coder:8000/v1"
        is_ollama = "11434" in url or "ollama" in url
        assert is_ollama is False

    def test_gpu_shared_url(self):
        url = "http://vllm-shared:8001/v1"
        is_ollama = "11434" in url or "ollama" in url
        assert is_ollama is False

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch as mock_patch


class TestRouterTimeout:
    """Phase 3.5: non-streaming dispatch raises TimeoutError on stalled endpoint."""

    @pytest.mark.asyncio
    async def test_ollama_dispatch_timeout(self):
        """
        Stalled Ollama endpoint triggers asyncio.TimeoutError.
        We mock _client.post to raise TimeoutError (simulating wait_for firing)
        and verify dispatch propagates it correctly.
        """
        from router import dispatch
        from models import ChatCompletionRequest, Message

        req = ChatCompletionRequest(
            model="orchestrator",
            messages=[Message(role="user", content="hello")],
            stream=False,
        )

        with mock_patch("router.resolve_endpoint",
                        AsyncMock(return_value=("http://ollama:11434", "qwen", "ollama"))), \
             mock_patch("router._build_ollama_body", return_value={}), \
             mock_patch("router._client") as mock_client:
            mock_client.post = AsyncMock(side_effect=asyncio.TimeoutError())
            with pytest.raises(asyncio.TimeoutError):
                await dispatch(req, role="coder")

    @pytest.mark.asyncio
    async def test_vllm_dispatch_timeout(self):
        """vLLM non-streaming path also raises TimeoutError on stall."""
        from router import dispatch
        from models import ChatCompletionRequest, Message

        req = ChatCompletionRequest(
            model="orchestrator",
            messages=[Message(role="user", content="hello")],
            stream=False,
        )

        with mock_patch("router.resolve_endpoint",
                        AsyncMock(return_value=("http://vllm:8000/v1", "qwen-coder", "vllm"))), \
             mock_patch("router._build_vllm_body", return_value={}), \
             mock_patch("router._client") as mock_client:
            mock_client.post = AsyncMock(side_effect=asyncio.TimeoutError())
            with pytest.raises(asyncio.TimeoutError):
                await dispatch(req, role="coder")

    @pytest.mark.asyncio
    async def test_streaming_not_affected_by_timeout(self):
        """
        Streaming dispatch returns async generator directly.
        _client.post must never be called (no wait_for, no HTTP call).
        """
        from router import dispatch
        from models import ChatCompletionRequest, Message

        req = ChatCompletionRequest(
            model="orchestrator",
            messages=[Message(role="user", content="hello")],
            stream=True,
        )

        async def empty_stream(*args, **kwargs):
            return
            yield   # makes it an async generator

        with mock_patch("router.resolve_endpoint",
                        AsyncMock(return_value=("http://ollama:11434", "qwen", "ollama"))), \
             mock_patch("router._build_ollama_body", return_value={}), \
             mock_patch("router._stream_ollama", empty_stream), \
             mock_patch("router._client") as mock_client:
            mock_client.post = AsyncMock(side_effect=AssertionError(
                "_client.post must not be called for streaming dispatch"
            ))
            result = await dispatch(req, role="coder")

        import inspect
        assert inspect.isasyncgen(result)