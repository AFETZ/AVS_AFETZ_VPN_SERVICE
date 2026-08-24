import logging
from abc import ABC, abstractmethod

from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiohttp.web import Application
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.utils.constants import (
    DEFAULT_LANGUAGE,
    EVENT_PAYMENT_CANCELED_TAG,
    EVENT_PAYMENT_SUCCEEDED_TAG,
    Currency,
    TransactionStatus,
)
from app.bot.utils.formatting import format_device_count, format_subscription_period
from app.config import Config
from app.db.models import Transaction, User

logger = logging.getLogger(__name__)


class PaymentGateway(ABC):
    name: str
    currency: Currency
    callback: str

    def __init__(
        self,
        app: Application,
        config: Config,
        session: async_sessionmaker,
        storage: RedisStorage,
        bot: Bot,
        i18n: I18n,
        services: ServicesContainer,
    ) -> None:
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.i18n = i18n
        self.services = services

    @abstractmethod
    async def create_payment(
        self,
        data: SubscriptionData,
        return_url: str | None = None,
    ) -> str:
        pass

    @abstractmethod
    async def handle_payment_succeeded(self, payment_id: str) -> None:
        pass

    @abstractmethod
    async def handle_payment_canceled(self, payment_id: str) -> None:
        pass

    async def reconcile_pending_payment(self, payment_id: str) -> bool:
        return False

    async def _on_payment_succeeded(self, payment_id: str) -> None:
        from app.bot.services.job_locks import RedisJobLock
        from app.bot.services.runtime_metrics import runtime_metrics

        logger.info(f"Payment succeeded {payment_id}")
        redis = getattr(self.storage, "redis", None)
        if redis is None:
            await self._process_payment_succeeded(payment_id)
            return

        async with RedisJobLock(redis, f"payment:process:{payment_id}", 5 * 60) as acquired:
            if not acquired:
                runtime_metrics.increment("payments.success_duplicate_locked")
                logger.info(
                    "Payment %s is already being processed. Skipping duplicate success event.",
                    payment_id,
                )
                return
            await self._process_payment_succeeded(payment_id)

    async def _process_payment_succeeded(self, payment_id: str) -> None:
        from app.bot.services.runtime_metrics import runtime_metrics

        async with self.session() as session:
            transaction = await Transaction.get_by_id(session=session, payment_id=payment_id)
            if not transaction:
                logger.warning(
                    "Cannot process successful payment %s: transaction not found.",
                    payment_id,
                )
                return

            if transaction.status == TransactionStatus.COMPLETED:
                runtime_metrics.increment("payments.success_duplicate_completed")
                logger.info(
                    "Payment %s already processed (status=%s). Skipping duplicate webhook.",
                    payment_id,
                    transaction.status.value,
                )
                return

            if transaction.status == TransactionStatus.CANCELED:
                logger.warning(
                    "Payment %s was auto-canceled but webhook confirms success. "
                    "Proceeding with subscription fulfillment.",
                    payment_id,
                )
            elif transaction.status != TransactionStatus.PENDING:
                logger.warning(
                    "Payment %s has unexpected status=%s. Skipping processing.",
                    payment_id,
                    transaction.status.value,
                )
                return

            data = SubscriptionData.unpack(transaction.subscription)
            logger.debug(f"Subscription data unpacked: {data}")
            user = await User.get(session=session, tg_id=data.user_id)

        if not user:
            logger.error(
                "Cannot process successful payment %s: user %s not found.",
                payment_id,
                data.user_id,
            )
            return

        is_web_user = user.tg_id < 0

        # Prepare locale for notification
        locale = user.language_code if user else DEFAULT_LANGUAGE
        with self.i18n.use_locale(locale):
            resolved_plan = self.services.subscription.get_payment_plan(
                plan_code=data.plan_code,
                devices=data.devices,
            )

            success = False
            if data.is_upgrade:
                if not data.plan_code:
                    logger.error(
                        "Upgrade payment %s is missing target plan code. Skipping plan activation.",
                        payment_id,
                    )
                    return

                await self.services.subscription.update_current_plan(
                    user=user,
                    plan_code=data.plan_code,
                    refresh_period=False,
                )
                logger.info(
                    "Tariff upgrade payment succeeded for user %s. Activated plan %s.",
                    user.tg_id,
                    data.plan_code,
                )
                if not is_web_user:
                    await self.services.notification.notify_upgrade_success(
                        user_id=user.tg_id,
                        plan_title=resolved_plan.title if resolved_plan else data.plan_code,
                    )
                success = True
            elif data.is_extend:
                success = await self.services.vpn.extend_subscription(
                    user=user,
                    devices=data.devices,
                    duration=data.duration,
                )
                if success:
                    if resolved_plan:
                        await self.services.subscription.update_current_plan(
                            user=user,
                            plan_code=resolved_plan.code,
                            refresh_period=True,
                            period_duration_days=data.duration,
                        )
                    logger.info(f"Subscription extended for user {user.tg_id}")
                    if not is_web_user:
                        await self.services.notification.notify_extend_success(
                            user_id=user.tg_id,
                            data=data,
                        )
            elif data.is_change:
                success = await self.services.vpn.change_subscription(
                    user=user,
                    devices=data.devices,
                )
                if success:
                    if resolved_plan:
                        await self.services.subscription.update_current_plan(
                            user=user,
                            plan_code=resolved_plan.code,
                            refresh_period=False,
                        )
                    logger.info(f"Subscription plan changed for user {user.tg_id}")
                    if not is_web_user:
                        await self.services.notification.notify_change_success(
                            user_id=user.tg_id,
                            data=data,
                            plan_title=resolved_plan.title if resolved_plan else "",
                        )
            else:
                success = await self.services.vpn.create_subscription(
                    user=user,
                    devices=data.devices,
                    duration=data.duration,
                )
                if success:
                    if resolved_plan:
                        await self.services.subscription.update_current_plan(
                            user=user,
                            plan_code=resolved_plan.code,
                            refresh_period=True,
                            period_duration_days=data.duration,
                        )
                    logger.info(f"Subscription created for user {user.tg_id}")
                    if not is_web_user:
                        key = await self.services.vpn.get_key(user)
                        await self.services.notification.notify_purchase_success(
                            user_id=user.tg_id,
                            key=key,
                        )

            if success:
                async with self.session() as session:
                    await Transaction.update(
                        session=session,
                        payment_id=payment_id,
                        status=TransactionStatus.COMPLETED,
                    )
                runtime_metrics.increment("payments.success_processed")

                if self.config.shop.REFERRER_REWARD_ENABLED:
                    await self.services.referral.add_referrers_rewards_on_payment(
                        referred_tg_id=data.user_id,
                        payment_amount=data.price,
                        payment_id=payment_id,
                    )

                plan_label = resolved_plan.title if resolved_plan and resolved_plan.title else ""
                await self.services.notification.notify_developer(
                    text=EVENT_PAYMENT_SUCCEEDED_TAG
                    + "\n\n"
                    + _("payment:event:payment_succeeded").format(
                        payment_id=payment_id,
                        user_id=user.tg_id,
                        plan=plan_label,
                        devices=format_device_count(data.devices),
                        duration=format_subscription_period(data.duration),
                    ),
                )

                if not is_web_user:
                    from app.bot.routers.main_menu.handler import redirect_to_main_menu

                    await redirect_to_main_menu(
                        bot=self.bot,
                        user=user,
                        services=self.services,
                        config=self.config,
                        storage=self.storage,
                    )
            else:
                logger.error(
                    "Failed to process subscription for user %s after payment %s.",
                    user.tg_id,
                    payment_id,
                )

    async def _on_payment_canceled(self, payment_id: str) -> None:
        logger.info(f"Payment canceled {payment_id}")
        async with self.session() as session:
            transaction = await Transaction.get_by_id(session=session, payment_id=payment_id)
            if not transaction:
                logger.warning(
                    "Cannot process canceled payment %s: transaction not found.",
                    payment_id,
                )
                return

            if transaction.status == TransactionStatus.CANCELED:
                logger.info(
                    "Payment %s already marked canceled. Skipping duplicate webhook.",
                    payment_id,
                )
                return

            if transaction.status == TransactionStatus.COMPLETED:
                logger.warning(
                    "Payment %s is already completed; cancel event ignored.",
                    payment_id,
                )
                return

            data = SubscriptionData.unpack(transaction.subscription)

            await Transaction.update(
                session=session,
                payment_id=payment_id,
                status=TransactionStatus.CANCELED,
            )

        await self.services.notification.notify_developer(
            text=EVENT_PAYMENT_CANCELED_TAG
            + "\n\n"
            + _("payment:event:payment_canceled").format(
                payment_id=payment_id,
                user_id=data.user_id,
                devices=format_device_count(data.devices),
                duration=format_subscription_period(data.duration),
            ),
        )
