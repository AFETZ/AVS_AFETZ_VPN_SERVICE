import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import IsAdmin, IsDev
from app.bot.payment_gateways import GatewayFactory
from app.bot.payment_gateways.yookassa import AutopayUnavailableError
from app.bot.services import ServicesContainer
from app.bot.utils.daily_test_autopay import (
    format_moscow_datetime,
    next_random_charge_at,
    utc_now_naive,
)
from app.bot.utils.navigation import NavAdminTools, NavSubscription
from app.db.models import DailyTestAutopayProfile, User

from .keyboard import admin_tools_keyboard

logger = logging.getLogger(__name__)
router = Router(name=__name__)


@router.callback_query(F.data == NavAdminTools.MAIN, IsAdmin())
async def callback_admin_tools(callback: CallbackQuery, user: User) -> None:
    logger.info(f"Admin {user.tg_id} opened admin tools.")
    is_dev = await IsDev()(user_id=user.tg_id)
    await callback.message.edit_text(
        text=_("admin_tools:message:main"),
        reply_markup=admin_tools_keyboard(is_dev),
    )

@router.callback_query(F.data == NavAdminTools.TEST, IsAdmin())
async def callback_admin_tools(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    services: ServicesContainer,
) -> None:
    logger.info(f"Admin {user.tg_id} clicked TEST BUTTON.")

    text = (
        "<b>bold</b>\n"
        "<i>italic</i>\n"
        "<u>underline</u>\n"
        "<s>strikethrough</s>\n"
        "<tg-spoiler>spoiler</tg-spoiler>\n\n"
        "<a href='http://www.example.com/'>inline URL</a>\n"
        "<a href='tg://user?id=243323533'>inline mention of a user</a>\n"
        "<tg-emoji emoji-id='5389102131527556772'>👍</tg-emoji>\n\n"
        "<code>inline fixed-width code</code>\n"
        "<pre>pre-formatted fixed-width code block</pre>\n"
        "<pre><code class='language-python'>pre-formatted fixed-width code block written in the Python programming language</code></pre>\n\n"
        "<blockquote>Block quotation started\nBlock quotation continued\nThe last line of the block quotation</blockquote>\n"
        "<blockquote expandable>Expandable block quotation started\nExpandable block quotation continued\nExpandable block quotation continued\nHidden by default part of the block quotation started\nExpandable block quotation continued\nThe last line of the block quotation</blockquote>\n"
    )

    await callback.message.answer(text=text)
    # logger.info(
    #     f"{user}\n\n{user.transactions}\n\n{user.server}\n\n{user.activated_promocodes}\n\n"
    # )
    # logger.info(f"{await vpn_service.get_key(user.tg_id)}\n\n")

    # connection = await server_pool_service.get_connection(user.server_id)

    # server = connection.server
    # logger.info(f"{server}\n\n{server.users}\n\n")

    # transaction = await Transaction.get(session=session, payment_id="test")
    # logger.info(f"{transaction}\n\n{transaction.user}")

    # logger.info(server.current_clients)


@router.callback_query(F.data == NavAdminTools.TEST_PURCHASE, IsAdmin())
async def callback_test_purchase(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    gateway_factory: GatewayFactory,
) -> None:
    logger.info("Admin %s opened daily test autopay settings.", user.tg_id)

    try:
        gateway_factory.get_gateway(NavSubscription.PAY_YOOKASSA)
    except ValueError:
        await callback.answer("YooKassa не подключена", show_alert=True)
        return

    profile = await DailyTestAutopayProfile.get(session, user.tg_id)
    is_bound = bool(profile and profile.payment_method_id)
    is_enabled = bool(profile and profile.enabled and is_bound)
    if is_enabled:
        status = "🟢 включён"
    elif is_bound:
        status = "⏸ приостановлен"
    else:
        status = "⚪ карта не привязана"

    builder = InlineKeyboardBuilder()
    if not is_bound:
        builder.row(
            InlineKeyboardButton(
                text="Привязать карту через YooKassa",
                callback_data=NavAdminTools.TEST_AUTOPAY_BIND,
            )
        )
    else:
        if is_enabled:
            builder.row(
                InlineKeyboardButton(
                    text="⏸ Приостановить",
                    callback_data=NavAdminTools.TEST_AUTOPAY_DISABLE,
                )
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text="▶️ Включить",
                    callback_data=NavAdminTools.TEST_AUTOPAY_ENABLE,
                )
            )
        builder.row(
            InlineKeyboardButton(
                text="🔄 Заменить карту",
                callback_data=NavAdminTools.TEST_AUTOPAY_BIND,
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Отключить автоплатёж и отвязать карту",
            callback_data=NavAdminTools.TEST_AUTOPAY_FORGET,
        )
    )
    builder.row(InlineKeyboardButton(text="Назад", callback_data=NavAdminTools.MAIN))

    await callback.message.edit_text(
        text=(
            "<b>Ежедневная тестовая покупка</b>\n\n"
            f"Статус: {status}\n"
            "Сумма: 10 ₽ один раз в день\n"
            "Время: случайно с 06:00 до 22:00 МСК\n"
            f"Следующее списание: {format_moscow_datetime(profile.next_charge_at if profile else None)}\n"
            f"Последняя успешная оплата: {format_moscow_datetime(profile.last_success_at if profile else None)}\n"
            f"Неудач подряд: {profile.consecutive_failures if profile else 0}\n\n"
            "Реквизиты карты бот не хранит. В базе сохраняется только идентификатор "
            "способа оплаты YooKassa."
        ),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == NavAdminTools.TEST_AUTOPAY_BIND, IsAdmin())
