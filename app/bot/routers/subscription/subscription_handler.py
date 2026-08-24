import logging
import math

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.i18n import gettext as _
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.models import ServicesContainer, SubscriptionData
from app.bot.payment_gateways import GatewayFactory
from app.bot.utils.constants import Currency
from app.bot.utils.formatting import format_date, format_device_count, format_subscription_period
from app.bot.utils.time import get_current_timestamp
from app.bot.utils.navigation import NavSubscription
from app.config import Config
from app.db.models import User

from .keyboard import (
    additional_profile_keyboard,
    change_apply_confirm_keyboard,
    change_mode_keyboard,
    devices_keyboard,
    duration_keyboard,
    payment_method_keyboard,
    plan_change_keyboard,
    scheduled_confirm_keyboard,
    scheduled_plan_keyboard,
    subscription_keyboard,
    upgrade_offer_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name=__name__)


def _get_plan_title(status) -> str:
    if status.plan and status.plan.title:
        return status.plan.title
    if status.client_data:
        return format_device_count(status.client_data.max_devices_count)
    return "-"


def _get_remaining_period_days(status) -> int | None:
    if not status.expiry_timestamp:
        return None

    remaining_ms = max(status.expiry_timestamp - get_current_timestamp(), 0)
    if remaining_ms <= 0:
        return 0

    return max(1, math.ceil(remaining_ms / 86_400_000))


def _build_subscription_text(status) -> str:
    if status.is_active and status.client_data:
        lines = [
            "Моя подписка",
            f"Текущий тариф: {_get_plan_title(status)}",
            f"Активна до: {status.expiry_date}",
            "",
        ]

        pending_plan_code = getattr(status.user, "pending_plan_code", None)
        if pending_plan_code:
            pending_starts_at = getattr(status.user, "pending_plan_starts_at", None)
            pending_start = format_date(pending_starts_at) if pending_starts_at else status.expiry_date
            lines.extend(
                [
                    f"Следующий тариф: {pending_plan_code}",
                    f"Старт после текущей подписки: {pending_start}",
                    "Списание сейчас: нет",
                    "",
                ]
            )

        if status.has_additional_profile:
            lines.extend(
                [
                    "Основной профиль:",
                    "Подписка обхода БС:",
                    "",
                ]
            )

        lines.append("Доступные действия:")
        return "\n".join(lines)

    if status.client_data and status.client_data.has_subscription_expired:
        return _("subscription:message:expired")

    return _("subscription:message:not_active")


def _build_additional_profile_text(
    status,
    *,
    quote=None,
    currency_symbol: str = "",
) -> str:
    if status.has_additional_profile:
        return (
            "Подписка обхода БС\n\n"
            "Статус: подключен\n"
            f"Текущий тариф: {_get_plan_title(status)}\n"
            f"Активна до: {status.expiry_date}\n\n"
            "Основная подписка, рекомендуемый вариант обхода БС и запасной вариант доступны ниже."
        )

    if quote:
        remaining_days = _get_remaining_period_days(status)
        remaining_text = (
            format_subscription_period(remaining_days)
            if remaining_days is not None and remaining_days > 0
            else "-"
        )
        return (
            "Подписка обхода БС\n\n"
            "Подключается только вместе с активной основной подпиской и начинает работать сразу после оплаты.\n\n"
            f"Текущий тариф: {_get_plan_title(status)}\n"
            f"Активна до: {status.expiry_date}\n"
            f"Осталось в текущем периоде: {remaining_text}\n\n"
            f"Доплата за оставшийся период: {quote.price} {currency_symbol}\n"
            f"Следующее продление: {quote.target_plan.title or format_device_count(quote.target_plan.devices)} "
            f"за {quote.renewal_price} {currency_symbol} "
            f"на {format_subscription_period(quote.renewal_duration_days)}\n\n"
            "После оплаты дата окончания текущей подписки не изменится. При следующем продлении "
            "бот предложит тариф уже с подпиской обхода БС."
        )

    if status.is_active:
        return (
            "Подписка обхода БС\n\n"
            "Опция доступна на тарифах с обходом БС.\n"
            "Откройте раздел подписки, чтобы выбрать подходящий тариф или улучшить текущий."
        )

    return (
        "Подписка обхода БС\n\n"
        "Оформите тариф с обходом БС в разделе подписки, "
        "чтобы получить основную подписку, рекомендуемый вариант обхода БС и запасной вариант."
    )


