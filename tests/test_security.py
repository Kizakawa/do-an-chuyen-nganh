from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from starlette.requests import Request

from app import worker
from app.api.dependencies import get_current_student, get_current_web_student
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.main import app, create_app
from app.models import Student
from app.rate_limit import AccountLoginRateLimiter, LoginRateLimitMiddleware
from app.request_logging import _sanitize_log_value
from app.security import (
    InvalidSessionToken,
    create_session_token,
    decode_session_token,
    hash_password,
    verify_password,
)
from app.seed import seed_all


def test_password_hash_and_session_signature() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("incorrect password", encoded)

    token, csrf = create_session_token(
        student_id=7, secret="s" * 32, ttl_seconds=300, now=1_000
    )
    payload = decode_session_token(token, secret="s" * 32, now=1_001)
    assert payload["sid"] == 7
    assert payload["csrf"] == csrf

    try:
        decode_session_token(token + "tampered", secret="s" * 32, now=1_001)
    except InvalidSessionToken:
        pass
    else:
        raise AssertionError("tampered session token must be rejected")


def test_production_configuration_requires_secure_worker_mode() -> None:
    try:
        Settings(app_env="production")
    except ValueError as exc:
        assert "production_requires_auth_mode_session" in str(exc)
    else:
        raise AssertionError("insecure production settings must be rejected")

    settings = Settings(
        app_env="production",
        auth_mode="session",
        auth_session_secret="s" * 48,
        auth_cookie_secure=True,
        hint_execution_mode="worker",
        hint_request_expire_seconds=300,
        database_url="postgresql+psycopg://app:secret@db:5432/app",
        judge0_base_url="https://judge0.internal.example.edu",
        judge0_language_id=100,
        llm_api_key="real-secret-for-validation",
        trusted_hosts="practice.example.edu",
    )
    assert settings.app_env == "production"


def test_login_rate_limiter_blocks_repeated_failures() -> None:
    test_app = FastAPI()
    test_app.add_middleware(
        LoginRateLimitMiddleware, max_attempts=2, window_seconds=60
    )

    @test_app.post("/api/auth/login")
    def always_reject() -> None:
        raise HTTPException(status_code=401, detail="invalid_credentials")

    with TestClient(test_app) as rate_client:
        assert rate_client.post("/api/auth/login").status_code == 401
        assert rate_client.post("/api/auth/login").status_code == 401
        blocked = rate_client.post("/api/auth/login")
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"


def test_session_login_and_csrf_protection(clean_database: None, monkeypatch) -> None:
    with SessionLocal.begin() as db:
        seed_all(db)
        student = db.scalar(select(Student).where(Student.login_name == "demo"))
        assert student is not None
        student.password_hash = hash_password("a secure demo password")

    monkeypatch.setenv("AUTH_MODE", "session")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "x" * 48)
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            unauthenticated = client.get("/api/problems")
            assert unauthenticated.status_code == 401

            logged_in = client.post(
                "/api/auth/login",
                json={"login_name": "demo", "password": "a secure demo password"},
            )
            assert logged_in.status_code == 200
            assert logged_in.json()["login_name"] == "demo"
            csrf = client.cookies.get("python_practice_csrf")
            assert csrf

            problem_id = client.get("/api/problems").json()[0]["id"]
            rejected = client.post(
                "/api/submissions",
                json={"problem_id": problem_id, "source_code": "def f(): return 1"},
            )
            assert rejected.status_code == 403

            accepted = client.post(
                "/api/submissions",
                headers={"X-CSRF-Token": csrf},
                json={"problem_id": problem_id, "source_code": "def f(): return 1"},
            )
            assert accepted.status_code == 202

            logged_out = client.post(
                "/api/auth/logout", headers={"X-CSRF-Token": csrf}
            )
            assert logged_out.status_code == 204
            assert client.get("/api/problems").status_code == 401
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_worker_threadpool_sleeps_when_idle_and_stops_cleanly(monkeypatch) -> None:
    import threading

    results = iter([True, False, False, False, False])
    lock = threading.Lock()
    sleep_calls: list[float] = []
    heartbeats: list[tuple[str, dict]] = []
    closed_flag = {"closed": False}

    class FakeJudge:
        def __init__(self, _settings: Settings) -> None:
            pass

        def close(self) -> None:
            closed_flag["closed"] = True

    class FakeService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def process_next(self) -> bool:
            with lock:
                return next(results, False)

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            worker._stop_requested = True

    monkeypatch.setattr(worker, "Judge0Client", FakeJudge)
    monkeypatch.setattr(worker, "GradingService", FakeService)
    monkeypatch.setattr(
        worker, "record_heartbeat", lambda name, details=None: heartbeats.append((name, details or {}))
    )
    monkeypatch.setattr(worker.time, "sleep", fake_sleep)

    worker._stop_requested = False
    try:
        worker.run(once=False, idle_seconds=0.01, concurrency=3)
    finally:
        worker._stop_requested = False

    assert len(sleep_calls) >= 2
    assert closed_flag["closed"] is True
    assert heartbeats and heartbeats[0][0] == "grading"
    assert heartbeats[0][1].get("workers") == 3


