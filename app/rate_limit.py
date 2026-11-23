from collections import deque
import math
import threading
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_MAX_TRACKED_KEYS = 10_000


def _prune_bucket(
    buckets: dict[str, deque[float]],
    key: str,
    now: float,
    window_seconds: int,
) -> deque[float] | None:
    cutoff = now - window_seconds
    failures = buckets.get(key)
    if failures is not None:
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            buckets.pop(key, None)
            failures = None
    if len(buckets) >= _MAX_TRACKED_KEYS:
        for candidate in list(buckets):
            values = buckets[candidate]
            while values and values[0] <= cutoff:
                values.popleft()
            if not values:
                buckets.pop(candidate, None)
        while len(buckets) >= _MAX_TRACKED_KEYS:
            oldest_key = next(iter(buckets))
            buckets.pop(oldest_key, None)
    return buckets.get(key)


class AccountLoginRateLimiter:
    """Giới hạn số lần đăng nhập sai theo từng tài khoản, không bị reset bởi IP hoặc tài khoản khác."""

    def __init__(self, *, monotonic=time.monotonic) -> None:
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._monotonic = monotonic

    def check(self, login_name: str, *, max_attempts: int, window_seconds: int) -> int:
        if not login_name:
            return 0
        now = self._monotonic()
        with self._lock:
            failures = _prune_bucket(self._failures, login_name, now, window_seconds)
            if not failures or len(failures) < max_attempts:
                return 0
            return max(1, math.ceil(window_seconds - (now - failures[0])))

    def record_failure(self, login_name: str, *, window_seconds: int) -> None:
        if not login_name:
            return
        now = self._monotonic()
        with self._lock:
            failures = _prune_bucket(self._failures, login_name, now, window_seconds)
            if failures is None:
                failures = deque()
                self._failures[login_name] = failures
            failures.append(now)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


ACCOUNT_LOGIN_LIMITER = AccountLoginRateLimiter()


class LoginRateLimitMiddleware:
    """Lá chắn IP theo tiến trình; kết hợp cùng AccountLoginRateLimiter và reverse proxy."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_attempts: int,
        window_seconds: int,
        ip_max_attempts: int | None = None,
    ) -> None:
        self.app = app
        self.max_attempts = max_attempts
        self.ip_max_attempts = (
            ip_max_attempts if ip_max_attempts is not None else max_attempts
        )
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _key(self, scope: Scope) -> str:
        client = scope.get("client")
        return str(client[0]) if client else "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        normalized_path = (scope.get("path") or "").rstrip("/")
        is_login = (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and normalized_path == "/api/auth/login"
        )
        if not is_login:
            await self.app(scope, receive, send)
            return

        key = self._key(scope)
        now = time.monotonic()
        with self._lock:
            failures = _prune_bucket(self._failures, key, now, self.window_seconds)
            blocked = failures is not None and len(failures) >= self.ip_max_attempts
            retry_after = (
                max(1, math.ceil(self.window_seconds - (now - failures[0])))
                if blocked and failures
                else 0
            )
        if blocked:
            response = JSONResponse(
                status_code=429,
                content={"detail": "login_rate_limited"},
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        response_status = 500

        async def capture_status(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = int(message["status"])
            await send(message)

        await self.app(scope, receive, capture_status)
        if response_status == 401:
            failure_time = time.monotonic()
            with self._lock:
                failures = _prune_bucket(
                    self._failures, key, failure_time, self.window_seconds
                )
                if failures is None:
                    failures = deque()
                    self._failures[key] = failures
                failures.append(failure_time)
