from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select

from app.api.dependencies import get_current_student, require_csrf
from app.api.schemas import CurrentStudentResponse, LoginRequest
from app.config import get_settings
from app.database import SessionLocal
from app.models import Student
from app.security import create_session_token, hash_password, verify_password


router = APIRouter(prefix="/api/auth", tags=["authentication"])
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")


@router.post("/login", response_model=CurrentStudentResponse)
def login(payload: LoginRequest, response: Response) -> CurrentStudentResponse:
    settings = get_settings()
    if settings.auth_mode != "session" or not settings.auth_session_secret:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="session_auth_disabled"
        )
    with SessionLocal() as db:
        student = db.scalar(
            select(Student).where(Student.login_name == payload.login_name.strip())
        )
        encoded = (
            student.password_hash
            if student is not None and student.password_hash
            else _DUMMY_PASSWORD_HASH
        )
        password_ok = verify_password(payload.password, encoded)
        if student is None or not student.is_active or not password_ok:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_credentials",
            )
        token, csrf_token = create_session_token(
            student_id=student.id,
            secret=settings.auth_session_secret,
            ttl_seconds=settings.auth_session_ttl_seconds,
            session_version=student.session_version,
        )
        result = CurrentStudentResponse(id=student.id, login_name=student.login_name)

    cookie_options = {
        "secure": settings.auth_cookie_secure,
        "samesite": "lax",
        "max_age": settings.auth_session_ttl_seconds,
        "path": "/",
    }
    response.set_cookie(
        settings.auth_cookie_name, token, httponly=True, **cookie_options
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name, csrf_token, httponly=False, **cookie_options
    )
    return result


@router.get("/me", response_model=CurrentStudentResponse)
def me(student: Student = Depends(get_current_student)) -> CurrentStudentResponse:
    return CurrentStudentResponse(id=student.id, login_name=student.login_name)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    student: Student = Depends(get_current_student),
    _csrf: None = Depends(require_csrf),
) -> Response:
    settings = get_settings()
    with SessionLocal.begin() as db:
        persisted = db.get(Student, student.id, with_for_update=True)
        if persisted is not None:
            persisted.session_version += 1
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