async def show_subscription(
    callback: CallbackQuery,
    user: User,
    status,
    callback_data: SubscriptionData,
    services: ServicesContainer,
) -> None:
    show_upgrade = services.subscription.can_upgrade_plan(status)
    show_change = status.is_active
    additional_profile_url = (
        services.subscription.get_additional_profile_url(user)
        if status.has_additional_profile
        else None
    )
    filtered_additional_profile_url = (
        services.subscription.get_filtered_additional_profile_url(user)
        if status.has_additional_profile
        else None
    )
    await callback.message.edit_text(
        text=_build_subscription_text(status),
        reply_markup=subscription_keyboard(
            has_subscription=status.is_active,
            callback_data=callback_data,
            show_change=show_change,
            show_upgrade=show_upgrade,
            show_primary_profile=status.has_additional_profile,
            additional_profile_url=additional_profile_url,
            filtered_additional_profile_url=filtered_additional_profile_url,
        ),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.CHANGE_MODE))
async def callback_subscription_change_mode(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    config: Config,
    services: ServicesContainer,
) -> None:
    logger.info("User %s opened tariff change mode screen.", user.tg_id)
    status = await services.subscription.get_subscription_status(user)
    if not status.status_check_ok or not status.is_active:
        await services.notification.show_popup(
            callback=callback,
            text="Смена тарифа сейчас недоступна.",
        )
        return

    trial_hint = ""
    if getattr(user, "current_plan_code", None) == config.shop.TRIAL_PLAN_CODE:
        trial_hint = (
            "\nДля пробной подписки можно заранее выбрать следующий тариф "
            "без списания сейчас."
        )

    text = (
        "Изменение тарифа\n\n"
        f"Текущий тариф: {_get_plan_title(status)}\n"
        f"Активна до: {status.expiry_date}\n\n"
        "Сменить сейчас — бот посчитает доплату за оставшийся срок, дата окончания не изменится.\n"
        "Запланировать следующий — списания сейчас не будет, выбранный тариф начнёт действовать "
        "после окончания текущей подписки после оплаты следующего периода."
        f"{trial_hint}"
    )
    await callback.message.edit_text(
        text=text,
        reply_markup=change_mode_keyboard(callback_data),
    )


@router.callback_query(F.data == NavSubscription.MAIN)
async def callback_subscription(
    callback: CallbackQuery,
    user: User,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    logger.info(f"User {user.tg_id} opened subscription page.")
    await state.set_state(None)

    status = await services.subscription.get_subscription_status(user)
    callback_data = SubscriptionData(
        state=NavSubscription.PROCESS,
        user_id=user.tg_id,
        plan_code=status.plan.code if status.plan else "",
    )
    await show_subscription(
        callback=callback,
        user=user,
        status=status,
        callback_data=callback_data,
        services=services,
    )


@router.callback_query(F.data == NavSubscription.ADDITIONAL_PROFILE)
async def callback_additional_profile(
    callback: CallbackQuery,
    user: User,
    config: Config,
    services: ServicesContainer,
) -> None:
    logger.info("User %s opened additional profile page.", user.tg_id)
    status = await services.subscription.get_subscription_status(user)
    additional_profile_url = (
        services.subscription.get_additional_profile_url(user)
        if status.has_additional_profile
        else None
    )
    filtered_additional_profile_url = (
        services.subscription.get_filtered_additional_profile_url(user)
        if status.has_additional_profile
        else None
    )

    upgrade_callback_data = None
    quote = None
    currency = Currency.from_code(config.shop.CURRENCY)
    if services.subscription.can_upgrade_plan(status):
        quote = await services.subscription.get_upgrade_quote(
            user=user,
            currency=currency,
        )
        upgrade_callback_data = SubscriptionData(
            state=NavSubscription.UPGRADE_PAYMENT,
            user_id=user.tg_id,
            plan_code=status.plan.code if status.plan else "",
        ).pack()

    await callback.message.edit_text(
        text=_build_additional_profile_text(
            status,
            quote=quote,
            currency_symbol=currency.symbol,
        ),
        reply_markup=additional_profile_keyboard(
            additional_profile_url=additional_profile_url,
            filtered_additional_profile_url=filtered_additional_profile_url,
            upgrade_callback_data=upgrade_callback_data,
            show_primary_profile=status.has_additional_profile,
        ),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.EXTEND))
