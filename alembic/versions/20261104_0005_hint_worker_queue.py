"""Add durable lease fields for the asynchronous hint worker.

Revision ID: 20261104_0005
Revises: 20261104_0004
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261104_0005"
down_revision: str | None = "20261104_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hint_request", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "hint_request", sa.Column("claim_token", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "hint_request",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_hint_request_nonnegative_attempt_count",
        "hint_request",
        "attempt_count >= 0",
    )
    op.create_index(
        "ix_hint_request_worker_queue",
        "hint_request",
        ["status", "claimed_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_hint_request_worker_queue", table_name="hint_request")
    op.drop_constraint(
        "ck_hint_request_nonnegative_attempt_count", "hint_request", type_="check"
    )
    op.drop_column("hint_request", "attempt_count")
    op.drop_column("hint_request", "claim_token")
    op.drop_column("hint_request", "claimed_at")
