from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Self

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.bot.utils.constants import TransactionStatus

from . import Base

logger = logging.getLogger(__name__)

AUTOPAY_BINDING_KIND = "binding"
AUTOPAY_DAILY_KIND = "daily"


class DailyTestAutopayProfile(Base):
    __tablename__ = "daily_test_autopay_profiles"

    user_tg_id: Mapped[int] = mapped_column(
        ForeignKey("users.tg_id", ondelete="CASCADE"),
        primary_key=True,
    )
    payment_method_id: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean(),
        default=False,
        server_default="0",
        nullable=False,
    )
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer(),
        default=0,
        server_default="0",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @classmethod
    async def get(cls, session: AsyncSession, user_tg_id: int) -> Self | None:
        result = await session.execute(
            select(cls).where(cls.user_tg_id == user_tg_id)
        )
        return result.scalar_one_or_none()

    @classmethod
    async def save_binding(
        cls,
        session: AsyncSession,
        *,
        user_tg_id: int,
        payment_method_id: str,
        next_charge_at: datetime,
    ) -> Self:
        profile = await cls.get(session, user_tg_id)
        if profile is None:
            profile = cls(user_tg_id=user_tg_id)
            session.add(profile)

        profile.payment_method_id = payment_method_id
        profile.enabled = True
        profile.next_charge_at = next_charge_at
        profile.consecutive_failures = 0
        await session.commit()
        await session.refresh(profile)
        return profile

    @classmethod
    async def set_enabled(
        cls,
        session: AsyncSession,
        *,
        user_tg_id: int,
        enabled: bool,
        next_charge_at: datetime | None = None,
    ) -> Self | None:
        profile = await cls.get(session, user_tg_id)
        if profile is None or (enabled and not profile.payment_method_id):
            return None

        profile.enabled = enabled
        profile.next_charge_at = next_charge_at if enabled else None
        if enabled:
            profile.consecutive_failures = 0
        await session.commit()
        await session.refresh(profile)
        return profile

    @classmethod
    async def forget_payment_method(
        cls,
        session: AsyncSession,
        *,
        user_tg_id: int,
    ) -> bool:
        profile = await cls.get(session, user_tg_id)
        if profile is None:
            return False

        profile.payment_method_id = None
        profile.enabled = False
        profile.next_charge_at = None
        profile.consecutive_failures = 0
        await session.commit()
        return True

    @classmethod
    async def get_due(
        cls,
        session: AsyncSession,
        *,
        now_utc: datetime,
    ) -> list[Self]:
        result = await session.execute(
            select(cls).where(
                cls.enabled.is_(True),
                cls.payment_method_id.is_not(None),
                cls.next_charge_at.is_not(None),
                cls.next_charge_at <= now_utc,
            )
        )
        return list(result.scalars().all())

    @classmethod
    async def record_success(
        cls,
        session: AsyncSession,
        *,
        user_tg_id: int,
        succeeded_at: datetime,
    ) -> None:
        await session.execute(
            update(cls)
            .where(cls.user_tg_id == user_tg_id)
            .values(last_success_at=succeeded_at, consecutive_failures=0)
        )
        await session.commit()

    @classmethod
    async def record_attempt(
        cls,
        session: AsyncSession,
        *,
        user_tg_id: int,
        next_charge_at: datetime,
    ) -> None:
        await session.execute(
            update(cls)
            .where(cls.user_tg_id == user_tg_id)
            .values(next_charge_at=next_charge_at)
        )
        await session.commit()

    @classmethod
    async def record_failure(
        cls,
        session: AsyncSession,
        *,
        user_tg_id: int,
        disable_after: int,
    ) -> tuple[int, bool]:
        profile = await cls.get(session, user_tg_id)
        if profile is None:
            return 0, False

        profile.consecutive_failures += 1
        disabled = profile.consecutive_failures >= disable_after
        if disabled:
            profile.enabled = False
            profile.next_charge_at = None
        await session.commit()
        return profile.consecutive_failures, disabled


class DailyTestAutopayPayment(Base):
    __tablename__ = "daily_test_autopay_payments"
    __table_args__ = (
        UniqueConstraint(
            "user_tg_id",
            "kind",
            "scheduled_for",
            name="uq_daily_test_autopay_user_kind_date",
        ),
    )

    payment_id: Mapped[str] = mapped_column(String(length=64), primary_key=True)
    user_tg_id: Mapped[int] = mapped_column(
        ForeignKey("users.tg_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(length=16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(length=16),
        default=TransactionStatus.PENDING.value,
        server_default=TransactionStatus.PENDING.value,
        nullable=False,
    )
    scheduled_for: Mapped[date | None] = mapped_column(Date(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @classmethod
    async def get(cls, session: AsyncSession, payment_id: str) -> Self | None:
        result = await session.execute(
            select(cls).where(cls.payment_id == payment_id)
        )
        return result.scalar_one_or_none()

    @classmethod
    async def get_for_day(
        cls,
        session: AsyncSession,
        *,
        user_tg_id: int,
        scheduled_for: date,
    ) -> Self | None:
        result = await session.execute(
            select(cls).where(
                cls.user_tg_id == user_tg_id,
                cls.kind == AUTOPAY_DAILY_KIND,
                cls.scheduled_for == scheduled_for,
            )
        )
        return result.scalar_one_or_none()

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        payment_id: str,
        user_tg_id: int,
        kind: str,
        scheduled_for: date | None = None,
    ) -> Self:
        existing = await cls.get(session, payment_id)
        if existing is not None:
            return existing

        payment = cls(
            payment_id=payment_id,
            user_tg_id=user_tg_id,
            kind=kind,
            scheduled_for=scheduled_for,
        )
        session.add(payment)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await cls.get(session, payment_id)
            if existing is not None:
                return existing
            raise
        await session.refresh(payment)
        return payment

    @classmethod
    async def set_status(
        cls,
        session: AsyncSession,
        *,
        payment_id: str,
        status: TransactionStatus,
    ) -> None:
        await session.execute(
            update(cls)
            .where(cls.payment_id == payment_id)
            .values(status=status.value)
        )
        await session.commit()

    @classmethod
    async def get_recent_pending(
        cls,
        session: AsyncSession,
        *,
        since: datetime,
    ) -> list[Self]:
        result = await session.execute(
            select(cls).where(
                cls.status == TransactionStatus.PENDING.value,
                cls.created_at >= since,
            )
        )
        return list(result.scalars().all())
