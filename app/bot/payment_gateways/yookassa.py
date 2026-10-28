import asyncio
import logging
from datetime import date
from uuid import NAMESPACE_URL, uuid4, uuid5

from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiohttp.web import Application, Request, Response
from sqlalchemy.ext.asyncio import async_sessionmaker
from yookassa import Configuration, Payment
from yookassa.domain.common import SecurityHelper
from yookassa.domain.common.confirmation_type import ConfirmationType
from yookassa.domain.exceptions import ForbiddenError
from yookassa.domain.models.receipt import Receipt, ReceiptItem
from yookassa.domain.notification import (
    WebhookNotificationEventType,
    WebhookNotificationFactory,
)
from yookassa.domain.request.payment_request import PaymentRequest

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.payment_gateways import PaymentGateway
from app.bot.utils.constants import YOOKASSA_WEBHOOK, Currency, TransactionStatus
from app.bot.utils.daily_test_autopay import (
    DAILY_TEST_AUTOPAY_AMOUNT,
    DAILY_TEST_AUTOPAY_MAX_FAILURES,
    format_moscow_datetime,
    next_random_charge_at,
    utc_now_naive,
)
from app.bot.utils.formatting import format_device_count, format_subscription_period
from app.bot.utils.navigation import NavSubscription
from app.config import Config
from app.db.models import (
    DailyTestAutopayPayment,
    DailyTestAutopayProfile,
    Transaction,
)
from app.db.models.daily_test_autopay import (
    AUTOPAY_BINDING_KIND,
    AUTOPAY_DAILY_KIND,
)

logger = logging.getLogger(__name__)


class AutopayUnavailableError(RuntimeError):
    """Raised when recurring payments are not enabled for the YooKassa shop."""


