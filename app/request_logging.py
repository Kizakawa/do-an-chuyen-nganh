import logging
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


logger = logging.getLogger("app.requests")


def _sanitize_log_value(value: str) -> str:
    cleaned: list[str] = []
    for ch in value:
        code = ord(ch)
        if ch == "\r":
            cleaned.append("\\r")
        elif ch == "\n":
            cleaned.append("\\n")
        elif ch == "\t":
            cleaned.append("\\t")
        elif code < 0x20 or code == 0x7F:
            cleaned.append(f"\\x{code:02x}")
        else:
            cleaned.append(ch)
    return "".join(cleaned)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        safe_method = _sanitize_log_value(request.method)
        safe_path = _sanitize_log_value(request.url.path)
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed request_id=%s method=%s path=%s",
                request_id,
                safe_method,
                safe_path,
            )
            raise
        duration_ms = round((time.monotonic() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_finished request_id=%s method=%s path=%s status=%s duration_ms=%s",
            request_id,
            safe_method,
            safe_path,
            response.status_code,
            duration_ms,
        )
        return response