async def callback_subscription_extend(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    config: Config,
    services: ServicesContainer,
) -> None:
    logger.info(f"User {user.tg_id} started extend subscription.")
    status = await services.subscription.get_subscription_status(user)
    if not status.status_check_ok or not status.client_data or not status.is_active:
        await services.notification.show_popup(
            callback=callback,
            text=_("subscription:popup:error_fetching_data"),
        )
        return

    current_plan = status.plan or services.plan.get_plan(status.client_data.max_devices_count)
    if not current_plan:
        await services.notification.show_popup(
            callback=callback,
            text=_("subscription:popup:error_fetching_plan"),
        )
        return

    callback_data.devices = status.client_data.max_devices_count
    callback_data.plan_code = current_plan.code
    callback_data.state = NavSubscription.DURATION
    callback_data.is_extend = True
    callback_data.is_change = False
    callback_data.is_upgrade = False
    await callback.message.edit_text(
        text=_("subscription:message:duration"),
        reply_markup=duration_keyboard(
            plan_service=services.plan,
            callback_data=callback_data,
            currency=config.shop.CURRENCY,
        ),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.CHANGE))
async def callback_subscription_change(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    config: Config,
    services: ServicesContainer,
) -> None:
    logger.info(f"User {user.tg_id} started change subscription.")
    currency = Currency.from_code(config.shop.CURRENCY)
    quotes = await services.subscription.get_plan_change_quotes(
        user=user, currency=currency,
    )

    if not quotes:
        await services.notification.show_popup(
            callback=callback,
            text="Нет доступных тарифов для перехода.",
        )
        return

    status = await services.subscription.get_subscription_status(user)
    remaining_days = _get_remaining_period_days(status)
    remaining_text = (
        format_subscription_period(remaining_days)
        if remaining_days is not None and remaining_days > 0
        else "-"
    )

    callback_data.state = NavSubscription.CHANGE_CONFIRM
    callback_data.is_change = True
    callback_data.is_extend = False
    callback_data.is_upgrade = False

    text = (
        "Сменить тариф\n\n"
        f"Текущий тариф: {_get_plan_title(status)}\n"
        f"Активна до: {status.expiry_date}\n"
        f"Осталось: {remaining_text}\n\n"
        "Можно перейти на более дорогой тариф (с доплатой за оставшийся срок) "
        "или на более дешёвый (без доплаты и без возврата средств).\n"
        "Дата окончания подписки не изменится.\n\n"
        "Выберите новый тариф:"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=plan_change_keyboard(quotes, callback_data, currency.symbol),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.SCHEDULE))
async def callback_subscription_schedule(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    services: ServicesContainer,
) -> None:
    logger.info("User %s opened scheduled tariff selection.", user.tg_id)
    status = await services.subscription.get_subscription_status(user)
    if not status.status_check_ok or not status.is_active or not status.expiry_timestamp:
        await services.notification.show_popup(
            callback=callback,
            text="Запланировать следующий тариф сейчас недоступно.",
        )
        return

    plans = services.plan.get_all_plans(
        prefer_additional_profile=bool(status.plan and status.plan.includes_additional_profile)
    )
    if not plans:
        await services.notification.show_popup(
            callback=callback,
            text=_("subscription:popup:error_fetching_plan"),
        )
        return

    default_duration = services.subscription._get_default_duration_days()
    text = (
        "Следующий тариф\n\n"
        f"Текущая подписка активна до: {status.expiry_date}\n"
        "Сейчас списания не будет. Бот сохранит выбранный тариф и предложит оплатить "
        "его после окончания текущей подписки.\n\n"
        "Выберите тариф:"
    )
    await callback.message.edit_text(
        text=text,
        reply_markup=scheduled_plan_keyboard(
            plans,
            callback_data,
            default_duration_days=default_duration,
        ),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.SCHEDULE_CONFIRM))
