from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio.client import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.payment_gateways import GatewayFactory
from app.bot.services.job_locks import RedisJobLock
from app.bot.utils.daily_test_autopay import (
    is_moscow_charge_window,
    moscow_date,
    next_random_charge_at,
    utc_now_naive,
)
from app.bot.utils.navigation import NavSubscription
from app.db.models import DailyTestAutopayPayment, DailyTestAutopayProfile

logger = logging.getLogger(__name__)


def _get_yookassa_gateway(gateway_factory: GatewayFactory | None):
    if gateway_factory is None:
        return None
    try:
        return gateway_factory.get_gateway(NavSubscription.PAY_YOOKASSA)
    except ValueError:
        return None


async def process_due_daily_test_autopayments(
    session_factory: async_sessionmaker,
    gateway_factory: GatewayFactory | None,
    *,
    now_utc: datetime | None = None,
) -> None:
    gateway = _get_yookassa_gateway(gateway_factory)
    if gateway is None:
        return

    now_utc = now_utc or utc_now_naive()
    async with session_factory() as session:
        due_profiles = await DailyTestAutopayProfile.get_due(
            session,
            now_utc=now_utc,
        )

    if not due_profiles:
        return

    if not is_moscow_charge_window(now_utc):
        next_charge_at = next_random_charge_at(now_utc)
        for profile in due_profiles:
            async with session_factory() as session:
                await DailyTestAutopayProfile.record_attempt(
                    session,
                    user_tg_id=profile.user_tg_id,
                    next_charge_at=next_charge_at,
                )
        logger.warning(
            "Skipped %d overdue daily test autopayments outside the Moscow charge window.",
            len(due_profiles),
        )
        return

    charge_date = moscow_date(now_utc)
    for profile in due_profiles:
        next_charge_at = next_random_charge_at(now_utc)
        async with session_factory() as session:
            await DailyTestAutopayProfile.record_attempt(
                session,
                user_tg_id=profile.user_tg_id,
                next_charge_at=next_charge_at,
            )

        try:
            await gateway.create_daily_autopayment(
                profile=profile,
                scheduled_for=charge_date,
            )
        except Exception as exception:
            logger.exception(
                "Failed to create daily test autopayment for user %s: %s",
                profile.user_tg_id,
                exception,
            )
            await gateway.services.notification.notify_by_id(
                chat_id=profile.user_tg_id,
                text=(
                    "⚠️ Сегодня не удалось создать тестовый автоплатёж на 10 ₽ "
                    "из-за технической ошибки. Следующая попытка запланирована на завтра."
                ),
            )


async def reconcile_pending_daily_test_autopayments(
    session_factory: async_sessionmaker,
    gateway_factory: GatewayFactory | None,
    *,
    lookback_minutes: int = 60,
) -> None:
    gateway = _get_yookassa_gateway(gateway_factory)
    if gateway is None:
        return

    since = utc_now_naive() - timedelta(minutes=lookback_minutes)
    async with session_factory() as session:
        pending = await DailyTestAutopayPayment.get_recent_pending(
            session,
            since=since,
        )

    for payment in pending:
        try:
            await gateway.reconcile_pending_payment(payment.payment_id)
        except Exception as exception:
            logger.exception(
                "Failed to reconcile daily test autopayment %s: %s",
                payment.payment_id,
                exception,
            )


async def process_due_daily_test_autopayments_locked(
    session_factory: async_sessionmaker,
    gateway_factory: GatewayFactory | None,
    redis: Redis,
) -> None:
    async with RedisJobLock(redis, "job:daily_test_autopay", 55) as acquired:
        if not acquired:
            return
        await process_due_daily_test_autopayments(
            session_factory,
            gateway_factory,
        )


async def reconcile_pending_daily_test_autopayments_locked(
    session_factory: async_sessionmaker,
    gateway_factory: GatewayFactory | None,
    redis: Redis,
) -> None:
    async with RedisJobLock(redis, "job:daily_test_autopay_reconcile", 55) as acquired:
        if not acquired:
            return
        await reconcile_pending_daily_test_autopayments(
            session_factory,
            gateway_factory,
        )


def start_scheduler(
    session_factory: async_sessionmaker,
    gateway_factory: GatewayFactory | None,
    redis: Redis | None = None,
) -> None:
    scheduler = AsyncIOScheduler()
    process_job = (
        process_due_daily_test_autopayments_locked
        if redis
        else process_due_daily_test_autopayments
    )
    reconcile_job = (
        reconcile_pending_daily_test_autopayments_locked
        if redis
        else reconcile_pending_daily_test_autopayments
    )
    process_args = (
        [session_factory, gateway_factory, redis]
        if redis
        else [session_factory, gateway_factory]
    )
    reconcile_args = (
        [session_factory, gateway_factory, redis]
        if redis
        else [session_factory, gateway_factory]
    )
    scheduler.add_job(
        process_job,
        "interval",
        minutes=1,
        args=process_args,
        next_run_time=datetime.now(),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reconcile_job,
        "interval",
        minutes=1,
        args=reconcile_args,
        next_run_time=datetime.now(),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
