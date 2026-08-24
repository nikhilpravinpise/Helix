"""Thin LLM access interface. Callers never see the Anthropic SDK's types.

Determinism policy lives here, in one place: pinned model, pinned effort.
Every evaluation subphase depends on this being stable and single-sourced.

The Claude 5 model family removes the sampling controls that a determinism
policy would traditionally pin. `temperature`, `top_p`, and `top_k` are
rejected with a 400, and the API exposes no random seed. What remains is the
pinned model id plus a pinned `effort` level, so reproducibility is
established by the documented tolerance band over repeated runs that
plan.md 1.11 already requires, not by a seed.

This is deliberately a single-provider interface - a multi-provider
abstraction is deferred until a second provider is actually required (see
plan.md, Explicitly Deferred).
"""

from dataclasses import dataclass
from typing import Protocol

import anthropic

# Determinism policy: pinned model, pinned effort. No sampling parameters and
# no seed are available on this model family.
MODEL = "claude-opus-5"
EFFORT = "high"


@dataclass
class LLMResponse:
    """Provider-neutral response returned to callers."""

    text: str
    model: str
    stop_reason: str | None


class LLMClient(Protocol):
    """Interface agents call through. No Anthropic types cross this boundary."""

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> LLMResponse: ...


class AnthropicLLMClient:
    """Anthropic implementation of LLMClient.

    Does not require a live API key at import time or at construction time -
    only when complete() is actually called, at which point the underlying
    SDK client will raise if no key is configured.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> LLMResponse:
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            output_config={"effort": EFFORT},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return LLMResponse(text="", model=response.model, stop_reason="refusal")
        text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResponse(text=text, model=response.model, stop_reason=response.stop_reason)
