"""LLM interface tests. Must not require network access or a real API key."""

import pytest
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


# --- Several API keys: failover on rate limits ------------------------------

import httpx  # noqa: E402
import openai  # noqa: E402

from helix import llm as llm_module  # noqa: E402
from helix.config import Settings  # noqa: E402


def _rate_limited(message="Rate limit reached ... tokens per day (TPD)"):
    request = httpx.Request("POST", "https://api.groq.test/chat/completions")
    return openai.RateLimitError(message, response=httpx.Response(429, request=request), body=None)


def _completion(text="ok"):
    from types import SimpleNamespace

    return SimpleNamespace(
        model="m",
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
    )


class _FakeOpenAI:
    """Stands in for openai.OpenAI; each key gets a scripted behaviour."""

    behaviours: dict = {}
    calls: list = []

    def __init__(self, api_key, base_url, max_retries):
        self._key = api_key
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        _FakeOpenAI.calls.append(self._key)
        outcome = _FakeOpenAI.behaviours[self._key]
        if isinstance(outcome, Exception):
            raise outcome
        return _completion(f"answered by {self._key}")


class TestKeyPool:
    def test_the_pool_is_the_main_key_then_the_extras_without_duplicates(self):
        settings = Settings(
            _env_file=None, llm_api_key="gsk_a", llm_api_keys="gsk_b, gsk_a ,gsk_c,"
        )
        assert settings.llm_api_key_pool() == ["gsk_a", "gsk_b", "gsk_c"]

    def test_extras_alone_still_make_a_pool(self):
        assert Settings(_env_file=None, llm_api_keys="gsk_b").llm_api_key_pool() == ["gsk_b"]

    def test_the_extra_keys_never_appear_in_the_settings_repr(self):
        assert "gsk_secret" not in repr(Settings(_env_file=None, llm_api_keys="gsk_secret"))


class TestFailover:
    """When Groq's daily token cap ran out mid-benchmark, every remaining
    call failed with 429 - five arm E scenarios lost to it (FOLLOWUPS #8).
    With more than one key, a refused key hands the same request to the
    next one."""

    def _client(self, monkeypatch, behaviours, keys="gsk_a,gsk_b"):
        _FakeOpenAI.behaviours = behaviours
        _FakeOpenAI.calls = []
        monkeypatch.setattr(llm_module.openai, "OpenAI", _FakeOpenAI)
        monkeypatch.setattr(llm_module, "_EXHAUSTED", {})
        monkeypatch.setenv("HELIX_LLM_API_KEY", keys.split(",")[0])
        monkeypatch.setenv("HELIX_LLM_API_KEYS", ",".join(keys.split(",")[1:]))
        return llm_module.OpenAICompatibleLLMClient()

    def test_a_rate_limited_key_hands_the_request_to_the_next(self, monkeypatch):
        client = self._client(monkeypatch, {"gsk_a": _rate_limited(), "gsk_b": "ok"})
        assert client.complete("hi").text == "answered by gsk_b"
        assert client.complete("again").text == "answered by gsk_b"
        assert _FakeOpenAI.calls == ["gsk_a", "gsk_b", "gsk_b"]  # a is not retried

    def test_a_revoked_key_is_skipped_too(self, monkeypatch):
        request = httpx.Request("POST", "https://api.groq.test/x")
        revoked = openai.AuthenticationError(
            "invalid api key", response=httpx.Response(401, request=request), body=None
        )
        client = self._client(monkeypatch, {"gsk_a": revoked, "gsk_b": "ok"})
        assert client.complete("hi").text == "answered by gsk_b"

    def test_every_key_exhausted_is_an_llm_error(self, monkeypatch):
        client = self._client(monkeypatch, {"gsk_a": _rate_limited(), "gsk_b": _rate_limited()})
        with pytest.raises(llm_module.LLMError, match="every configured API key"):
            client.complete("hi")

    def test_other_errors_do_not_rotate(self, monkeypatch):
        """A bad request is bad on every key; trying the rest only burns
        their budgets."""
        request = httpx.Request("POST", "https://api.groq.test/x")
        bad = openai.BadRequestError(
            "bad prompt", response=httpx.Response(400, request=request), body=None
        )
        client = self._client(monkeypatch, {"gsk_a": bad, "gsk_b": "ok"})
        with pytest.raises(llm_module.LLMError):
            client.complete("hi")
        assert _FakeOpenAI.calls == ["gsk_a"]

    def test_a_single_key_behaves_as_before(self, monkeypatch):
        client = self._client(monkeypatch, {"gsk_a": "ok"}, keys="gsk_a")
        assert client.complete("hi").text == "answered by gsk_a"
