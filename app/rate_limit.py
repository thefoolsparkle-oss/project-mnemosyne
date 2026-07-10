from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from .database import now_ts


AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SECONDS = 10 * 60
AUTH_BLOCK_SECONDS = 10 * 60

_AUTH_FAILURES: dict[str, dict[str, int]] = {}


def auth_rate_key(username: str, request: Any = None) -> str:
    host = "local"
    client = getattr(request, "client", None)
    if client is not None:
        host = str(getattr(client, "host", "") or "local")
    return f"{host}:{str(username or '').strip().lower()}"


def check_auth_rate_limit(key: str, ts: int | None = None) -> None:
    ts = ts or now_ts()
    entry = _AUTH_FAILURES.get(key)
    if not entry:
        return
    blocked_until = int(entry.get("blocked_until") or 0)
    if blocked_until > ts:
        retry_after = max(1, blocked_until - ts)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "auth_rate_limited",
                "message": "登录尝试太频繁，请稍后再试。",
                "retry_after_seconds": retry_after,
            },
        )
    if int(entry.get("window_started_at") or 0) + AUTH_FAILURE_WINDOW_SECONDS <= ts:
        _AUTH_FAILURES.pop(key, None)


def record_auth_failure(key: str, ts: int | None = None) -> None:
    ts = ts or now_ts()
    entry = _AUTH_FAILURES.get(key)
    if not entry or int(entry.get("window_started_at") or 0) + AUTH_FAILURE_WINDOW_SECONDS <= ts:
        entry = {"count": 0, "window_started_at": ts, "blocked_until": 0}
    entry["count"] = int(entry.get("count") or 0) + 1
    if entry["count"] >= AUTH_FAILURE_LIMIT:
        entry["blocked_until"] = ts + AUTH_BLOCK_SECONDS
    _AUTH_FAILURES[key] = entry


def reset_auth_failures(key: str) -> None:
    _AUTH_FAILURES.pop(key, None)


def reset_all_auth_failures() -> None:
    _AUTH_FAILURES.clear()