async def callback_test_autopay_bind_consent(
    callback: CallbackQuery,
) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Согласен и перейти к оплате 10 ₽",
            callback_data=NavAdminTools.TEST_AUTOPAY_BIND_CONFIRM,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Отмена",
            callback_data=NavAdminTools.TEST_PURCHASE,
        )
    )
    await callback.message.edit_text(
        text=(
            "<b>Подключение автоплатежа</b>\n\n"
            "Подтверждая подключение, вы соглашаетесь на списание 10 ₽ ежедневно "
            "в случайное время с 06:00 до 22:00 по московскому времени. "
            "Максимум — 310 ₽ за календарный месяц.\n\n"
            "Первый платёж 10 ₽ привяжет карту и считается сегодняшней тестовой "
            "покупкой. Следующее автоматическое списание будет не раньше завтра.\n\n"
            "Остановить списания или отвязать карту можно в любой момент в этом меню. "
            "Карта вводится только на защищённой странице YooKassa."
        ),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == NavAdminTools.TEST_AUTOPAY_BIND_CONFIRM, IsAdmin())
async def callback_test_autopay_bind_confirm(
    callback: CallbackQuery,
    user: User,
    gateway_factory: GatewayFactory,
) -> None:
    logger.info("Admin %s confirmed daily test autopay binding.", user.tg_id)

    try:
        gateway = gateway_factory.get_gateway(NavSubscription.PAY_YOOKASSA)
    except ValueError:
        await callback.answer("YooKassa не подключена", show_alert=True)
        return

    try:
        pay_url = await gateway.create_autopay_binding(user_tg_id=user.tg_id)
    except AutopayUnavailableError:
        logger.warning(
            "Daily test autopay is unavailable for YooKassa shop; recurring payments are disabled."
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="Назад",
                callback_data=NavAdminTools.TEST_PURCHASE,
            )
        )
        await callback.message.edit_text(
            text=(
                "❌ <b>Автоплатежи не подключены в YooKassa</b>\n\n"
                "Это рабочий магазин, но YooKassa запретила сохранение карты: "
                "функция рекуррентных платежей не активирована.\n\n"
                "Напишите менеджеру YooKassa: «Подключите рекуррентные платежи "
                "по API для магазина». После активации вернитесь сюда и повторите "
                "привязку."
            ),
            reply_markup=builder.as_markup(),
        )
        return
    except Exception as e:
        logger.exception("Autopay binding payment creation failed: %s", e)
        await callback.answer("Ошибка создания платежа", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Оплатить 10 ₽ и привязать карту", url=pay_url))
    builder.row(InlineKeyboardButton(text="Назад", callback_data=NavAdminTools.TEST_PURCHASE))

    await callback.message.edit_text(
        text=(
            "Откройте YooKassa и оплатите 10 ₽. При успешной оплате карта будет "
            "привязана для ежедневных тестовых покупок. Реквизиты карты боту не передаются."
        ),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == NavAdminTools.TEST_AUTOPAY_DISABLE, IsAdmin())
async def callback_test_autopay_disable(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    gateway_factory: GatewayFactory,
) -> None:
    profile = await DailyTestAutopayProfile.set_enabled(
        session,
        user_tg_id=user.tg_id,
        enabled=False,
    )
    await callback.answer(
        "Автоплатёж приостановлен" if profile else "Автоплатёж не найден",
        show_alert=True,
    )
    await callback_test_purchase(callback, user, session, gateway_factory)


@router.callback_query(F.data == NavAdminTools.TEST_AUTOPAY_ENABLE, IsAdmin())
async def callback_test_autopay_enable(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    gateway_factory: GatewayFactory,
) -> None:
    profile = await DailyTestAutopayProfile.set_enabled(
        session,
        user_tg_id=user.tg_id,
        enabled=True,
        next_charge_at=next_random_charge_at(utc_now_naive()),
    )
    await callback.answer(
        "Автоплатёж включён" if profile else "Сначала привяжите карту",
        show_alert=True,
    )
    await callback_test_purchase(callback, user, session, gateway_factory)


@router.callback_query(F.data == NavAdminTools.TEST_AUTOPAY_FORGET, IsAdmin())
async def callback_test_autopay_forget(
    callback: CallbackQuery,
) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Да, отключить и отвязать",
            callback_data=NavAdminTools.TEST_AUTOPAY_FORGET_CONFIRM,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Отмена",
            callback_data=NavAdminTools.TEST_PURCHASE,
        )
    )
    await callback.message.edit_text(
        text=(
            "Отключить ежедневные списания и удалить из базы идентификатор "
            "сохранённого способа оплаты?"
        ),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == NavAdminTools.TEST_AUTOPAY_FORGET_CONFIRM, IsAdmin())
async def callback_test_autopay_forget_confirm(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    gateway_factory: GatewayFactory,
) -> None:
    forgotten = await DailyTestAutopayProfile.forget_payment_method(
        session,
        user_tg_id=user.tg_id,
    )
    await callback.answer(
        (
            "Автоплатёж отключён, карта отвязана"
            if forgotten
            else "Автоплатёж уже отключён, сохранённой карты нет"
        ),
        show_alert=True,
    )
    await callback_test_purchase(callback, user, session, gateway_factory)