async def callback_subscription_schedule_confirm(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    services: ServicesContainer,
) -> None:
    logger.info(
        "User %s selected scheduled tariff candidate: plan=%s devices=%s.",
        user.tg_id,
        callback_data.plan_code,
        callback_data.devices,
    )
    status = await services.subscription.get_subscription_status(user)
    target_plan = services.plan.get_plan_by_code(callback_data.plan_code)
    if not status.status_check_ok or not status.is_active or not target_plan:
        await services.notification.show_popup(
            callback=callback,
            text="Запланировать следующий тариф сейчас недоступно.",
        )
        return

    callback_data.devices = target_plan.devices
    callback_data.plan_code = target_plan.code
    callback_data.duration = callback_data.duration or services.subscription._get_default_duration_days()
    callback_data.price = 0
    callback_data.is_change = False
    callback_data.is_extend = False
    callback_data.is_upgrade = False

    text = (
        "Подтвердите следующий тариф\n\n"
        f"Текущий тариф: {_get_plan_title(status)}\n"
        f"Активна до: {status.expiry_date}\n"
        f"Следующий тариф: {target_plan.title or format_device_count(target_plan.devices)}\n"
        f"Период после оплаты: {format_subscription_period(callback_data.duration)}\n\n"
        "Списание сейчас: нет.\n"
        "Дата окончания текущей подписки не изменится.\n"
        "Когда текущая подписка закончится, бот напомнит оплатить выбранный тариф."
    )
    await callback.message.edit_text(
        text=text,
        reply_markup=scheduled_confirm_keyboard(callback_data),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.SCHEDULE_APPLY))
async def callback_subscription_schedule_apply(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    services: ServicesContainer,
) -> None:
    logger.info(
        "User %s confirmed scheduled tariff: plan=%s devices=%s.",
        user.tg_id,
        callback_data.plan_code,
        callback_data.devices,
    )
    status = await services.subscription.get_subscription_status(user)
    target_plan = services.plan.get_plan_by_code(callback_data.plan_code)
    if not status.status_check_ok or not status.is_active or not target_plan:
        await services.notification.show_popup(
            callback=callback,
            text="Запланировать следующий тариф сейчас недоступно.",
        )
        return

    duration = callback_data.duration or services.subscription._get_default_duration_days()
    await services.subscription.schedule_next_plan(
        user=user,
        plan_code=target_plan.code,
        period_duration_days=duration,
        starts_at=status.expiry_timestamp,
    )

    refreshed_status = await services.subscription.get_subscription_status(user)
    refreshed_callback_data = SubscriptionData(
        state=NavSubscription.PROCESS,
        user_id=user.tg_id,
        plan_code=refreshed_status.plan.code if refreshed_status.plan else "",
    )
    await show_subscription(
        callback=callback,
        user=user,
        status=refreshed_status,
        callback_data=refreshed_callback_data,
        services=services,
    )
    await services.notification.show_popup(
        callback=callback,
        text="Следующий тариф запланирован.",
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.PROCESS))
async def callback_subscription_process(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    callback_data: SubscriptionData,
    services: ServicesContainer,
) -> None:
    logger.info(f"User {user.tg_id} started subscription process.")
    server = await services.server_pool.get_available_server()

    if not server:
        await services.notification.show_popup(
            callback=callback,
            text=_("subscription:popup:no_available_servers"),
            cache_time=120,
        )
        return

    callback_data.state = NavSubscription.DEVICES
    callback_data.plan_code = ""
    purchase_plans = services.plan.get_all_plans(
        prefer_additional_profile=not user.server_id and not user.current_plan_code,
    )
    await callback.message.edit_text(
        text=_("subscription:message:devices"),
        reply_markup=devices_keyboard(purchase_plans, callback_data),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.DEVICES))
async def callback_devices_selected(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    config: Config,
    services: ServicesContainer,
) -> None:
    logger.info(
        "User %s selected plan candidate: devices=%s code=%s",
        user.tg_id,
        callback_data.devices,
        callback_data.plan_code,
    )
    plan = services.subscription.get_payment_plan(
        plan_code=callback_data.plan_code,
        devices=callback_data.devices,
    )
    callback_data.plan_code = plan.code if plan else callback_data.plan_code
    if plan:
        callback_data.devices = plan.devices
    callback_data.state = NavSubscription.DURATION
    await callback.message.edit_text(
        text=_("subscription:message:duration"),
        reply_markup=duration_keyboard(
            plan_service=services.plan,
            callback_data=callback_data,
            currency=config.shop.CURRENCY,
        ),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.DURATION))
async def callback_duration_selected(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    logger.info(f"User {user.tg_id} selected duration: {callback_data.duration}")
    plan = services.subscription.get_payment_plan(
        plan_code=callback_data.plan_code,
        devices=callback_data.devices,
    )
    if not plan:
        await services.notification.show_popup(
            callback=callback,
            text=_("subscription:popup:error_fetching_plan"),
        )
        return

    callback_data.state = NavSubscription.PAY
    await callback.message.edit_text(
        text=_("subscription:message:payment_method"),
        reply_markup=payment_method_keyboard(
            plan=plan,
            callback_data=callback_data,
            gateways=gateway_factory.get_gateways(),
        ),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.CHANGE_CONFIRM))
