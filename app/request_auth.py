from __future__ import annotations

import hashlib
import hmac
import json
import time

from .models import PdfJobRequest, model_to_dict


MAX_FUTURE_TICKET_MS = 10 * 60 * 1000


def canonical_ticket(job: PdfJobRequest, expires_at: int) -> bytes:
    payload = {
        "expiresAt": expires_at,
        "job": model_to_dict(job),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_ticket(job: PdfJobRequest, expires_at: int, token: str) -> str:
    return hmac.new(
        token.encode("utf-8"),
        canonical_ticket(job, expires_at),
        hashlib.sha256,
    ).hexdigest()


def verify_ticket(
    job: PdfJobRequest,
    expires_at: int,
    signature: str,
    token: str,
    now_ms: int | None = None,
) -> bool:
    return ticket_validation_error(job, expires_at, signature, token, now_ms) is None


def ticket_validation_error(
    job: PdfJobRequest,
    expires_at: int,
    signature: str,
    token: str,
    now_ms: int | None = None,
) -> str | None:
    token = token.strip()
    if not token:
        return "processor token is not configured"
    current = int(time.time() * 1000) if now_ms is None else now_ms
    if expires_at < current:
        return "job ticket expired"
    if expires_at > current + MAX_FUTURE_TICKET_MS:
        return "job ticket timestamp is invalid"
    expected = sign_ticket(job, expires_at, token)
    if not hmac.compare_digest(signature, expected):
        return "job ticket signature does not match"
    return None
