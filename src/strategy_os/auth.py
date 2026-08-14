"""Small, signed cookie gate for the single private pilot."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional


COOKIE_NAME = "strategy_os_access"


def issue_cookie(secret: str, actor_id: str = "brad") -> str:
    payload = json.dumps({"actor": actor_id, "iat": int(time.time())}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def read_cookie(secret: str, token: Optional[str]) -> Optional[str]:
    if not secret or not token or "." not in token:
        return None
    encoded, supplied = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload.get("actor") if payload.get("actor") == "brad" else None