async def callback_change_confirm(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    config: Config,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    logger.info(
        "User %s selected plan change to %s (%s devices).",
        user.tg_id,
        callback_data.plan_code,
        callback_data.devices,
    )
    target_plan = services.plan.get_plan_by_code(callback_data.plan_code)
    if not target_plan:
        await services.notification.show_popup(
            callback=callback,
            text=_("subscription:popup:error_fetching_plan"),
        )
        return

    shop_currency = Currency.from_code(config.shop.CURRENCY)
    display_quote = await services.subscription.get_upgrade_quote(
        user=user, currency=shop_currency, target_plan=target_plan,
    )
    if not display_quote:
        await services.notification.show_popup(
            callback=callback,
            text="Смена тарифа сейчас недоступна.",
        )
        return

    if display_quote.price == 0:
        status = await services.subscription.get_subscription_status(user)
        current_plan = status.plan

        warning_lines: list[str] = []
        if current_plan and target_plan.devices < current_plan.devices:
            warning_lines.append(
                f"Количество устройств уменьшится с {current_plan.devices} до {target_plan.devices}. "
                "Лишние устройства могут быть отключены."
            )
        if (
            current_plan
            and current_plan.includes_additional_profile
            and not target_plan.includes_additional_profile
        ):
            warning_lines.append(
                "Подписка обхода БС будет отключена."
            )

        warning_block = ""
        if warning_lines:
            warning_block = "Внимание:\n" + "\n".join(f"• {line}" for line in warning_lines) + "\n\n"

        text = (
            "Смена тарифа\n\n"
            f"Текущий: {_get_plan_title(status)}\n"
            f"Новый: {target_plan.title or format_device_count(target_plan.devices)}\n\n"
            "Доплата не потребуется.\n"
            f"Дата окончания не изменится: {status.expiry_date}\n\n"
            f"{warning_block}"
            "Подтвердить смену?"
        )

        callback_data.state = NavSubscription.CHANGE_APPLY
        callback_data.devices = target_plan.devices
        callback_data.duration = display_quote.renewal_duration_days
        callback_data.plan_code = target_plan.code
        callback_data.price = 0.0
        callback_data.is_change = True
        callback_data.is_extend = False
        callback_data.is_upgrade = False

        await callback.message.edit_text(
            text=text,
            reply_markup=change_apply_confirm_keyboard(callback_data),
        )
        return

    prices_by_callback: dict[str, float | int] = {}
    for gateway in gateway_factory.get_gateways():
        gw_quote = await services.subscription.get_upgrade_quote(
            user=user, currency=gateway.currency, target_plan=target_plan,
        )
        if gw_quote:
            prices_by_callback[gateway.callback] = gw_quote.price

    callback_data.state = NavSubscription.CHANGE_CONFIRM
    callback_data.devices = target_plan.devices
    callback_data.duration = display_quote.renewal_duration_days
    callback_data.plan_code = target_plan.code
    callback_data.price = float(display_quote.price)
    callback_data.is_change = True
    callback_data.is_extend = False
    callback_data.is_upgrade = False

    status = await services.subscription.get_subscription_status(user)
    remaining_days = _get_remaining_period_days(status)
    remaining_text = (
        format_subscription_period(remaining_days)
        if remaining_days is not None and remaining_days > 0
        else "-"
    )

    text = (
        "Смена тарифа\n\n"
        f"Текущий: {_get_plan_title(status)}\n"
        f"Новый: {target_plan.title or format_device_count(target_plan.devices)}\n\n"
        f"Доплата за оставшийся срок: {display_quote.price} {shop_currency.symbol}\n"
        f"Дата окончания не изменится: {display_quote.expiry_date}"
    )

    await callback.message.edit_text(
        text=text,
        reply_markup=payment_method_keyboard(
            plan=target_plan,
            callback_data=callback_data,
            gateways=gateway_factory.get_gateways(),
            prices_by_callback=prices_by_callback,
        ),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.CHANGE_APPLY))
