import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram.utils.i18n import I18n
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio.client import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.services import NotificationService, VPNService
from app.bot.services.job_locks import RedisJobLock
from app.bot.services.runtime_metrics import open_fd_count, runtime_metrics
from app.bot.services.subscription_state import client_data_from_user_snapshot
from app.bot.services.vpn import InboundCache
from app.db.models import User

logger = logging.getLogger(__name__)

EXPIRY_NOTIFICATION_TTL = timedelta(days=2)
EXPIRY_CHECK_CONCURRENCY = 3
EXPIRY_CHECK_LOOKAHEAD_BUFFER = timedelta(hours=1)
SUBSCRIPTION_SYNC_CONCURRENCY = 3
SUBSCRIPTION_EXPIRY_LOCK_TTL_SECONDS = 14 * 60
SUBSCRIPTION_SYNC_LOCK_TTL_SECONDS = 9 * 60


@dataclass(frozen=True)
class ExpiryNotificationThreshold:
    name: str
    window: timedelta
    message_key: str


EXPIRY_NOTIFICATION_THRESHOLDS = (
    ExpiryNotificationThreshold(
        name="3h",
        window=timedelta(hours=3),
        message_key="task:message:subscription_expiry_urgent",
    ),
    ExpiryNotificationThreshold(
        name="24h",
        window=timedelta(hours=24),
        message_key="task:message:subscription_expiry",
    ),
)
EXPIRY_CHECK_LOOKAHEAD = (
    max(threshold.window for threshold in EXPIRY_NOTIFICATION_THRESHOLDS)
    + EXPIRY_CHECK_LOOKAHEAD_BUFFER
)


def _select_expiry_notification(
    time_left: timedelta,
) -> ExpiryNotificationThreshold | None:
    if time_left <= timedelta(0):
        return None

    for threshold in EXPIRY_NOTIFICATION_THRESHOLDS:
        if time_left <= threshold.window:
            return threshold

    return None


def _expiry_notification_key(
    tg_id: int,
    expiry_time: int,
    threshold: ExpiryNotificationThreshold,
) -> str:
    return (
        f"user:notified:subscription_expiry:{tg_id}:{expiry_time}:{threshold.name}"
    )


def _legacy_expiry_notification_key(tg_id: int) -> str:
    return f"user:notified:{tg_id}"


def _pending_tariff_notification_key(tg_id: int, plan_code: str) -> str:
    return f"user:notified:pending_tariff:{tg_id}:{plan_code}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _current_period_expiry(user: User) -> datetime | None:
    started_at = getattr(user, "current_period_started_at", None)
    duration_days = getattr(user, "current_period_duration_days", None)
    if not started_at or not duration_days:
        return None
    return _as_utc(started_at) + timedelta(days=duration_days)


def _should_check_user_for_expiry(user: User, now: datetime) -> bool:
    if not getattr(user, "server_id", None):
        return False

    if getattr(user, "is_blocked", False):
        return False

    period_expiry = _current_period_expiry(user)
    if period_expiry and period_expiry - now > EXPIRY_CHECK_LOOKAHEAD:
        return False

    return True


async def notify_users_with_expiring_subscription(
    session_factory: async_sessionmaker,
    redis: Redis,
    i18n: I18n,
    vpn_service: VPNService,
    notification_service: NotificationService,
) -> None:
    started_at = time.monotonic()
    session: AsyncSession
    async with session_factory() as session:
        users = await User.get_all(session=session)

    now = datetime.now(timezone.utc)
    candidate_users = [
        user for user in users if _should_check_user_for_expiry(user=user, now=now)
    ]
    fd_count = open_fd_count()
    logger.info(
        "[Background task] Starting subscription expiration check: users=%s candidates=%s fd_count=%s.",
        len(users),
        len(candidate_users),
        fd_count if fd_count is not None else "unknown",
    )

    semaphore = asyncio.Semaphore(EXPIRY_CHECK_CONCURRENCY)
    inbound_cache = InboundCache()

    async def process_user(user: User) -> bool:
        client_data = client_data_from_user_snapshot(user, require_fresh=True)
        if client_data is None:
            async with semaphore:
                try:
                    client_data = await vpn_service.get_client_data(
                        user,
                        inbound_cache=inbound_cache,
                    )
                except Exception as exception:
                    logger.warning(
                        "[Background task] Failed to check subscription for user %s: %s",
                        user.tg_id,
                        exception,
                    )
                    return False

        # Skip if no client data or subscription is unlimited
        if not client_data or client_data._expiry_time == -1:
            return False

        expiry_datetime = datetime.fromtimestamp(
            client_data._expiry_time / 1000, timezone.utc
        )
        time_left = expiry_datetime - datetime.now(timezone.utc)

        pending_plan_code = getattr(user, "pending_plan_code", None)
        if pending_plan_code and time_left <= timedelta(0):
            user_notified_key = _pending_tariff_notification_key(
                user.tg_id,
                pending_plan_code,
            )
            if await redis.get(user_notified_key):
                return False

            await notification_service.notify_by_id(
                chat_id=user.tg_id,
                text=(
                    "Текущая подписка закончилась.\n\n"
                    f"Запланированный тариф: {pending_plan_code}.\n"
                    "Откройте «Тарифы», чтобы оплатить следующий период."
                ),
            )
            await redis.set(user_notified_key, "true", ex=EXPIRY_NOTIFICATION_TTL)
            logger.info(
                "[Background task] Sent pending tariff reminder to user %s for plan %s.",
                user.tg_id,
                pending_plan_code,
            )
            return True

        threshold = _select_expiry_notification(time_left)
        if threshold is None:
            return False

        user_notified_key = _expiry_notification_key(
            user.tg_id,
            client_data._expiry_time,
            threshold,
        )

        # Check if user was already notified for this expiry timestamp.
        if await redis.get(user_notified_key):
            return False

        if (
            threshold.name == "24h"
            and await redis.get(_legacy_expiry_notification_key(user.tg_id))
        ):
            return False

        # BUG: The button and expiry_time will not be translated
        # (the translation logic needs to be changed outside the current context)
        notification_text = i18n.gettext(
            threshold.message_key,
            locale=user.language_code,
        ).format(
            devices=client_data.max_devices,
            expiry_time=client_data.expiry_time,
        )
        await notification_service.notify_by_id(
            chat_id=user.tg_id,
            text=notification_text,
            # reply_markup=keyboard_extend
        )

        await redis.set(user_notified_key, "true", ex=EXPIRY_NOTIFICATION_TTL)
        logger.info(
            "[Background task] Sent %s expiry notification to user %s.",
            threshold.name,
            user.tg_id,
        )
        return True

    sent_results = await asyncio.gather(
        *(process_user(user) for user in candidate_users),
        return_exceptions=False,
    )
    sent_count = sum(sent_results)
    final_fd_count = open_fd_count()
    runtime_metrics.record_event(
        "subscription_expiry.last_run",
        users=len(users),
        candidates=len(candidate_users),
        checked=len(candidate_users),
        sent=sent_count,
        fd_count=final_fd_count,
    )
    runtime_metrics.record_duration("subscription_expiry.duration_seconds", started_at)
    logger.info(
        "[Background task] Subscription check finished: checked=%s sent=%s fd_count=%s.",
        len(candidate_users),
        sent_count,
        final_fd_count or "unknown",
    )


