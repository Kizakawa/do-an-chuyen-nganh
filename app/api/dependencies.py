import hmac

from fastapi import HTTPException, Request, status
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Student
from app.security import InvalidSessionToken, decode_session_token


def get_current_student(request: Request) -> Student:
    """Resolve demo identity locally or a signed production session cookie.

    Mở và đóng phiên đọc ngắn hạn ngay lập tức để tránh giữ kết nối CSDL
    trong suốt thời gian chờ các tác vụ ngoại vi (như gọi LLM ở API gợi ý).
    """
    settings = get_settings()
    with SessionLocal() as db:
        if settings.auth_mode == "demo":
            student = db.scalar(
                select(Student).where(
                    Student.login_name == settings.demo_login_name,
                    Student.is_active.is_(True),
                )
            )
        else:
            token = request.cookies.get(settings.auth_cookie_name)
            if not token or not settings.auth_session_secret:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="authentication_required",
                )
            try:
                payload = decode_session_token(
                    token, secret=settings.auth_session_secret
                )
            except InvalidSessionToken as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="invalid_or_expired_session",
                ) from exc
            request.state.auth_session = payload
            student = db.scalar(
                select(Student).where(
                    Student.id == payload["sid"],
                    Student.is_active.is_(True),
                    Student.session_version == payload["ver"],
                )
            )
        if student is None:
            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if settings.auth_mode == "demo"
                    else status.HTTP_401_UNAUTHORIZED
                ),
                detail=(
                    "demo_student_not_seeded"
                    if settings.auth_mode == "demo"
                    else "invalid_or_expired_session"
                ),
            )
        db.expunge(student)
        return student


def get_current_web_student(request: Request) -> Student:
    try:
        return get_current_student(request)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
                detail="authentication_required",
            ) from exc
        raise


def require_csrf(request: Request) -> None:
    settings = get_settings()
    if settings.auth_mode == "demo":
        return
    payload = getattr(request.state, "auth_session", None)
    if payload is None:
        # Ensure authentication is evaluated even when dependency ordering changes.
        get_current_student(request)
        payload = getattr(request.state, "auth_session", None)
    header_token = request.headers.get("X-CSRF-Token", "")
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name, "")
    expected_token = payload.get("csrf", "") if payload else ""
    if not (
        header_token
        and cookie_token
        and expected_token
        and hmac.compare_digest(header_token, cookie_token)
        and hmac.compare_digest(header_token, expected_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="csrf_validation_failed",
        )
