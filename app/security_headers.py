from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings


PYODIDE_CDN_BASE = "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/"
SANDBOX_HTML_PATH = "/static/pyodide-sandbox.html"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        path = str(scope.get("path", ""))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

                if path == SANDBOX_HTML_PATH:
                    headers["X-Frame-Options"] = "SAMEORIGIN"
                    headers["Content-Security-Policy"] = (
                        "default-src 'none'; base-uri 'none'; frame-ancestors 'self'; "
                        "sandbox allow-scripts; "
                        f"script-src 'self' 'wasm-unsafe-eval' {PYODIDE_CDN_BASE} blob:; "
                        f"worker-src 'self' blob:; connect-src {PYODIDE_CDN_BASE}"
                    )
                else:
                    headers["X-Frame-Options"] = "DENY"
                    headers["Content-Security-Policy"] = (
                        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
                        "form-action 'self'; frame-src 'self'; img-src 'self' data:; style-src 'self'; "
                        f"script-src 'self' 'wasm-unsafe-eval' {PYODIDE_CDN_BASE} blob:; "
                        f"worker-src 'self' blob:; connect-src 'self' {PYODIDE_CDN_BASE}"
                    )

                if (
                    path.startswith("/api/")
                    or path.startswith("/ui/")
                    or path == "/login"
                ):
                    headers["Cache-Control"] = "no-store, private"

                if settings.auth_cookie_secure or settings.app_env == "production":
                    headers["Strict-Transport-Security"] = (
                        "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)
