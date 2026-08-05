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
    if not token:
        return False
    current = int(time.time() * 1000) if now_ms is None else now_ms
    if expires_at < current or expires_at > current + MAX_FUTURE_TICKET_MS:
        return False
    expected = sign_ticket(job, expires_at, token)
    return hmac.compare_digest(signature, expected)
