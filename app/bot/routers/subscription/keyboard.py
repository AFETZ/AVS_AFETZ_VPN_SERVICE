from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.bot.services import PlanService

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.models import SubscriptionData
from app.bot.models.plan import Plan
from app.bot.payment_gateways import PaymentGateway
from app.bot.routers.misc.keyboard import (
    back_button,
    back_to_main_menu_button,
    close_notification_button,
)
from app.bot.utils.constants import Currency
from app.bot.utils.formatting import format_device_count, format_subscription_period
from app.bot.utils.navigation import NavDownload, NavMain, NavProfile, NavSubscription


def change_subscription_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=_("subscription:button:change"),
        callback_data=NavSubscription.CHANGE,
    )


def upgrade_subscription_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="Подключить подписку обхода БС",
        callback_data=NavSubscription.ADDITIONAL_PROFILE,
    )


def subscription_keyboard(
    has_subscription: bool,
    callback_data: SubscriptionData,
    *,
    show_change: bool = False,
    show_upgrade: bool = False,
    show_primary_profile: bool = False,
    additional_profile_url: str | None = None,
    filtered_additional_profile_url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if not has_subscription:
        builder.row(
            InlineKeyboardButton(
                text=_("subscription:button:buy"),
                callback_data=callback_data.pack(),
            )
        )
    else:
        if show_primary_profile:
            builder.row(
                InlineKeyboardButton(
                    text="Подключить основную подписку",
                    callback_data=NavProfile.SHOW_KEY,
                )
            )

        if filtered_additional_profile_url:
            builder.row(
                InlineKeyboardButton(
                    text="Подписка обхода БС — рекомендуется",
                    url=filtered_additional_profile_url,
                )
            )

        if additional_profile_url:
            builder.row(
                InlineKeyboardButton(
                    text="Подписка обхода БС — запасной вариант",
                    url=additional_profile_url,
                )
            )

        callback_data.state = NavSubscription.EXTEND
        builder.row(
            InlineKeyboardButton(
                text=_("subscription:button:extend"),
                callback_data=callback_data.pack(),
            )
        )

        if show_change:
            callback_data.state = NavSubscription.CHANGE_MODE
            builder.row(
                InlineKeyboardButton(
                    text=_("subscription:button:change"),
                    callback_data=callback_data.pack(),
                )
            )

        if show_upgrade:
            builder.row(upgrade_subscription_button())

    builder.row(
        InlineKeyboardButton(
            text=_("subscription:button:activate_promocode"),
            callback_data=NavSubscription.PROMOCODE,
        )
    )
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def change_mode_keyboard(callback_data: SubscriptionData) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    callback_data.state = NavSubscription.CHANGE
    builder.row(
        InlineKeyboardButton(
            text="Сменить тариф сейчас",
            callback_data=callback_data.pack(),
        )
    )
    callback_data.state = NavSubscription.SCHEDULE
    builder.row(
        InlineKeyboardButton(
            text="Запланировать следующий тариф",
            callback_data=callback_data.pack(),
        )
    )
    builder.row(back_button(NavSubscription.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def plan_change_keyboard(
    quotes: list,
    callback_data: SubscriptionData,
    currency_symbol: str,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for quote in quotes:
        callback_data.devices = quote.target_plan.devices
        callback_data.plan_code = quote.target_plan.code
        callback_data.price = float(quote.price)
        callback_data.duration = quote.renewal_duration_days
        label = quote.target_plan.title or format_device_count(quote.target_plan.devices)
        if quote.target_plan.is_popular:
            label = f"🔥 {label}"
        price_label = f"+{quote.price} {currency_symbol}" if quote.price > 0 else "без доплаты"
        builder.button(
            text=f"{label} | {price_label}",
            callback_data=callback_data.pack(),
        )

    builder.adjust(1)
    builder.row(back_button(NavSubscription.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def scheduled_plan_keyboard(
    plans: list[Plan],
    callback_data: SubscriptionData,
    *,
    default_duration_days: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    callback_data.state = NavSubscription.SCHEDULE_CONFIRM
    callback_data.is_change = False
    callback_data.is_extend = False
    callback_data.is_upgrade = False
    callback_data.duration = default_duration_days
    callback_data.price = 0

    for plan in plans:
        callback_data.devices = plan.devices
        callback_data.plan_code = plan.code
        label = plan.title or format_device_count(plan.devices)
        if plan.is_popular:
            label = f"🔥 {label}"
        builder.button(
            text=f"{label} | после текущей",
            callback_data=callback_data.pack(),
        )

    builder.adjust(1)
    callback_data.state = NavSubscription.CHANGE_MODE
    builder.row(back_button(callback_data.pack()))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def scheduled_confirm_keyboard(callback_data: SubscriptionData) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    callback_data.state = NavSubscription.SCHEDULE_APPLY
    builder.row(
        InlineKeyboardButton(
            text="Купить после окончания текущей",
            callback_data=callback_data.pack(),
        )
    )
    callback_data.state = NavSubscription.SCHEDULE
    builder.row(back_button(callback_data.pack(), text="Назад к выбору тарифа"))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def devices_keyboard(
    plans: list[Plan],
    callback_data: SubscriptionData,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for plan in plans:
        callback_data.devices = plan.devices
        callback_data.plan_code = plan.code
        label = plan.title or format_device_count(plan.devices)
        if plan.is_popular:
            label = f"🔥 {label}"
        builder.button(
            text=label,
            callback_data=callback_data.pack(),
        )

    builder.adjust(2)
    builder.row(back_button(NavSubscription.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def duration_keyboard(
    plan_service: PlanService,
    callback_data: SubscriptionData,
    currency: str,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    currency: Currency = Currency.from_code(currency)
    plan = plan_service.get_plan_by_code(callback_data.plan_code) or plan_service.get_plan(
        callback_data.devices
    )

    if not plan:
        builder.row(back_button(NavSubscription.MAIN))
        builder.row(back_to_main_menu_button())
        return builder.as_markup()

    durations = plan.get_available_durations(plan_service.get_durations())

    for duration in durations:
        callback_data.duration = duration
        period = format_subscription_period(duration)
        price = plan.get_price(currency=currency, duration=duration)
        discount_percent = plan.get_discount_percent(currency=currency, duration=duration)
        discount_suffix = f" | -{discount_percent}%" if discount_percent > 0 else ""
        builder.button(
            text=f"{period} | {price} {currency.symbol}{discount_suffix}",
            callback_data=callback_data.pack(),
        )

    builder.adjust(2)

    if callback_data.is_extend:
        builder.row(back_button(NavSubscription.MAIN))
    else:
        callback_data.state = NavSubscription.PROCESS
        builder.row(
            back_button(
                callback_data.pack(),
                text=_("subscription:button:change_devices"),
            )
        )

    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def change_apply_confirm_keyboard(callback_data: SubscriptionData) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    callback_data.state = NavSubscription.CHANGE_APPLY
    builder.row(
        InlineKeyboardButton(
            text="Подтвердить смену тарифа",
            callback_data=callback_data.pack(),
        )
    )
    callback_data.state = NavSubscription.CHANGE
    builder.row(
        back_button(
            callback_data.pack(),
            text="Назад к выбору тарифа",
        )
    )
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def pay_keyboard(pay_url: str, callback_data: SubscriptionData) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(InlineKeyboardButton(text=_("subscription:button:pay"), url=pay_url))

    if callback_data.is_upgrade:
        callback_data.state = NavSubscription.UPGRADE_PAYMENT
    elif callback_data.is_change:
        callback_data.state = NavSubscription.CHANGE_CONFIRM
    else:
        callback_data.state = NavSubscription.DURATION
    builder.row(
        back_button(
            callback_data.pack(),
            text=_("subscription:button:change_payment_method"),
        )
    )
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def payment_method_keyboard(
    plan: Plan,
    callback_data: SubscriptionData,
    gateways: list[PaymentGateway],
    prices_by_callback: dict[str, float | int] | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for gateway in gateways:
        if prices_by_callback is not None:
            price = prices_by_callback.get(gateway.callback)
        else:
            price = plan.get_price(currency=gateway.currency, duration=callback_data.duration)
        if price is None:
            continue

        callback_data.state = gateway.callback
        builder.row(
            InlineKeyboardButton(
                text=f"{gateway.name} | {price} {gateway.currency.symbol}",
                callback_data=callback_data.pack(),
            )
        )

    if callback_data.is_upgrade:
        builder.row(back_button(NavSubscription.ADDITIONAL_PROFILE))
    elif callback_data.is_change:
        callback_data.state = NavSubscription.CHANGE
        builder.row(
            back_button(
                callback_data.pack(),
                text=_("subscription:button:change_devices"),
            )
        )
    else:
        callback_data.state = NavSubscription.DEVICES
        builder.row(
            back_button(
                callback_data.pack(),
                text=_("subscription:button:change_duration"),
            )
        )

    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def upgrade_offer_keyboard(callback_data: SubscriptionData) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    callback_data.state = NavSubscription.UPGRADE_PAYMENT
    builder.row(
        InlineKeyboardButton(
            text=_("subscription:button:pay"),
            callback_data=callback_data.pack(),
        )
    )
    builder.row(back_button(NavSubscription.ADDITIONAL_PROFILE))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def additional_profile_keyboard(
    *,
    additional_profile_url: str | None = None,
    filtered_additional_profile_url: str | None = None,
    upgrade_callback_data: str | None = None,
    show_primary_profile: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if show_primary_profile:
        builder.row(
            InlineKeyboardButton(
                text="Подключить основную подписку",
                callback_data=NavProfile.SHOW_KEY,
            )
        )

    if filtered_additional_profile_url:
        builder.row(
            InlineKeyboardButton(
                text="Подписка обхода БС — рекомендуется",
                url=filtered_additional_profile_url,
            )
        )

    if additional_profile_url:
        builder.row(
            InlineKeyboardButton(
                text="Подписка обхода БС — запасной вариант",
                url=additional_profile_url,
            )
        )

    if upgrade_callback_data:
        builder.row(
            InlineKeyboardButton(
                text="Подключить подписку обхода БС",
                callback_data=upgrade_callback_data,
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="Открыть подписку",
            callback_data=NavSubscription.MAIN,
        )
    )
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def payment_success_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("subscription:button:download_app"),
            callback_data=NavMain.REDIRECT_TO_DOWNLOAD,
        )
    )

    builder.row(close_notification_button())
    return builder.as_markup()


def trial_success_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("subscription:button:connect"),
            callback_data=NavDownload.MAIN,
        )
    )

    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def promocode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(back_button(NavSubscription.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()
