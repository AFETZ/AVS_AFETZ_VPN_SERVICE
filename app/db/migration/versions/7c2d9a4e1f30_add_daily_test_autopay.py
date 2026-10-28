"""add daily test autopay

Revision ID: 7c2d9a4e1f30
Revises: b9f4e2a7c6d1
Create Date: 2026-08-25 18:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "7c2d9a4e1f30"
down_revision: Union[str, None] = "b9f4e2a7c6d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_test_autopay_profiles",
        sa.Column("user_tg_id", sa.Integer(), nullable=False),
        sa.Column("payment_method_id", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_charge_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_tg_id"], ["users.tg_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_tg_id"),
    )
    op.create_table(
        "daily_test_autopay_payments",
        sa.Column("payment_id", sa.String(length=64), nullable=False),
        sa.Column("user_tg_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("scheduled_for", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_tg_id"], ["users.tg_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("payment_id"),
        sa.UniqueConstraint(
            "user_tg_id",
            "kind",
            "scheduled_for",
            name="uq_daily_test_autopay_user_kind_date",
        ),
    )
    op.create_index(
        op.f("ix_daily_test_autopay_payments_user_tg_id"),
        "daily_test_autopay_payments",
        ["user_tg_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_daily_test_autopay_payments_user_tg_id"),
        table_name="daily_test_autopay_payments",
    )
    op.drop_table("daily_test_autopay_payments")
    op.drop_table("daily_test_autopay_profiles")
