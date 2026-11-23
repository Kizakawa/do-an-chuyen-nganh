from collections import defaultdict, deque
import threading
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class LoginRateLimitMiddleware:
    """Small in-process shield; the reverse proxy remains the global rate limiter."""

    def __init__(self, app: ASGIApp, *, max_attempts: int, window_seconds: int) -> None:
        self.app = app
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _key(self, scope: Scope) -> str:
        client = scope.get("client")
        return str(client[0]) if client else "unknown"

    def _prune(self, key: str, now: float) -> deque[float]:
        failures = self._failures[key]
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if len(self._failures) > 10_000:
            for candidate in list(self._failures):
                values = self._failures[candidate]
                while values and values[0] <= cutoff:
                    values.popleft()
                if not values:
                    self._failures.pop(candidate, None)
        return failures

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_login = (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/auth/login"
        )
        if not is_login:
            await self.app(scope, receive, send)
            return

        key = self._key(scope)
        now = time.monotonic()
        with self._lock:
            failures = self._prune(key, now)
            blocked = len(failures) >= self.max_attempts
            retry_after = (
                max(1, round(self.window_seconds - (now - failures[0])))
                if blocked
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
        with self._lock:
            if response_status == 401:
                failure_time = time.monotonic()
                self._prune(key, failure_time).append(failure_time)
            elif response_status < 400:
                self._failures.pop(key, None)
