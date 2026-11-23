"""Add password authentication fields to student.

Revision ID: 20261104_0004
Revises: 20261102_0003
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261104_0004"
down_revision: str | None = "20261102_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("student", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column(
        "student",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("student", "is_active")
    op.drop_column("student", "password_hash")
