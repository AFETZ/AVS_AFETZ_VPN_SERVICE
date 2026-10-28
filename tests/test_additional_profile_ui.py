from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.models.plan import Plan
from app.bot.models.subscription_data import SubscriptionData
from app.bot.routers.main_menu.keyboard import main_menu_keyboard
from app.bot.routers.subscription.keyboard import additional_profile_keyboard, subscription_keyboard
from app.bot.routers.subscription.subscription_handler import (
    _build_subscription_text,
    _build_additional_profile_text,
    callback_additional_profile,
)
from app.bot.services.subscription import SubscriptionStatus, UpgradeQuote
from app.bot.utils.navigation import NavSubscription


def flatten_keyboard_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def fake_gettext(message, plural=None, n=1):
    if plural is None:
        return message
    return message if n == 1 else plural


@pytest.fixture(autouse=True)
def patch_i18n(monkeypatch):
    monkeypatch.setattr("app.bot.routers.main_menu.keyboard._", fake_gettext)
    monkeypatch.setattr("app.bot.routers.misc.keyboard._", fake_gettext)
    monkeypatch.setattr("app.bot.routers.subscription.keyboard._", fake_gettext)
    monkeypatch.setattr("app.bot.routers.subscription.subscription_handler._", fake_gettext)
    monkeypatch.setattr("app.bot.utils.formatting._", fake_gettext)


def test_main_menu_keyboard_keeps_primary_inline_entrypoints():
    markup = main_menu_keyboard()
    texts = flatten_keyboard_texts(markup)

    assert "main_menu:button:profile" in texts
    assert "main_menu:button:subscription" in texts
    assert "main_menu:button:support" in texts
    assert "main_menu:button:news_channel" in texts


def test_subscription_keyboard_keeps_renewal_inside_bot():
    callback_data = SubscriptionData(
        state=NavSubscription.PROCESS,
        user_id=1,
        plan_code="p3",
    )

    markup = subscription_keyboard(
        has_subscription=True,
        callback_data=callback_data,
    )
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert "subscription:button:extend" in [button.text for button in buttons]
    assert all(button.url is None for button in buttons)


def test_subscription_text_does_not_advertise_external_renewal():
    status = SubscriptionStatus(
        user=SimpleNamespace(tg_id=1),
        client_data=None,
        plan=None,
        is_active=False,
        status_check_ok=True,
        period_duration_days=None,
        expiry_timestamp=None,
    )

    text = _build_subscription_text(status)

    assert text == "subscription:message:not_active"


def test_additional_profile_keyboard_contains_relevant_actions():
    markup = additional_profile_keyboard(
        additional_profile_url="https://bot.example/wl/vpn-1",
        filtered_additional_profile_url="https://bot.example/wl-filtered/vpn-1",
        upgrade_callback_data=SubscriptionData(
            state=NavSubscription.UPGRADE,
            user_id=1,
            plan_code="p3",
        ).pack(),
        show_primary_profile=True,
    )

    texts = flatten_keyboard_texts(markup)

    assert "Подключить основную подписку" in texts
    assert "Подписка обхода БС — рекомендуется" in texts
    assert "Подписка обхода БС — запасной вариант" in texts
    assert "Подключить подписку обхода БС" in texts
    assert "Открыть подписку" in texts


def test_build_additional_profile_text_for_upgraded_user():
    plan = Plan(
        code="p3a",
        devices=3,
        title="3 устройства + доп. профиль",
        is_public=False,
        includes_additional_profile=True,
        prices={"RUB": {30: 498}},
    )
    status = SubscriptionStatus(
        user=SimpleNamespace(tg_id=1),
        client_data=None,
        plan=plan,
        is_active=True,
        status_check_ok=True,
        period_duration_days=30,
        expiry_timestamp=1,
    )

    text = _build_additional_profile_text(status)

    assert "Статус: подключен" in text
    assert "3 устройства + доп. профиль" in text


@pytest.mark.asyncio
async def test_callback_additional_profile_shows_upgrade_offer():
    current_plan = Plan(
        code="p3",
        devices=3,
        title="3 устройства",
        prices={"RUB": {30: 349}},
    )
    target_plan = Plan(
        code="p3a",
        devices=3,
        title="3 устройства + доп. профиль",
        is_public=False,
        includes_additional_profile=True,
        upgrade_from="p3",
        prices={"RUB": {30: 498}},
    )
    quote = UpgradeQuote(
        current_plan=current_plan,
        target_plan=target_plan,
        price=75,
        renewal_price=498,
        renewal_duration_days=30,
        expiry_timestamp=1,
    )
    status = SubscriptionStatus(
        user=SimpleNamespace(tg_id=1),
        client_data=None,
        plan=current_plan,
        is_active=True,
        status_check_ok=True,
        period_duration_days=30,
        expiry_timestamp=1,
    )
    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(message=message)
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-1")
    services = SimpleNamespace(
        subscription=SimpleNamespace(
            get_subscription_status=AsyncMock(return_value=status),
            get_additional_profile_url=lambda _: "https://bot.example/wl/vpn-1",
            can_upgrade_plan=lambda _: True,
            get_upgrade_quote=AsyncMock(return_value=quote),
        )
    )
    config = SimpleNamespace(shop=SimpleNamespace(CURRENCY="RUB"))

    await callback_additional_profile(
        callback=callback,
        user=user,
        config=config,
        services=services,
    )

    assert message.edit_text.await_count == 1
    kwargs = message.edit_text.await_args.kwargs
    assert "Подписка обхода БС" in kwargs["text"]
    assert "Доплата за оставшийся период: 75 ₽" in kwargs["text"]
    assert "Подключить подписку обхода БС" in flatten_keyboard_texts(
        kwargs["reply_markup"]
    )


@pytest.mark.asyncio
async def test_callback_additional_profile_shows_subscription_cta_for_inactive_user():
    status = SubscriptionStatus(
        user=SimpleNamespace(tg_id=1),
        client_data=None,
        plan=None,
        is_active=False,
        status_check_ok=True,
        period_duration_days=None,
        expiry_timestamp=None,
    )
    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(message=message)
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-1")
    services = SimpleNamespace(
        subscription=SimpleNamespace(
            get_subscription_status=AsyncMock(return_value=status),
            get_additional_profile_url=lambda _: "https://bot.example/wl/vpn-1",
            can_upgrade_plan=lambda _: False,
        )
    )
    config = SimpleNamespace(shop=SimpleNamespace(CURRENCY="RUB"))

    await callback_additional_profile(
        callback=callback,
        user=user,
        config=config,
        services=services,
    )

    kwargs = message.edit_text.await_args.kwargs
    assert "Оформите тариф с обходом БС" in kwargs["text"]
    assert "Открыть подписку" in flatten_keyboard_texts(kwargs["reply_markup"])
