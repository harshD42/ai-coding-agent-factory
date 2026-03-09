"""
gateway.py — Optional LiteLLM gateway for multi-provider routing.

Phase 4A.4: flag-gated wrapper around LiteLLM completion.
Enabled only when USE_LITELLM=true in environment.
Default is false — zero behaviour change when disabled.

When enabled, router.dispatch() routes through gateway_dispatch() instead
of calling Ollama/vLLM directly. LiteLLM handles:
  - Provider normalisation (OpenAI, Anthropic, Ollama, vLLM, etc.)
  - Cost tracking across providers
  - Automatic retries with exponential backoff
  - API compatibility layer

Usage:
    Set USE_LITELLM=true and configure LiteLLM environment variables:
        OPENAI_API_KEY, ANTHROPIC_API_KEY, etc. as needed.

    The direct Ollama/vLLM router path is preserved and works unchanged
    when USE_LITELLM=false (the default).

Installation:
    LiteLLM is not in requirements.txt by default.
    Install manually when enabling: pip install litellm>=1.40.0
"""

import logging
from typing import AsyncIterator, Union

import config

log = logging.getLogger("gateway")


async def gateway_dispatch(
    messages:   list[dict],
    model:      str,
    stream:     bool = False,
    temperature: float = None,
    max_tokens:  int   = None,
    **kwargs,
) -> Union[dict, AsyncIterator[str]]:
    """
    Route a completion request through LiteLLM.

    Accepts the same message format as router.dispatch() and returns
    the same response shape (OpenAI-compatible dict or SSE iterator).

    Raises ImportError if litellm is not installed.
    Raises RuntimeError if USE_LITELLM is false (should not be called).
    """
    if not config.USE_LITELLM:
        raise RuntimeError(
            "gateway_dispatch() called but USE_LITELLM=false. "
            "This is a routing bug — report it."
        )

    try:
        import litellm
    except ImportError as e:
        raise ImportError(
            "LiteLLM is not installed. Run: pip install litellm>=1.40.0\n"
            "Or set USE_LITELLM=false to disable the gateway."
        ) from e

    params: dict = {
        "model":    model,
        "messages": messages,
        "stream":   stream,
    }
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    params.update(kwargs)

    log.info("gateway_dispatch  model=%s  stream=%s", model, stream)

    if stream:
        return _stream_litellm(params)
    else:
        response = await litellm.acompletion(**params)
        return response.model_dump()


async def _stream_litellm(params: dict) -> AsyncIterator[str]:
    """Yield SSE chunks from a LiteLLM streaming completion."""
    import json
    try:
        import litellm
    except ImportError as e:
        raise ImportError("LiteLLM is not installed.") from e

    try:
        async for chunk in await litellm.acompletion(**params):
            yield f"data: {json.dumps(chunk.model_dump())}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        log.error("gateway stream error: %s", e)
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"