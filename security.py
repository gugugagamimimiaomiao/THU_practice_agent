"""Authentication and lightweight abuse protection for public deployment."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque


LOCAL_DEV_KEY = "dev-local-key"


def is_production() -> bool:
    return os.getenv("PRACTICE_XIAODA_ENV", "development").strip().lower() == "production"


def configured_api_keys() -> list[str]:
    raw = os.getenv("XIAODA_API_KEYS") or os.getenv("XIAODA_API_KEY") or ""
    keys = [item.strip() for item in raw.split(",") if item.strip()]
    if not keys and not is_production():
        return [LOCAL_DEV_KEY]
    return keys


def chat_auth_ready() -> bool:
    return bool(configured_api_keys())


def bearer_token(header: str | None) -> str:
    if not header:
        return ""
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def verify_bearer(header: str | None, *, admin: bool = False, keys_env: str = "") -> bool:
    token = bearer_token(header)
    if not token:
        return False
    if keys_env:
        # 指定环境变量的独立钥匙串（目前用于 INGEST_API_KEYS）。没配就是没开，
        # 不回退到别的密钥——否则"我以为只发了投稿权限"会变成发了全部权限。
        expected = [item.strip() for item in (os.getenv(keys_env) or "").split(",") if item.strip()]
    elif admin:
        raw = os.getenv("ADMIN_API_KEYS") or os.getenv("ADMIN_API_KEY") or ""
        expected = [item.strip() for item in raw.split(",") if item.strip()]
        if not expected and not is_production():
            expected = configured_api_keys()
    else:
        expected = configured_api_keys()
    return any(hmac.compare_digest(token, candidate) for candidate in expected)


def token_fingerprint(header: str | None, fallback: str) -> str:
    token = bearer_token(header)
    value = token or fallback
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:20]


class SlidingWindowRateLimiter:
    """Small per-process limiter suitable for one MVP instance.

    Use a shared gateway/Redis limiter when horizontally scaling beyond one instance.
    """

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, identity: str, *, limit: int | None = None, window_seconds: int = 60) -> tuple[bool, int, int]:
        limit = limit or max(1, int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")))
        now = time.monotonic()
        floor = now - window_seconds
        with self._lock:
            events = self._events[identity]
            while events and events[0] <= floor:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(1, int(window_seconds - (now - events[0])) + 1)
                return False, 0, retry_after
            events.append(now)
            return True, max(0, limit - len(events)), 0


RATE_LIMITER = SlidingWindowRateLimiter()

