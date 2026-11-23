from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.auth import router as auth_router
from app.api.hints import router as hints_router
from app.api.problems import router as problems_router
from app.api.submissions import router as submissions_router
from app.config import Settings, get_settings
from app.database import engine, get_db
from app.models import WorkerHeartbeat
from app.rate_limit import LoginRateLimitMiddleware
from app.request_logging import RequestLoggingMiddleware
from app.security_headers import SecurityHeadersMiddleware
from app.web.routes import router as web_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    engine.dispose()


def create_app(app_settings: Settings | None = None) -> FastAPI:
    current_settings = app_settings or get_settings()
    is_production = current_settings.app_env == "production"
    application = FastAPI(
        title=current_settings.app_name,
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )
    application.add_middleware(
        LoginRateLimitMiddleware,
        max_attempts=current_settings.auth_login_max_attempts,
        ip_max_attempts=max(current_settings.auth_login_max_attempts * 6, 30),
        window_seconds=current_settings.auth_login_window_seconds,
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=current_settings.trusted_host_list,
    )
    application.add_middleware(SecurityHeadersMiddleware, settings=app_settings)
    application.add_middleware(RequestLoggingMiddleware)
    application.include_router(auth_router)
    application.include_router(problems_router)
    application.include_router(submissions_router)
    application.include_router(hints_router)
    application.include_router(web_router)
    application.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Liveness: tiến trình API vẫn phục vụ request."""
        active_settings = app_settings or get_settings()
        return {"status": "ok", "service": active_settings.app_name}

    @application.get("/health/ready", tags=["system"])
    def readiness(db: Session = Depends(get_db)) -> dict[str, str]:
        """Readiness: API truy vấn được PostgreSQL ứng dụng."""
        active_settings = app_settings or get_settings()
        try:
            db.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database_unavailable",
            ) from exc
        if active_settings.app_env == "production":
            now = datetime.now(UTC)
            heartbeats = {
                item.worker_name: item
                for item in db.scalars(
                    select(WorkerHeartbeat).where(
                        WorkerHeartbeat.worker_name.in_(["grading", "hint"])
                    )
                )
            }
            stale_workers = [
                name
                for name in ("grading", "hint")
                if name not in heartbeats
                or (now - heartbeats[name].last_seen_at).total_seconds()
                > active_settings.worker_heartbeat_max_age_seconds
            ]
            if stale_workers:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "workers_unavailable", "workers": stale_workers},
                )
        return {"status": "ok", "database": "ready"}

    return application


settings = get_settings()
app = create_app()
