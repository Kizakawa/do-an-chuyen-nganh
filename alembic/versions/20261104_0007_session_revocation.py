"""Add per-student session revocation version.

Revision ID: 20261104_0007
Revises: 20261104_0006
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261104_0007"
down_revision: str | None = "20261104_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "student",
        sa.Column("session_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_student_nonnegative_session_version", "student", "session_version >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_student_nonnegative_session_version", "student", type_="check"
    )
    op.drop_column("student", "session_version")
