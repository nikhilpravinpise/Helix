import httpx
import pytest
from fastapi.testclient import TestClient

from payment_api.main import app

client = TestClient(app)


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient so tests never touch the network."""

    def __init__(self, *, response=None, raises=None):
        self._response = response or _FakeResponse()
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, *args, **kwargs):
        if self._raises:
            raise self._raises
        return self._response

    async def post(self, *args, **kwargs):
        if self._raises:
            raise self._raises
        return self._response


def _patch_downstream(monkeypatch, **kwargs):
    monkeypatch.setattr(
        "payment_api.main.httpx.AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(**kwargs),
    )


def test_health_returns_200():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_200_when_dependency_up(monkeypatch):
    _patch_downstream(monkeypatch, response=_FakeResponse(200))

    response = client.get("/ready")

    assert response.status_code == 200


def test_ready_returns_503_when_dependency_unreachable(monkeypatch):
    _patch_downstream(monkeypatch, raises=httpx.ConnectError("connection refused"))

    response = client.get("/ready")

    assert response.status_code == 503


def test_payment_succeeds_with_valid_body(monkeypatch):
    _patch_downstream(monkeypatch, response=_FakeResponse(200))

    response = client.post(
        "/payments",
        json={"amount_cents": 1500, "currency": "USD", "recipient": "acct_123"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["amount_cents"] == 1500
    assert body["currency"] == "USD"
    assert body["status"] == "settled"


@pytest.mark.parametrize(
    "payload",
    [
        {"amount_cents": 0, "currency": "USD", "recipient": "acct_123"},
        {"amount_cents": 1500, "currency": "US", "recipient": "acct_123"},
        {"amount_cents": 1500, "currency": "USD", "recipient": ""},
        {"currency": "USD", "recipient": "acct_123"},
    ],
)
def test_payment_rejects_invalid_body(payload):
    response = client.post("/payments", json=payload)

    assert response.status_code == 422


def test_payment_handles_downstream_timeout(monkeypatch):
    _patch_downstream(monkeypatch, raises=httpx.TimeoutException("timed out"))

    response = client.post(
        "/payments",
        json={"amount_cents": 1500, "currency": "USD", "recipient": "acct_123"},
    )

    assert response.status_code == 502