async def sync_subscription_snapshots(
    session_factory: async_sessionmaker,
    vpn_service: VPNService,
) -> None:
    started_at = time.monotonic()
    session: AsyncSession
    async with session_factory() as session:
        users = await User.get_all(session=session)

    candidate_users = [
        user
        for user in users
        if getattr(user, "server_id", None) and not getattr(user, "is_blocked", False)
    ]
    semaphore = asyncio.Semaphore(SUBSCRIPTION_SYNC_CONCURRENCY)
    inbound_cache = InboundCache()

    async def sync_user(user: User) -> bool:
        async with semaphore:
            try:
                await vpn_service.get_client_data(user, inbound_cache=inbound_cache)
            except Exception as exception:
                logger.warning(
                    "[Background task] Failed to sync subscription snapshot for user %s: %s",
                    user.tg_id,
                    exception,
                )
                return False
        return True

    results = await asyncio.gather(
        *(sync_user(user) for user in candidate_users),
        return_exceptions=False,
    )
    synced_count = sum(results)
    runtime_metrics.record_event(
        "subscription_sync.last_run",
        users=len(users),
        candidates=len(candidate_users),
        synced=synced_count,
        failed=len(candidate_users) - synced_count,
        fd_count=open_fd_count(),
    )
    runtime_metrics.record_duration("subscription_sync.duration_seconds", started_at)
    logger.info(
        "[Background task] Subscription snapshot sync finished: candidates=%s synced=%s failed=%s.",
        len(candidate_users),
        synced_count,
        len(candidate_users) - synced_count,
    )


async def notify_users_with_expiring_subscription_locked(
    session_factory: async_sessionmaker,
    redis: Redis,
    i18n: I18n,
    vpn_service: VPNService,
    notification_service: NotificationService,
) -> None:
    async with RedisJobLock(
        redis,
        "job:subscription_expiry",
        SUBSCRIPTION_EXPIRY_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            logger.info("[Background task] Subscription expiration check already running.")
            return
        await notify_users_with_expiring_subscription(
            session_factory=session_factory,
            redis=redis,
            i18n=i18n,
            vpn_service=vpn_service,
            notification_service=notification_service,
        )


async def sync_subscription_snapshots_locked(
    session_factory: async_sessionmaker,
    redis: Redis,
    vpn_service: VPNService,
) -> None:
    async with RedisJobLock(
        redis,
        "job:subscription_sync",
        SUBSCRIPTION_SYNC_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            logger.info("[Background task] Subscription snapshot sync already running.")
            return
        await sync_subscription_snapshots(
            session_factory=session_factory,
            vpn_service=vpn_service,
        )


def start_scheduler(
    session_factory: async_sessionmaker,
    redis: Redis,
    i18n: I18n,
    vpn_service: VPNService,
    notification_service: NotificationService,
) -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        notify_users_with_expiring_subscription_locked,
        "interval",
        minutes=15,
        args=[session_factory, redis, i18n, vpn_service, notification_service],
        next_run_time=datetime.now(tz=timezone.utc),
    )
    scheduler.add_job(
        sync_subscription_snapshots_locked,
        "interval",
        minutes=10,
        args=[session_factory, redis, vpn_service],
        next_run_time=datetime.now(tz=timezone.utc) + timedelta(minutes=2),
    )
    scheduler.start()
