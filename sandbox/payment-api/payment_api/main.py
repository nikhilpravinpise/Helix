import logging
import sys
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from payment_api.config import Settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payment_api")

try:
    settings = Settings()
except Exception as exc:
    logger.error("startup failed: invalid configuration: %s", exc)
    sys.exit(1)

# Memory ballast: exists solely to make an OOMKilled fault reproducible against
# a low container memory limit. Off by default.
_ballast = bytearray(settings.ballast_mb * 1024 * 1024) if settings.ballast_mb > 0 else None

app = FastAPI(title="payment-api")


class PaymentRequest(BaseModel):
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    recipient: str = Field(min_length=1)


class PaymentRecord(BaseModel):
    id: str
    amount_cents: int
    currency: str
    recipient: str
    status: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(settings.downstream_url, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="downstream dependency unreachable") from None
    return {"status": "ready"}


@app.post("/payments", response_model=PaymentRecord, status_code=201)
async def create_payment(payment: PaymentRequest):
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                settings.downstream_url, json=payment.model_dump(), headers=headers
            )
            response.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="downstream dependency unreachable") from None

    return PaymentRecord(
        id=str(uuid.uuid4()),
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        recipient=payment.recipient,
        status="settled",
    )
