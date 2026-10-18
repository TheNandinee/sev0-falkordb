# Tenant API keys (constant-time compare), token-bucket rate limiting, HMAC webhook signatures.
import hashlib
import hmac
import threading
import time
from typing import Optional

from fastapi import Depends, Header, HTTPException, Query, Request

from .config import settings


def _parse_keys(raw):
    keys = {}
    for part in (raw or "").split(","):
        if ":" in part:
            tenant, key = part.split(":", 1)
            if tenant.strip() and key.strip():
                keys[key.strip()] = tenant.strip()
    return keys


API_KEYS = _parse_keys(settings.api_keys_raw)


def auth_enabled():
    return bool(API_KEYS)


def resolve_tenant(request: Request, tenant: Optional[str] = Query(None),
                   x_api_key: Optional[str] = Header(None)) -> Optional[str]:
    # Open mode: tenant comes from ?tenant=. Auth mode: the key decides the tenant and ?tenant= is ignored,
    # so one customer can never read another customer's graph.
    if not API_KEYS:
        return tenant
    presented = x_api_key or request.query_params.get("api_key") or ""
    for known, owner in API_KEYS.items():
        if hmac.compare_digest(known.encode(), presented.encode()):
            return owner
    raise HTTPException(status_code=401, detail="Missing or invalid API key")


class RateLimiter:
    def __init__(self, per_minute):
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0
        self.state = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            tokens, last = self.state.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1:
                self.state[key] = (tokens, now)
                return False
            self.state[key] = (tokens - 1, now)
            return True


investigate_limiter = RateLimiter(settings.investigate_rate_per_min)


def limit_investigations(request: Request, tenant: Optional[str] = Depends(resolve_tenant)) -> Optional[str]:
    client = request.client.host if request.client else "-"
    if not investigate_limiter.allow(f"{tenant or 'default'}:{client}"):
        raise HTTPException(status_code=429, detail="Too many investigations. Slow down.")
    return tenant


def verify_signature(secret, body: bytes, header: Optional[str]) -> bool:
    if not secret:
        return True  # dev mode
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])
