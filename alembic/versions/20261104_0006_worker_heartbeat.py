"""Add worker heartbeat monitoring.

Revision ID: 20261104_0006
Revises: 20261104_0005
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261104_0006"
down_revision: str | None = "20261104_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeat",
        sa.Column("worker_name", sa.String(length=100), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("worker_name", name="pk_worker_heartbeat"),
    )
    op.create_index(
        "ix_worker_heartbeat_last_seen_at",
        "worker_heartbeat",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_heartbeat_last_seen_at", table_name="worker_heartbeat")
    op.drop_table("worker_heartbeat")
