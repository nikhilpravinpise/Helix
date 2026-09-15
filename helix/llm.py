"""Thin LLM access interface. Callers never see the provider SDK's types.

Determinism policy lives here, in one place: pinned model, temperature=0,
fixed seed. Every evaluation subphase depends on this being stable and
single-sourced.

The provider is Groq's OpenAI-compatible API (decision D14): the `openai`
SDK with a configurable base_url, so the same client serves Groq's free
tier, a local Ollama, or any OpenAI-compatible endpoint - configured via
HELIX_LLM_BASE_URL / HELIX_LLM_MODEL / HELIX_LLM_API_KEY. A bespoke
multi-provider abstraction stays deferred (plan.md, Explicitly Deferred);
the config surface is the abstraction.

This restores the determinism plan.md 1.1 originally specified: the earlier
Claude 5 family rejected temperature/top_p/top_k and exposed no seed, which
is why the 1.1 implementation shipped "pinned model + pinned effort". Groq
accepts temperature and seed, so the pinned-model + temperature=0 + fixed-
seed policy is now implemented literally.
"""

import json
import time
from dataclasses import dataclass
from typing import Protocol, TypeVar

import openai
from pydantic import BaseModel

from helix.config import get_settings
from helix.observability import llm_call_counter, llm_call_latency

# Determinism policy: pinned model, temperature 0, fixed seed.
DEFAULT_MODEL = "llama-3.3-70b-versatile"
TEMPERATURE = 0.0
SEED = 42


class LLMError(Exception):
    """Provider call failed."""


class LLMStructuredOutputError(LLMError):
    """The model's response did not validate against the requested schema."""


@dataclass
class LLMResponse:
    """Provider-neutral response returned to callers."""

    text: str
    model: str
    stop_reason: str | None


T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    """Interface agents call through. No provider SDK types cross this boundary."""

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> LLMResponse: ...

    def complete_structured(self, prompt: str, schema: type[T], *, max_tokens: int = 2048) -> T: ...


class OpenAICompatibleLLMClient:
    """OpenAI-compatible implementation of LLMClient (Groq by default, D14).

    Does not require a live API key at import or construction time - only
    when a call is actually made, at which point the provider will reject a
    missing/invalid key. A placeholder key is passed to the SDK purely so
    construction never fails on missing config.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._model = model or settings.llm_model or DEFAULT_MODEL
        self._client = openai.OpenAI(
            api_key=api_key or settings.llm_api_key or "helix-missing-key",
            base_url=base_url or settings.llm_base_url,
            max_retries=3,  # SDK-level retry with backoff; free-tier rate limits need it
        )

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> LLMResponse:
        try:
            response = self._chat(prompt, max_tokens=max_tokens)
        except openai.OpenAIError as exc:
            # Provider exceptions never cross the client boundary - callers
            # see LLMError only.
            raise LLMError(f"LLM request failed: {exc}") from exc
        choice = response.choices[0]
        return LLMResponse(
            text=choice.message.content or "",
            model=response.model,
            stop_reason=choice.finish_reason,
        )

    def complete_structured(self, prompt: str, schema: type[T], *, max_tokens: int = 2048) -> T:
        """Single call constrained to JSON matching `schema`, validated by pydantic.

        Prefers the provider's JSON-schema response format; falls back to
        plain JSON mode with the schema inlined into the prompt for models
        that reject json_schema. Other provider errors become LLMError.
        """
        json_schema = {
            "name": schema.__name__,
            "schema": schema.model_json_schema(),
        }
        try:
            try:
                response = self._chat(
                    prompt,
                    max_tokens=max_tokens,
                    response_format={"type": "json_schema", "json_schema": json_schema},
                    kind="structured",
                )
            except openai.BadRequestError:
                hinted = (
                    prompt
                    + "\n\nRespond with a single JSON object matching this JSON schema:\n"
                    + json.dumps(json_schema["schema"])
                )
                response = self._chat(
                    hinted,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    kind="structured",
                )
        except openai.OpenAIError as exc:
            raise LLMError(f"structured LLM request failed: {exc}") from exc
        text = response.choices[0].message.content or ""
        try:
            return schema.model_validate_json(text)
        except Exception as exc:  # pydantic.ValidationError or json.JSONDecodeError
            raise LLMStructuredOutputError(
                f"response did not validate as {schema.__name__}: {exc}"
            ) from exc

    def _chat(
        self,
        prompt: str,
        *,
        max_tokens: int,
        response_format: dict | None = None,
        kind: str = "text",
    ):
        start = time.monotonic()
        kwargs = {
            "model": self._model,
            "temperature": TEMPERATURE,
            "seed": SEED,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        try:
            response = self._client.chat.completions.create(**kwargs)
        finally:
            llm_call_latency.record(time.monotonic() - start)
            llm_call_counter.add(1, {"model": self._model, "kind": kind})
        return response
