"""LLM interface tests. Must not require network access or a real API key."""

from helix.llm import EFFORT, MODEL, AnthropicLLMClient, LLMResponse


class FakeLLMClient:
    """A stub satisfying the LLMClient protocol, for use by callers under test."""

    def complete(self, prompt: str, *, max_tokens: int = 1024) -> LLMResponse:
        return LLMResponse(text=f"echo: {prompt}", model=MODEL, stop_reason="end_turn")


def test_fake_client_satisfies_protocol():
    client = FakeLLMClient()
    response = client.complete("hello")
    assert response.text == "echo: hello"
    assert response.model == MODEL


def test_anthropic_client_constructible_without_api_key():
    # No network call is made, so no live API key is needed here.
    client = AnthropicLLMClient(api_key=None)
    assert client is not None


def test_determinism_policy_is_fixed():
    assert MODEL == "claude-opus-5"
    assert EFFORT == "high"
