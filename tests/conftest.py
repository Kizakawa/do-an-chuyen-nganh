from functools import lru_cache

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, engine
from app.rate_limit import ACCOUNT_LOGIN_LIMITER


@pytest.fixture(autouse=True)
def reset_cached_security_state():
    get_settings.cache_clear()
    ACCOUNT_LOGIN_LIMITER.reset()
    yield
    get_settings.cache_clear()
    ACCOUNT_LOGIN_LIMITER.reset()


@lru_cache(maxsize=1)
def is_database_reachable() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


def require_database() -> None:
    if not is_database_reachable():
        pytest.skip("PostgreSQL chưa chạy trên localhost:5432 (bỏ qua DB integration test)")


@pytest.fixture
def database_reachable() -> bool:
    return is_database_reachable()


@pytest.fixture
def db_session() -> Session:
    require_database()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def clean_database() -> None:
    """Xóa dữ liệu trong database test dành riêng cho integration test."""
    require_database()
    with SessionLocal.begin() as session:
        session.execute(
            text(
                "TRUNCATE TABLE worker_heartbeat, hint_request, help_quota, test_result, grading_run, "
                "submission, test_case, problem, student RESTART IDENTITY CASCADE"
            )
        )