class Yookassa(PaymentGateway):
    name = ""
    currency = Currency.RUB
    callback = NavSubscription.PAY_YOOKASSA

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
        self.name = __("payment:gateway:yookassa")
        self.app = app
        self.config = config
        self.session = session
        self.storage = storage
        self.bot = bot
        self.i18n = i18n
        self.services = services

        Configuration.configure(self.config.yookassa.SHOP_ID, self.config.yookassa.TOKEN)
        self.app.router.add_get(YOOKASSA_WEBHOOK, self.webhook_probe_handler)
        self.app.router.add_post(YOOKASSA_WEBHOOK, self.webhook_handler)
        logger.info("YooKassa payment gateway initialized.")

    async def create_payment(
        self,
        data: SubscriptionData,
        return_url: str | None = None,
    ) -> str:
        if return_url:
            redirect_url = return_url
        else:
            bot_username = (await self.bot.get_me()).username
            redirect_url = f"https://t.me/{bot_username}"

        devices = format_device_count(data.devices)
        if data.is_change:
            description = (
                f"Доплата за смену тарифа | {devices}. "
                "Дата окончания подписки не изменится."
            )
        else:
            description = _("payment:invoice:description").format(
                devices=devices,
                duration=format_subscription_period(data.duration),
            )

        price = str(data.price)

        receipt = Receipt(
            customer={"email": self.config.shop.EMAIL},
            items=[
                ReceiptItem(
                    description=description,
                    quantity=1,
                    amount={"value": price, "currency": self.currency.code},
                    vat_code=1,
                )
            ],
        )

        request = PaymentRequest(
            amount={"value": price, "currency": self.currency.code},
            confirmation={"type": ConfirmationType.REDIRECT, "return_url": redirect_url},
            capture=True,
            save_payment_method=False,
            description=description,
            receipt=receipt,
        )

        response = Payment.create(request)

        async with self.session() as session:
            await Transaction.create(
                session=session,
                tg_id=data.user_id,
                subscription=data.pack(),
                payment_id=response.id,
                status=TransactionStatus.PENDING,
            )

        pay_url = response.confirmation["confirmation_url"]
        logger.info(f"Payment link created for user {data.user_id}: {pay_url}")
        return pay_url

    async def _default_return_url(self) -> str:
        bot_username = (await self.bot.get_me()).username
        return f"https://t.me/{bot_username}"

    def _autopay_receipt(self, description: str) -> Receipt:
        return Receipt(
            customer={"email": self.config.shop.EMAIL},
            items=[
                ReceiptItem(
                    description=description,
                    quantity=1,
                    amount={
                        "value": str(DAILY_TEST_AUTOPAY_AMOUNT),
                        "currency": self.currency.code,
                    },
                    vat_code=1,
                )
            ],
        )

    async def create_autopay_binding(
        self,
        *,
        user_tg_id: int,
        return_url: str | None = None,
    ) -> str:
        description = "Подключение ежедневной тестовой покупки 10 рублей"
        request = PaymentRequest(
            amount={
                "value": str(DAILY_TEST_AUTOPAY_AMOUNT),
                "currency": self.currency.code,
            },
            confirmation={
                "type": ConfirmationType.REDIRECT,
                "return_url": return_url or await self._default_return_url(),
            },
            capture=True,
            save_payment_method=True,
            description=description,
            receipt=self._autopay_receipt(description),
            metadata={
                "purpose": "daily_test_autopay",
                "kind": AUTOPAY_BINDING_KIND,
                "user_tg_id": str(user_tg_id),
            },
        )
        try:
            response = await asyncio.to_thread(Payment.create, request, str(uuid4()))
        except ForbiddenError as exception:
            description = str(exception).lower()
            if "recurring" in description or "автоплат" in description:
                raise AutopayUnavailableError(
                    "Recurring payments are not enabled for this YooKassa shop."
                ) from exception
            raise

        async with self.session() as session:
            await DailyTestAutopayPayment.create(
                session,
                payment_id=response.id,
                user_tg_id=user_tg_id,
                kind=AUTOPAY_BINDING_KIND,
            )

        logger.info(
            "Daily test autopay binding payment created for user %s: payment_id=%s",
            user_tg_id,
            response.id,
        )
        return response.confirmation["confirmation_url"]

    async def create_daily_autopayment(
        self,
        *,
        profile: DailyTestAutopayProfile,
        scheduled_for: date,
    ) -> str | None:
        if not profile.enabled or not profile.payment_method_id:
            return None

        async with self.session() as session:
            existing = await DailyTestAutopayPayment.get_for_day(
                session,
                user_tg_id=profile.user_tg_id,
                scheduled_for=scheduled_for,
            )
            if existing is not None:
                return existing.payment_id

        description = "Ежедневная тестовая покупка 10 рублей"
        request = PaymentRequest(
            amount={
                "value": str(DAILY_TEST_AUTOPAY_AMOUNT),
                "currency": self.currency.code,
            },
            payment_method_id=profile.payment_method_id,
            capture=True,
            description=description,
            receipt=self._autopay_receipt(description),
            metadata={
                "purpose": "daily_test_autopay",
                "kind": AUTOPAY_DAILY_KIND,
                "user_tg_id": str(profile.user_tg_id),
                "scheduled_for": scheduled_for.isoformat(),
            },
        )
        idempotency_key = str(
            uuid5(
                NAMESPACE_URL,
                f"daily-test-autopay:{profile.user_tg_id}:{scheduled_for.isoformat()}",
            )
        )
        response = await asyncio.to_thread(Payment.create, request, idempotency_key)

        async with self.session() as session:
            await DailyTestAutopayPayment.create(
                session,
                payment_id=response.id,
                user_tg_id=profile.user_tg_id,
                kind=AUTOPAY_DAILY_KIND,
                scheduled_for=scheduled_for,
            )

        status = self._normalize_status(response.status)
        if status == "succeeded" and bool(getattr(response, "paid", False)):
            await self.handle_payment_succeeded(response.id)
        elif status == "canceled":
            await self.handle_payment_canceled(response.id)

        logger.info(
            "Daily test autopay created for user %s: payment_id=%s scheduled_for=%s",
            profile.user_tg_id,
            response.id,
            scheduled_for,
        )
        return response.id

    async def _get_autopay_payment(
        self,
        payment_id: str,
    ) -> DailyTestAutopayPayment | None:
        async with self.session() as session:
            return await DailyTestAutopayPayment.get(session, payment_id)

    async def _process_autopay_succeeded(
        self,
        autopay_payment: DailyTestAutopayPayment,
    ) -> None:
        if autopay_payment.status == TransactionStatus.COMPLETED.value:
            return

        now_utc = utc_now_naive()
        if autopay_payment.kind == AUTOPAY_BINDING_KIND:
            payment = await asyncio.to_thread(
                Payment.find_one,
                autopay_payment.payment_id,
            )
            payment_method = getattr(payment, "payment_method", None)
            payment_method_id = getattr(payment_method, "id", None)
            payment_method_saved = bool(getattr(payment_method, "saved", False))
            if not payment_method_saved or not payment_method_id:
                async with self.session() as session:
                    await DailyTestAutopayPayment.set_status(
                        session,
                        payment_id=autopay_payment.payment_id,
                        status=TransactionStatus.CANCELED,
                    )
                await self.services.notification.notify_by_id(
                    chat_id=autopay_payment.user_tg_id,
                    text=(
                        "⚠️ Платёж прошёл, но YooKassa не сохранила способ оплаты. "
                        "Автоплатёж не включён. Проверьте, что автоплатежи доступны "
                        "для магазина, и повторите привязку."
                    ),
                )
                return

            next_charge_at = next_random_charge_at(now_utc)
            async with self.session() as session:
                await DailyTestAutopayProfile.save_binding(
                    session,
                    user_tg_id=autopay_payment.user_tg_id,
                    payment_method_id=payment_method_id,
                    next_charge_at=next_charge_at,
                )
                await DailyTestAutopayPayment.set_status(
                    session,
                    payment_id=autopay_payment.payment_id,
                    status=TransactionStatus.COMPLETED,
                )

            await self.services.notification.notify_by_id(
                chat_id=autopay_payment.user_tg_id,
                text=(
                    "✅ Автоплатёж подключён.\n\n"
                    "Сумма: 10 ₽ ежедневно.\n"
                    "Окно: случайное время с 06:00 до 22:00 МСК.\n"
                    f"Следующее списание: {format_moscow_datetime(next_charge_at)}.\n\n"
                    "Приостановить или отвязать карту можно в админ-панели."
                ),
            )
            return

        async with self.session() as session:
            await DailyTestAutopayProfile.record_success(
                session,
                user_tg_id=autopay_payment.user_tg_id,
                succeeded_at=now_utc,
            )
            await DailyTestAutopayPayment.set_status(
                session,
                payment_id=autopay_payment.payment_id,
                status=TransactionStatus.COMPLETED,
            )

        await self.services.notification.notify_by_id(
            chat_id=autopay_payment.user_tg_id,
            text="✅ Ежедневная тестовая покупка на 10 ₽ успешно оплачена.",
        )

    async def _process_autopay_canceled(
        self,
        autopay_payment: DailyTestAutopayPayment,
    ) -> None:
        if autopay_payment.status in {
            TransactionStatus.COMPLETED.value,
            TransactionStatus.CANCELED.value,
        }:
            return

        async with self.session() as session:
            await DailyTestAutopayPayment.set_status(
                session,
                payment_id=autopay_payment.payment_id,
                status=TransactionStatus.CANCELED,
            )
            if autopay_payment.kind == AUTOPAY_DAILY_KIND:
                failures, disabled = await DailyTestAutopayProfile.record_failure(
                    session,
                    user_tg_id=autopay_payment.user_tg_id,
                    disable_after=DAILY_TEST_AUTOPAY_MAX_FAILURES,
                )
            else:
                failures, disabled = 0, False

        if autopay_payment.kind == AUTOPAY_BINDING_KIND:
            text = "❌ Привязка карты отменена. Автоплатёж не включён."
        elif disabled:
            text = (
                "❌ Тестовый автоплатёж не прошёл третий раз подряд и был "
                "приостановлен. Проверьте карту и включите его снова в админ-панели."
            )
        else:
            text = (
                "❌ Ежедневная тестовая покупка на 10 ₽ не прошла. "
                f"Неудачных попыток подряд: {failures}."
            )
        await self.services.notification.notify_by_id(
            chat_id=autopay_payment.user_tg_id,
            text=text,
        )

    async def handle_payment_succeeded(self, payment_id: str) -> None:
        autopay_payment = await self._get_autopay_payment(payment_id)
        if autopay_payment is not None:
            await self._process_autopay_succeeded(autopay_payment)
            return
        await self._on_payment_succeeded(payment_id)

    async def handle_payment_canceled(self, payment_id: str) -> None:
        autopay_payment = await self._get_autopay_payment(payment_id)
        if autopay_payment is not None:
            await self._process_autopay_canceled(autopay_payment)
            return
        await self._on_payment_canceled(payment_id)

    async def ensure_autopay_payment_from_metadata(self, payment: object) -> None:
        metadata = dict(getattr(payment, "metadata", {}) or {})
        if metadata.get("purpose") != "daily_test_autopay":
            return

        kind = metadata.get("kind")
        if kind not in {AUTOPAY_BINDING_KIND, AUTOPAY_DAILY_KIND}:
            return
        try:
            user_tg_id = int(metadata["user_tg_id"])
            scheduled_for = (
                date.fromisoformat(metadata["scheduled_for"])
                if metadata.get("scheduled_for")
                else None
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Invalid daily test autopay metadata for payment %s", getattr(payment, "id", ""))
            return

        payment_id = getattr(payment, "id", None)
        if not payment_id:
            return
        async with self.session() as session:
            await DailyTestAutopayPayment.create(
                session,
                payment_id=payment_id,
                user_tg_id=user_tg_id,
                kind=kind,
                scheduled_for=scheduled_for,
            )

    async def reconcile_pending_payment(self, payment_id: str) -> bool:
        try:
            payment = await asyncio.to_thread(Payment.find_one, payment_id)
        except Exception as exception:
            logger.exception(
                "YooKassa reconcile failed for payment %s: %s",
                payment_id,
                exception,
            )
            return False

        status = self._normalize_status(payment.status)
        paid = bool(getattr(payment, "paid", False))

        if status == "succeeded" and paid:
            logger.info(
                "YooKassa reconcile confirmed successful payment %s via API.",
                payment_id,
            )
            await self.handle_payment_succeeded(payment_id)
            return True

        if status == "canceled":
            logger.info(
                "YooKassa reconcile confirmed canceled payment %s via API.",
                payment_id,
            )
            await self.handle_payment_canceled(payment_id)
            return True

        return False

    @staticmethod
    def _extract_source_ip(request: Request) -> tuple[str, str, str]:
        cf_connecting_ip = request.headers.get("CF-Connecting-IP", "")
        x_forwarded_for = request.headers.get("X-Forwarded-For", "")
        remote_ip = request.remote or ""

        ip = cf_connecting_ip or (x_forwarded_for.split(",")[0].strip() if x_forwarded_for else remote_ip)
        return ip, x_forwarded_for, remote_ip

    @staticmethod
    def _normalize_status(value: object) -> str:
        if hasattr(value, "value"):
            return str(getattr(value, "value")).lower()
        return str(value).lower()

    async def _is_event_confirmed_by_api(
        self,
        payment_id: str,
        event: WebhookNotificationEventType,
    ) -> bool:
        try:
            payment = await asyncio.to_thread(Payment.find_one, payment_id)
        except Exception as exception:
            logger.exception(
                "YooKassa webhook: failed to verify payment %s via API: %s",
                payment_id,
                exception,
            )
            return False

        status = self._normalize_status(payment.status)
        paid = bool(getattr(payment, "paid", False))

        if event == WebhookNotificationEventType.PAYMENT_SUCCEEDED:
            is_valid = status == "succeeded" and paid
        elif event == WebhookNotificationEventType.PAYMENT_CANCELED:
            is_valid = status == "canceled"
        else:
            is_valid = False

        if not is_valid:
            logger.warning(
                "YooKassa webhook validation failed for payment %s: event=%s api_status=%s paid=%s",
                payment_id,
                event,
                status,
                paid,
            )

        return is_valid

    async def webhook_probe_handler(self, _: Request) -> Response:
        return Response(status=405, headers={"Allow": "POST"})

    async def webhook_handler(self, request: Request) -> Response:
        try:
            event_json = await request.json()
            notification_object = WebhookNotificationFactory().create(event_json)
            response_object = notification_object.object
            if response_object is None:
                return Response(status=400)
            payment_id = response_object.id
            if not payment_id:
                return Response(status=400)

            source_ip, x_forwarded_for, remote_ip = self._extract_source_ip(request)
            source_ip_trusted = bool(source_ip) and SecurityHelper().is_ip_trusted(source_ip)

            if not source_ip_trusted:
                logger.warning(
                    "YooKassa webhook source is not trusted by SDK list (ip='%s', X-Forwarded-For='%s', remote='%s'). Falling back to API verification.",
                    source_ip,
                    x_forwarded_for,
                    remote_ip,
                )

                if not await self._is_event_confirmed_by_api(
                    payment_id, notification_object.event
                ):
                    return Response(status=403)

            await self.ensure_autopay_payment_from_metadata(response_object)

            match notification_object.event:
                case WebhookNotificationEventType.PAYMENT_SUCCEEDED:
                    await self.handle_payment_succeeded(payment_id)
                    return Response(status=200)

                case WebhookNotificationEventType.PAYMENT_CANCELED:
                    await self.handle_payment_canceled(payment_id)
                    return Response(status=200)

                case _:
                    return Response(status=400)

        except Exception as exception:
            logger.exception(f"Error processing YooKassa webhook: {exception}")
            return Response(status=400)
