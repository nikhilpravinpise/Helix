"""LLM interface tests. Must not require network access or a real API key."""

from pydantic import BaseModel

from helix.llm import (
    DEFAULT_MODEL,
    SEED,
    TEMPERATURE,
    LLMResponse,
    OpenAICompatibleLLMClient,
)


class _Echo(BaseModel):
    text: str


class FakeLLMClient:
    """A stub satisfying the LLMClient protocol, for use by callers under test."""

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> LLMResponse:
        return LLMResponse(text=f"echo: {prompt}", model=DEFAULT_MODEL, stop_reason="stop")

    def complete_structured(
        self, prompt: str, schema: type[BaseModel], *, max_tokens: int = 2048
    ) -> BaseModel:
        return _Echo(text=prompt)


def test_fake_client_satisfies_protocol():
    client = FakeLLMClient()
    response = client.complete("hello")
    assert response.text == "echo: hello"
    assert response.model == DEFAULT_MODEL


def test_fake_client_structured():
    result = FakeLLMClient().complete_structured("hello", _Echo)
    assert result.text == "hello"


def test_client_constructible_without_api_key():
    # No network call is made, so no live API key is needed here.
    client = OpenAICompatibleLLMClient(api_key=None)
    assert client is not None


def test_determinism_policy_is_fixed():
    # Pinned model, temperature 0, fixed seed (D14 - Groq/OpenAI-compatible).
    assert DEFAULT_MODEL == "llama-3.3-70b-versatile"
    assert TEMPERATURE == 0.0
    assert SEED == 42
