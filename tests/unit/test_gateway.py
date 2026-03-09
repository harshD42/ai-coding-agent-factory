"""
tests/unit/test_gateway.py — Unit tests for Phase 4A.4 LiteLLM gateway.

The gateway is flag-gated (USE_LITELLM=false by default). Tests verify:
  - gateway_dispatch() raises RuntimeError when flag is false
  - gateway_dispatch() raises ImportError when litellm not installed
  - router.dispatch() still uses direct path when USE_LITELLM=false
  - router.dispatch() routes through gateway when USE_LITELLM=true
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import config


# ── gateway_dispatch() ────────────────────────────────────────────────────────

class TestGatewayDispatch:

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_flag_false(self):
        """gateway_dispatch must never be called when USE_LITELLM=false."""
        from gateway import gateway_dispatch
        with patch.object(config, "USE_LITELLM", False):
            with pytest.raises(RuntimeError, match="USE_LITELLM=false"):
                await gateway_dispatch(
                    messages=[{"role": "user", "content": "hi"}],
                    model="qwen2.5-coder:7b",
                )

    @pytest.mark.asyncio
    async def test_raises_import_error_when_litellm_not_installed(self):
        """If litellm package is missing, raise ImportError with install instructions."""
        from gateway import gateway_dispatch
        with patch.object(config, "USE_LITELLM", True), \
             patch.dict("sys.modules", {"litellm": None}):
            with pytest.raises((ImportError, TypeError)):
                await gateway_dispatch(
                    messages=[{"role": "user", "content": "hi"}],
                    model="qwen2.5-coder:7b",
                )

    @pytest.mark.asyncio
    async def test_calls_litellm_acompletion_when_enabled(self):
        """When USE_LITELLM=true and litellm is available, call acompletion."""
        mock_response = MagicMock()
        mock_response.model_dump.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}]
        }

        mock_litellm = MagicMock()
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)

        from gateway import gateway_dispatch
        with patch.object(config, "USE_LITELLM", True), \
             patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = await gateway_dispatch(
                messages=[{"role": "user", "content": "hello"}],
                model="gpt-4",
                stream=False,
            )

        mock_litellm.acompletion.assert_called_once()
        call_kwargs = mock_litellm.acompletion.call_args[1]
        assert call_kwargs["model"]    == "gpt-4"
        assert call_kwargs["stream"]   is False
        assert call_kwargs["messages"] == [{"role": "user", "content": "hello"}]

    @pytest.mark.asyncio
    async def test_passes_temperature_and_max_tokens(self):
        """Optional params forwarded to litellm if provided."""
        mock_response = MagicMock()
        mock_response.model_dump.return_value = {"choices": []}

        mock_litellm = MagicMock()
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)

        from gateway import gateway_dispatch
        with patch.object(config, "USE_LITELLM", True), \
             patch.dict("sys.modules", {"litellm": mock_litellm}):
            await gateway_dispatch(
                messages=[{"role": "user", "content": "test"}],
                model="gpt-4",
                temperature=0.7,
                max_tokens=512,
            )

        kwargs = mock_litellm.acompletion.call_args[1]
        assert kwargs["temperature"] == 0.7
        assert kwargs["max_tokens"]  == 512

    @pytest.mark.asyncio
    async def test_none_params_not_forwarded(self):
        """temperature=None and max_tokens=None must not be sent to litellm."""
        mock_response = MagicMock()
        mock_response.model_dump.return_value = {"choices": []}

        mock_litellm = MagicMock()
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)

        from gateway import gateway_dispatch
        with patch.object(config, "USE_LITELLM", True), \
             patch.dict("sys.modules", {"litellm": mock_litellm}):
            await gateway_dispatch(
                messages=[{"role": "user", "content": "test"}],
                model="gpt-4",
                temperature=None,
                max_tokens=None,
            )

        kwargs = mock_litellm.acompletion.call_args[1]
        assert "temperature" not in kwargs
        assert "max_tokens"  not in kwargs


# ── router.dispatch() gateway integration ────────────────────────────────────

class TestRouterGatewayIntegration:
    """
    Verify router.dispatch() honours the USE_LITELLM flag correctly.
    When false: direct path used (no gateway import).
    When true:  gateway_dispatch() called.
    """

    @pytest.mark.asyncio
    async def test_router_uses_direct_path_when_flag_false(self):
        """USE_LITELLM=false must not touch gateway at all."""
        from models import ChatCompletionRequest, Message
        import router

        req = ChatCompletionRequest(
            model="orchestrator",
            messages=[Message(role="user", content="hello")],
            stream=False,
        )

        mock_response = {
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {},
        }

        with patch.object(config, "USE_LITELLM", False), \
             patch.object(router, "resolve_endpoint",
                          AsyncMock(return_value=("http://ollama:11434", "qwen2.5-coder:7b", "ollama"))), \
             patch.object(router, "_is_healthy", AsyncMock(return_value=True)), \
             patch("router._client") as mock_client:
            mock_client.post = AsyncMock(return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value={
                    "message": {"content": "hi"},
                    "prompt_eval_count": 10,
                    "eval_count": 5,
                }),
                raise_for_status=MagicMock(),
            ))
            result = await router.dispatch(req, role="coder")

        assert isinstance(result, dict)
        assert "choices" in result

    @pytest.mark.asyncio
    async def test_router_calls_gateway_when_flag_true(self):
        """
        USE_LITELLM=true must route through gateway_dispatch().
        We verify this by patching gateway_dispatch at its source module
        and confirming it is called when the flag is set.
        """
        from models import ChatCompletionRequest, Message
        import router

        req = ChatCompletionRequest(
            model="orchestrator",
            messages=[Message(role="user", content="hello")],
            stream=False,
        )

        mock_gateway_result = {
            "choices": [{"message": {"role": "assistant", "content": "via litellm"}}]
        }

        with patch.object(config, "USE_LITELLM", True), \
             patch.object(router, "resolve_endpoint",
                          AsyncMock(return_value=("http://ollama:11434", "qwen2.5-coder:7b", "ollama"))), \
             patch("gateway.gateway_dispatch", AsyncMock(return_value=mock_gateway_result)) as mock_gw:
            result = await router.dispatch(req, role="coder")

        mock_gw.assert_called_once()
        assert result == mock_gateway_result