def test_demo_and_session_web_auth_redirects_without_db(monkeypatch) -> None:
    def make_request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/ui/problems",
                "headers": headers or [],
            }
        )

    monkeypatch.setenv("AUTH_MODE", "demo")
    get_settings.cache_clear()
    try:
        bad_req = make_request([(b"x-student-login", b"bad login!")])
        with pytest.raises(HTTPException) as api_err:
            get_current_student(bad_req)
        assert api_err.value.status_code == 401
        assert api_err.value.detail == "invalid_student_login"

        with pytest.raises(HTTPException) as web_err:
            get_current_web_student(bad_req)
        assert web_err.value.status_code == 401

        monkeypatch.setenv("AUTH_MODE", "session")
        monkeypatch.setenv("AUTH_SESSION_SECRET", "y" * 48)
        get_settings.cache_clear()

        anon_req = make_request()
        with pytest.raises(HTTPException) as redirect_err:
            get_current_web_student(anon_req)
        assert redirect_err.value.status_code == 303
        assert redirect_err.value.headers is not None
        assert redirect_err.value.headers["Location"] == "/login"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_dockerignore_excludes_production_secrets_and_dumps() -> None:
    root = Path(__file__).resolve().parent.parent
    lines = {
        line.strip()
        for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in lines
    assert ".env.*" in lines
    assert "*.dump" in lines


def test_login_rate_limiter_does_not_reset_on_success_and_limits_accounts() -> None:
    test_app = FastAPI()
    test_app.add_middleware(
        LoginRateLimitMiddleware, max_attempts=2, window_seconds=60
    )

    @test_app.post("/api/auth/login")
    def login_stub(ok: bool = False) -> dict[str, bool]:
        if not ok:
            raise HTTPException(status_code=401, detail="invalid_credentials")
        return {"ok": True}

    with TestClient(test_app) as rate_client:
        assert rate_client.post("/api/auth/login?ok=false").status_code == 401
        assert rate_client.post("/api/auth/login?ok=true").status_code == 200
        assert rate_client.post("/api/auth/login?ok=false").status_code == 401
        blocked = rate_client.post("/api/auth/login?ok=false")
    assert blocked.status_code == 429

    now = {"t": 100.0}
    acct_limiter = AccountLoginRateLimiter(monotonic=lambda: now["t"])
    acct_limiter.record_failure("sv01", window_seconds=300)
    acct_limiter.record_failure("sv01", window_seconds=300)
    assert acct_limiter.check("sv01", max_attempts=2, window_seconds=300) > 0
    assert acct_limiter.check("sv02", max_attempts=2, window_seconds=300) == 0


def test_production_config_rejects_placeholders_wildcards_and_insecure_urls() -> None:
    base = {
        "app_env": "production",
        "auth_mode": "session",
        "auth_session_secret": "s" * 48,
        "auth_cookie_secure": True,
        "hint_execution_mode": "worker",
        "hint_request_expire_seconds": 300,
        "database_url": "postgresql+psycopg://app:secret@db:5432/app",
        "judge0_base_url": "https://judge0.internal.example.edu",
        "judge0_language_id": 100,
        "llm_api_key": "real-secret-for-validation",
        "trusted_hosts": "practice.example.edu",
    }
    for overrides, expected_msg in [
        ({"judge0_auth_token": "REPLACE_WITH_TOKEN"}, "production_judge0_auth_token_is_placeholder"),
        ({"trusted_hosts": "*"}, "production_forbids_wildcard_trusted_hosts"),
        ({"database_url": "postgresql+psycopg://app:URL_ENCODED_PASSWORD@db:5432/app"}, "production_requires_nondefault_database_url"),
        ({"judge0_base_url": "http://judge0.internal.example.edu"}, "production_requires_https_judge0_url"),
        ({"llm_api_base_url": "http://api.openai.com/v1"}, "production_requires_https_llm_url"),
        (
            {"llm_mode": "colab_qwen", "colab_qwen_endpoint_url": "http://colab.ngrok.app/v1"},
            "production_requires_https_colab_qwen_endpoint",
        ),
    ]:
        with pytest.raises(ValueError, match=expected_msg):
            Settings(**{**base, **overrides})


def test_security_headers_cache_hsts_csp_and_production_docs(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "session")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "z" * 48)
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            login_resp = client.get("/login")
            assert login_resp.status_code == 200
            assert login_resp.headers["cache-control"] == "no-store, private"
            assert "max-age=31536000" in login_resp.headers["strict-transport-security"]
            csp = login_resp.headers["content-security-policy"]
            assert "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/" in csp
            assert "https://cdn.jsdelivr.net;" not in csp
            assert "https://cdn.jsdelivr.net " not in csp

            ui_redirect = client.get("/ui/history", follow_redirects=False)
            assert ui_redirect.status_code == 303
            assert ui_redirect.headers["cache-control"] == "no-store, private"

            sandbox_resp = client.get("/static/pyodide-sandbox.html")
            assert sandbox_resp.status_code == 200
            assert sandbox_resp.headers["x-frame-options"] == "SAMEORIGIN"
            assert "sandbox allow-scripts" in sandbox_resp.headers["content-security-policy"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    prod_settings = Settings(
        app_env="production",
        auth_mode="session",
        auth_session_secret="p" * 48,
        auth_cookie_secure=True,
        hint_execution_mode="worker",
        hint_request_expire_seconds=300,
        database_url="postgresql+psycopg://app:secret@db:5432/app",
        judge0_base_url="https://judge0.internal.example.edu",
        judge0_language_id=100,
        llm_api_key="real-secret-for-validation",
        trusted_hosts="practice.example.edu",
    )
    prod_app = create_app(prod_settings)
    assert prod_app.docs_url is None
    assert prod_app.redoc_url is None
    assert prod_app.openapi_url is None
    with TestClient(prod_app, base_url="https://practice.example.edu") as prod_client:
        assert prod_client.get("/docs").status_code == 404
        assert prod_client.get("/openapi.json").status_code == 404


def test_request_logging_sanitizes_crlf() -> None:
    sanitized = _sanitize_log_value("/ui/problems\r\nFAKE_LOG\tENTRY")
    assert "\r" not in sanitized
    assert "\n" not in sanitized
    assert "\\r\\n" in sanitized
    assert "\\t" in sanitized
