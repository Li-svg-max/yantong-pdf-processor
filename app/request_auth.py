from __future__ import annotations

import hashlib
import hmac
import json
import time

from pydantic import BaseModel

from .models import model_to_dict


MAX_FUTURE_TICKET_MS = 10 * 60 * 1000


def canonical_ticket(
    payload: BaseModel,
    expires_at: int,
    payload_key: str = "job",
) -> bytes:
    payload_data = model_to_dict(payload)
    # The PDF ticket is signed by the cloud function before Pydantic adds its
    # default discriminator. Keep the canonical payload identical on both
    # sides; image-batch requests carry an explicit discriminator and retain it.
    if payload_key == "job" and payload_data.get("inputKind") == "pdf":
        payload_data.pop("inputKind", None)
    payload = {
        "expiresAt": expires_at,
        payload_key: payload_data,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_ticket(
    payload: BaseModel,
    expires_at: int,
    token: str,
    payload_key: str = "job",
) -> str:
    return hmac.new(
        token.encode("utf-8"),
        canonical_ticket(payload, expires_at, payload_key),
        hashlib.sha256,
    ).hexdigest()


def verify_ticket(
    payload: BaseModel,
    expires_at: int,
    signature: str,
    token: str,
    now_ms: int | None = None,
    payload_key: str = "job",
) -> bool:
    return (
        ticket_validation_error(
            payload,
            expires_at,
            signature,
            token,
            now_ms,
            payload_key,
        )
        is None
    )


def ticket_validation_error(
    payload: BaseModel,
    expires_at: int,
    signature: str,
    token: str,
    now_ms: int | None = None,
    payload_key: str = "job",
) -> str | None:
    token = token.strip()
    if not token:
        return "processor token is not configured"
    current = int(time.time() * 1000) if now_ms is None else now_ms
    if expires_at < current:
        return "job ticket expired"
    if expires_at > current + MAX_FUTURE_TICKET_MS:
        return "job ticket timestamp is invalid"
    expected = sign_ticket(payload, expires_at, token, payload_key)
    if not hmac.compare_digest(signature, expected):
        return "job ticket signature does not match"
    return None