async def callback_change_apply(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    config: Config,
    services: ServicesContainer,
) -> None:
    logger.info(
        "User %s confirmed plan change to %s (%s devices).",
        user.tg_id,
        callback_data.plan_code,
        callback_data.devices,
    )
    target_plan = services.plan.get_plan_by_code(callback_data.plan_code)
    if not target_plan:
        await services.notification.show_popup(
            callback=callback,
            text=_("subscription:popup:error_fetching_plan"),
        )
        return

    shop_currency = Currency.from_code(config.shop.CURRENCY)
    display_quote = await services.subscription.get_upgrade_quote(
        user=user, currency=shop_currency, target_plan=target_plan,
    )
    if not display_quote or display_quote.price != 0:
        await services.notification.show_popup(
            callback=callback,
            text="Смена тарифа сейчас недоступна.",
        )
        return

    success = await services.vpn.change_subscription(
        user=user,
        devices=target_plan.devices,
    )
    if not success:
        await services.notification.show_popup(
            callback=callback,
            text="Не удалось изменить тариф.",
        )
        return

    await services.subscription.update_current_plan(
        user=user,
        plan_code=target_plan.code,
        refresh_period=False,
    )

    refreshed_status = await services.subscription.get_subscription_status(user)
    refreshed_callback_data = SubscriptionData(
        state=NavSubscription.PROCESS,
        user_id=user.tg_id,
        plan_code=refreshed_status.plan.code if refreshed_status.plan else "",
    )
    await show_subscription(
        callback=callback,
        user=user,
        status=refreshed_status,
        callback_data=refreshed_callback_data,
        services=services,
    )
    await services.notification.show_popup(
        callback=callback,
        text="Тариф изменён.",
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.UPGRADE))
async def callback_subscription_upgrade(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    config: Config,
    services: ServicesContainer,
) -> None:
    logger.info("User %s opened whitelist bypass purchase offer.", user.tg_id)
    currency = Currency.from_code(config.shop.CURRENCY)
    quote = await services.subscription.get_upgrade_quote(user=user, currency=currency)
    if not quote:
        await services.notification.show_popup(
            callback=callback,
            text="Подключение подписки обхода БС сейчас недоступно.",
        )
        return

    status = await services.subscription.get_subscription_status(user)

    callback_data.state = NavSubscription.UPGRADE
    callback_data.devices = quote.target_plan.devices
    callback_data.duration = quote.renewal_duration_days
    callback_data.plan_code = quote.target_plan.code
    callback_data.price = float(quote.price)
    callback_data.is_extend = False
    callback_data.is_change = False
    callback_data.is_upgrade = True

    await callback.message.edit_text(
        text=_build_additional_profile_text(
            status,
            quote=quote,
            currency_symbol=currency.symbol,
        ),
        reply_markup=upgrade_offer_keyboard(callback_data),
    )


@router.callback_query(SubscriptionData.filter(F.state == NavSubscription.UPGRADE_PAYMENT))
async def callback_subscription_upgrade_payment(
    callback: CallbackQuery,
    user: User,
    callback_data: SubscriptionData,
    config: Config,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    logger.info("User %s selected payment screen for whitelist bypass purchase.", user.tg_id)
    status = await services.subscription.get_subscription_status(user)
    current_plan = status.plan
    target_plan = services.plan.get_upgrade_plan(current_plan)
    if not status.status_check_ok or not status.is_active or not current_plan or not target_plan:
        await services.notification.show_popup(
            callback=callback,
            text="Подключение подписки обхода БС сейчас недоступно.",
        )
        return

    shop_currency = Currency.from_code(config.shop.CURRENCY)
    display_quote = await services.subscription.get_upgrade_quote(user=user, currency=shop_currency)
    if not display_quote:
        await services.notification.show_popup(
            callback=callback,
            text="Подключение подписки обхода БС сейчас недоступно.",
        )
        return

    prices_by_callback: dict[str, float | int] = {}
    for gateway in gateway_factory.get_gateways():
        gateway_quote = await services.subscription.get_upgrade_quote(
            user=user,
            currency=gateway.currency,
        )
        if gateway_quote:
            prices_by_callback[gateway.callback] = gateway_quote.price

    callback_data.state = NavSubscription.UPGRADE_PAYMENT
    callback_data.devices = target_plan.devices
    callback_data.duration = display_quote.renewal_duration_days
    callback_data.plan_code = target_plan.code
    callback_data.price = float(display_quote.price)
    callback_data.is_extend = False
    callback_data.is_change = False
    callback_data.is_upgrade = True

    await callback.message.edit_text(
        text=_build_additional_profile_text(
            status,
            quote=display_quote,
            currency_symbol=shop_currency.symbol,
        ),
        reply_markup=payment_method_keyboard(
            plan=target_plan,
            callback_data=callback_data,
            gateways=gateway_factory.get_gateways(),
            prices_by_callback=prices_by_callback,
        ),
    )
