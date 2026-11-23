"""Create or rotate a student's production password without exposing it in argv."""

import argparse
from getpass import getpass

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Student
from app.security import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("login_name")
    args = parser.parse_args()
    login_name = args.login_name.strip()
    if not login_name or len(login_name) > 100:
        raise SystemExit("login_name must contain 1-100 characters")
    password = getpass("Password (at least 12 characters): ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("password confirmation does not match")
    encoded = hash_password(password)
    with SessionLocal.begin() as db:
        student = db.scalar(
            select(Student).where(Student.login_name == login_name).with_for_update()
        )
        if student is None:
            student = Student(login_name=login_name)
            db.add(student)
        student.password_hash = encoded
        student.is_active = True
        student.session_version += 1
    print(f"Student '{login_name}' is active and has a production password.")


if __name__ == "__main__":
    main()
