import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.i18n import gettext as _
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import IsAdmin
from app.bot.models import AdminUserDetails, AdminUserListItem, AdminUserListPage, ServicesContainer
from app.bot.payment_gateways import GatewayFactory
from app.bot.routers.misc.keyboard import back_keyboard
from app.bot.utils.constants import MAIN_MESSAGE_ID_KEY, Currency
from app.bot.utils.formatting import format_date, format_remaining_time, format_subscription_period
from app.bot.utils.navigation import NavAdminTools
from app.db.models import User

from .keyboard import user_details_keyboard, user_editor_keyboard, user_list_keyboard
from .keyboard import user_plan_confirm_keyboard, user_plan_duration_keyboard, user_plan_keyboard

logger = logging.getLogger(__name__)
router = Router(name=__name__)

USERS_PER_PAGE = 8
USER_RETURN_CONTEXT_KEY = "user_return_context"
USER_SEARCH_QUERY_KEY = "user_search_query"
USER_TARGET_TG_ID_KEY = "target_tg_id"


class UserSearchStates(StatesGroup):
    waiting_search = State()


class UserMessageStates(StatesGroup):
    waiting_message = State()


class UserDiscountStates(StatesGroup):
    waiting_discount = State()


@router.callback_query(F.data == NavAdminTools.USER_EDITOR, IsAdmin())
async def callback_user_editor(
    callback: CallbackQuery,
    user: User,
    state: FSMContext,
    session: AsyncSession,
    services: ServicesContainer,
) -> None:
    logger.info("Admin %s opened user editor.", user.tg_id)
    overview = await services.admin_users.get_editor_overview(session=session)
    await state.set_state(None)
    await state.update_data(
        {
            USER_RETURN_CONTEXT_KEY: {"kind": "editor"},
            USER_SEARCH_QUERY_KEY: None,
        }
    )
    await callback.message.edit_text(
        text=_("user_editor:message:main").format(
            total=overview.total_users,
            paid=overview.paid_users,
            trial=overview.trial_users,
            inactive=overview.inactive_users,
            new_users_7d=overview.new_users_7d,
        ),
        reply_markup=user_editor_keyboard(overview=overview),
    )


@router.callback_query(
    F.data.in_(
        {
            NavAdminTools.USER_LIST,
            NavAdminTools.USER_ACTIVE_FILTER,
            NavAdminTools.USER_PAID_FILTER,
            NavAdminTools.USER_TRIAL_FILTER,
            NavAdminTools.USER_INACTIVE_FILTER,
            NavAdminTools.USER_ALL_FILTER,
        }
    ),
    IsAdmin(),
)
async def callback_user_list(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    logger.info("Admin %s opened user list.", user.tg_id)
    filter_type = _resolve_filter_type(callback.data)
    await _render_filter_page(
        callback_message=callback.message,
        filter_type=filter_type,
        page=0,
        session=session,
        state=state,
        services=services,
    )


@router.callback_query(F.data.startswith(NavAdminTools.USER_LIST_PAGE), IsAdmin())
async def callback_user_list_page(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    parts = callback.data.split("_")
    page = int(parts[-1])
    filter_type = parts[-2] if len(parts) > 3 else "all"
    logger.info(
        "Admin %s opened user list page %s for filter %s.",
        user.tg_id,
        page,
        filter_type,
    )

    if filter_type == "search":
        await _render_search_results_page(
            callback_message=callback.message,
            page=page,
            session=session,
            state=state,
            services=services,
        )
        return

    await _render_filter_page(
        callback_message=callback.message,
        filter_type=filter_type,
        page=page,
        session=session,
        state=state,
        services=services,
    )


@router.callback_query(F.data == NavAdminTools.USER_SEARCH, IsAdmin())
async def callback_user_search(
    callback: CallbackQuery,
    user: User,
    state: FSMContext,
) -> None:
    logger.info("Admin %s started user search.", user.tg_id)
    await _render_search_prompt(callback.message, state)


@router.message(UserSearchStates.waiting_search, IsAdmin())
async def handle_user_search(
    message: Message,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    forwarded_user_id = _extract_forwarded_user_id(message)
    query_text = str(forwarded_user_id) if forwarded_user_id else (message.text or "").strip()
    logger.info("Admin %s searching for: %s", user.tg_id, query_text)

    data = await state.get_data()
    main_message_id = data.get(MAIN_MESSAGE_ID_KEY)
    found_users = await services.admin_users.search_users(
        query_text=query_text,
        session=session,
    )

    if not found_users:
        await services.notification.notify_by_message(
            message=message,
            text=_("user_editor:ntf:not_found"),
            duration=5,
        )
        return

    if len(found_users) == 1:
        await state.set_state(None)
        await state.update_data(
            {
                USER_RETURN_CONTEXT_KEY: {"kind": "search_prompt"},
                USER_SEARCH_QUERY_KEY: query_text,
            }
        )
        await _render_user_details(
            chat_id=message.chat.id,
            message_id=main_message_id,
            tg_id=found_users[0].tg_id,
            session=session,
            services=services,
            gateway_factory=gateway_factory,
            bot=message.bot,
        )
        return

    await state.set_state(None)
    await state.update_data(
        {
            USER_RETURN_CONTEXT_KEY: {"kind": "search_results", "page": 0},
            USER_SEARCH_QUERY_KEY: query_text,
        }
    )

    search_page = services.admin_users.paginate_items(
        found_users,
        filter_type="search",
        page=0,
        limit=USERS_PER_PAGE,
    )
    await message.bot.edit_message_text(
        text=_build_search_results_text(query=query_text, user_page=search_page),
        chat_id=message.chat.id,
        message_id=main_message_id,
        reply_markup=user_list_keyboard(search_page),
    )


@router.callback_query(F.data.startswith(NavAdminTools.USER_DETAILS), IsAdmin())
async def callback_user_details(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    target_tg_id = int(callback.data.split("_")[-1])
    logger.info("Admin %s viewing user %s.", user.tg_id, target_tg_id)
    await state.set_state(None)
    await _render_user_details(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        tg_id=target_tg_id,
        session=session,
        services=services,
        gateway_factory=gateway_factory,
        bot=callback.bot,
    )


@router.callback_query(F.data == NavAdminTools.USER_BACK, IsAdmin())
async def callback_user_back(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    logger.info("Admin %s navigated back inside user editor.", user.tg_id)
    context = (await state.get_data()).get(USER_RETURN_CONTEXT_KEY) or {"kind": "editor"}
    kind = context.get("kind")

    if kind == "filter":
        await _render_filter_page(
            callback_message=callback.message,
            filter_type=context.get("filter_type", "all"),
            page=context.get("page", 0),
            session=session,
            state=state,
            services=services,
        )
        return

    if kind == "search_results":
        await _render_search_results_page(
            callback_message=callback.message,
            page=context.get("page", 0),
            session=session,
            state=state,
            services=services,
        )
        return

    if kind == "search_prompt":
        await _render_search_prompt(callback.message, state)
        return

    await callback_user_editor(
        callback=callback,
        user=user,
        state=state,
        session=session,
        services=services,
    )


@router.callback_query(F.data.startswith(NavAdminTools.USER_SEND_MESSAGE), IsAdmin())
async def callback_user_send_message(
    callback: CallbackQuery,
    user: User,
    state: FSMContext,
) -> None:
    target_tg_id = int(callback.data.split("_")[-1])
    logger.info("Admin %s wants to send message to %s.", user.tg_id, target_tg_id)

    await state.set_state(UserMessageStates.waiting_message)
    await state.update_data(
        {
            MAIN_MESSAGE_ID_KEY: callback.message.message_id,
            USER_TARGET_TG_ID_KEY: target_tg_id,
        }
    )

    await callback.message.edit_text(
        text=_("user_editor:message:enter_message").format(tg_id=target_tg_id),
        reply_markup=back_keyboard(NavAdminTools.USER_DETAILS + f"_{target_tg_id}"),
    )


@router.message(UserMessageStates.waiting_message, IsAdmin())
async def handle_user_send_message(
    message: Message,
    user: User,
    state: FSMContext,
    services: ServicesContainer,
    session: AsyncSession,
    gateway_factory: GatewayFactory,
) -> None:
    data = await state.get_data()
    target_tg_id = data.get(USER_TARGET_TG_ID_KEY)
    main_message_id = data.get(MAIN_MESSAGE_ID_KEY)

    try:
        await message.bot.send_message(chat_id=target_tg_id, text=message.text)
        await state.set_state(None)
        await _render_user_details(
            chat_id=message.chat.id,
            message_id=main_message_id,
            tg_id=target_tg_id,
            session=session,
            services=services,
            gateway_factory=gateway_factory,
            bot=message.bot,
        )
        await services.notification.notify_by_message(
            message=message,
            text=_("user_editor:ntf:message_sent"),
            duration=5,
        )
    except Exception as exception:
        logger.error("Failed to send message to %s: %s", target_tg_id, exception)
        await services.notification.notify_by_message(
            message=message,
            text=_("user_editor:ntf:message_failed"),
            duration=5,
        )


@router.callback_query(F.data.startswith(NavAdminTools.USER_EDIT_SUBSCRIPTION.value + ":"), IsAdmin())
async def callback_user_edit_subscription(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    services: ServicesContainer,
) -> None:
    target_tg_id = int(callback.data.split(":")[1])
    target = await User.get(session=session, tg_id=target_tg_id)
    if not target:
        await callback.answer(text=_("user_editor:popup:user_not_found"), show_alert=True)
        return

    await callback.message.edit_text(
        text=(
            "✏️ <b>Изменить подписку пользователя</b>\n\n"
            f"Пользователь: <code>{target.tg_id}</code>\n"
            "Выберите тариф. После выбора срока бот пересоздаст/обновит клиента в 3X-UI "
            "и запишет тариф в карточку пользователя."
        ),
        reply_markup=user_plan_keyboard(
            tg_id=target_tg_id,
            plans=services.plan.get_all_plan_records(),
        ),
    )


@router.callback_query(F.data.startswith(NavAdminTools.USER_SET_PLAN.value + ":"), IsAdmin())
async def callback_user_set_plan(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
) -> None:
    _, target_tg_id, plan_code = callback.data.split(":", 2)
    plan = services.plan.get_plan_by_code(plan_code)
    if not plan:
        await callback.answer(text="Тариф не найден.", show_alert=True)
        return

    await callback.message.edit_text(
        text=(
            "✏️ <b>Изменить подписку пользователя</b>\n\n"
            f"Пользователь: <code>{target_tg_id}</code>\n"
            f"Тариф: <b>{plan.title or plan.code}</b>\n"
            "Выберите срок подписки."
        ),
        reply_markup=user_plan_duration_keyboard(
            tg_id=int(target_tg_id),
            plan=plan,
            durations=services.plan.get_durations(),
        ),
    )


@router.callback_query(F.data.startswith(NavAdminTools.USER_SET_PLAN_DURATION.value + ":"), IsAdmin())
async def callback_user_set_plan_duration(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
) -> None:
    _, target_tg_id, plan_code, duration_raw = callback.data.split(":", 3)
    plan = services.plan.get_plan_by_code(plan_code)
    if not plan:
        await callback.answer(text="Тариф не найден.", show_alert=True)
        return

    duration = int(duration_raw)
    await callback.message.edit_text(
        text=(
            "⚠️ <b>Подтвердите изменение подписки</b>\n\n"
            f"Пользователь: <code>{target_tg_id}</code>\n"
            f"Тариф: <b>{plan.title or plan.code}</b>\n"
            f"Устройств: <b>{plan.devices}</b>\n"
            f"Срок: <b>{duration} дн.</b>\n\n"
            "Текущий клиент в 3X-UI будет обновлен: лимит устройств и срок будут заменены."
        ),
        reply_markup=user_plan_confirm_keyboard(
            tg_id=int(target_tg_id),
            plan_code=plan_code,
            duration=duration,
        ),
    )


@router.callback_query(F.data.startswith(NavAdminTools.USER_CONFIRM_PLAN.value + ":"), IsAdmin())
async def callback_user_confirm_plan(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    _, target_tg_id_raw, plan_code, duration_raw = callback.data.split(":", 3)
    target_tg_id = int(target_tg_id_raw)
    duration = int(duration_raw)
    target = await User.get(session=session, tg_id=target_tg_id)
    plan = services.plan.get_plan_by_code(plan_code)
    if not target or not plan:
        await callback.answer(text="Пользователь или тариф не найден.", show_alert=True)
        return

    success = await services.vpn.create_subscription(
        user=target,
        devices=plan.devices,
        duration=duration,
    )
    if not success:
        await callback.answer(text="Не удалось обновить клиента в 3X-UI.", show_alert=True)
        return

    await services.subscription.update_current_plan(
        user=target,
        plan_code=plan.code,
        refresh_period=True,
        period_duration_days=duration,
    )
    session.expire_all()
    await callback.answer(text="Подписка обновлена.")
    await _render_user_details(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        tg_id=target_tg_id,
        session=session,
        services=services,
        gateway_factory=gateway_factory,
        bot=callback.bot,
    )


@router.callback_query(F.data.startswith("user_lifetime:"), IsAdmin())
async def callback_user_lifetime(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"0", "1"}:
        await callback.answer(text="Некорректная команда.", show_alert=True)
        return
    target = await User.get(session=session, tg_id=int(parts[1]))
    if not target:
        await callback.answer(text=_("user_editor:popup:user_not_found"), show_alert=True)
        return
    enabled = parts[2] == "1"
    if enabled and target.is_blocked:
        await callback.answer(text="Сначала разблокируйте пользователя.", show_alert=True)
        return
    await callback.answer()
    if not await services.vpn.set_lifetime_subscription(target, enabled=enabled):
        await callback.message.edit_text(
            text="Не удалось изменить подписку на всех серверах. Проверьте доступность панелей и наличие клиента, затем повторите.",
            reply_markup=back_keyboard(NavAdminTools.USER_DETAILS + f"_{target.tg_id}"),
        )
        return
    if enabled:
        await services.subscription.clear_scheduled_plan(user=target)
    logger.info("Admin %s set lifetime subscription for %s enabled=%s", user.tg_id, target.tg_id, enabled)
    session.expire_all()
    await _render_user_details(
        chat_id=callback.message.chat.id, message_id=callback.message.message_id,
        tg_id=int(parts[1]), session=session, services=services,
        gateway_factory=gateway_factory, bot=callback.bot,
    )


@router.callback_query(F.data.startswith(NavAdminTools.USER_TOGGLE_BLOCK.value + ":"), IsAdmin())
async def callback_user_toggle_block(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    target_tg_id = int(callback.data.split(":")[1])
    target = await User.get(session=session, tg_id=target_tg_id)
    if not target:
        await callback.answer(text=_("user_editor:popup:user_not_found"), show_alert=True)
        return

    should_block = not target.is_blocked
    if target.server_id:
        vpn_updated = await services.vpn.set_client_enabled(target, enabled=not should_block)
        if not vpn_updated:
            await callback.answer(text="Не удалось обновить клиента в 3X-UI.", show_alert=True)
            return

    await User.update(session=session, tg_id=target_tg_id, is_blocked=should_block)
    session.expire_all()
    await callback.answer(text="Пользователь заблокирован." if should_block else "Пользователь разблокирован.")
    await _render_user_details(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        tg_id=target_tg_id,
        session=session,
        services=services,
        gateway_factory=gateway_factory,
        bot=callback.bot,
    )


@router.callback_query(F.data.startswith(NavAdminTools.USER_SET_DISCOUNT.value + ":"), IsAdmin())
async def callback_user_set_discount(
    callback: CallbackQuery,
    user: User,
    state: FSMContext,
) -> None:
    target_tg_id = int(callback.data.split(":")[1])
    await state.set_state(UserDiscountStates.waiting_discount)
    await state.update_data(
        {
            MAIN_MESSAGE_ID_KEY: callback.message.message_id,
            USER_TARGET_TG_ID_KEY: target_tg_id,
        }
    )
    await callback.message.edit_text(
        text=(
            "🏷️ <b>Персональная скидка</b>\n\n"
            f"Пользователь: <code>{target_tg_id}</code>\n"
            "Введите процент скидки от 0 до 95. 0 отключает персональную скидку."
        ),
        reply_markup=back_keyboard(NavAdminTools.USER_DETAILS + f"_{target_tg_id}"),
    )


@router.message(UserDiscountStates.waiting_discount, IsAdmin())
async def handle_user_discount(
    message: Message,
    user: User,
    state: FSMContext,
    session: AsyncSession,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    data = await state.get_data()
    target_tg_id = int(data.get(USER_TARGET_TG_ID_KEY))
    main_message_id = data.get(MAIN_MESSAGE_ID_KEY)

    try:
        discount = int((message.text or "").strip())
    except ValueError:
        await services.notification.notify_by_message(
            message=message,
            text="Введите число от 0 до 95.",
            duration=5,
        )
        return

    if discount < 0 or discount > 95:
        await services.notification.notify_by_message(
            message=message,
            text="Скидка должна быть от 0 до 95.",
            duration=5,
        )
        return

    await User.update(
        session=session,
        tg_id=target_tg_id,
        personal_discount_percent=discount,
    )
    session.expire_all()
    await state.set_state(None)
    await _render_user_details(
        chat_id=message.chat.id,
        message_id=main_message_id,
        tg_id=target_tg_id,
        session=session,
        services=services,
        gateway_factory=gateway_factory,
        bot=message.bot,
    )
    await services.notification.notify_by_message(
        message=message,
        text="Скидка сохранена.",
        duration=5,
    )


async def _render_filter_page(
    *,
    callback_message,
    filter_type: str,
    page: int,
    session: AsyncSession,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    user_page = await services.admin_users.get_user_page(
        filter_type=filter_type,
        page=page,
        limit=USERS_PER_PAGE,
        session=session,
    )
    if user_page.total <= 0:
        await state.update_data(
            {
                USER_RETURN_CONTEXT_KEY: {"kind": "editor"},
                USER_SEARCH_QUERY_KEY: None,
            }
        )
        await callback_message.edit_text(
            text=_("user_editor:message:no_users"),
            reply_markup=back_keyboard(NavAdminTools.USER_EDITOR),
        )
        return

    await state.update_data(
        {
            USER_RETURN_CONTEXT_KEY: {
                "kind": "filter",
                "filter_type": user_page.filter_type,
                "page": user_page.page,
            },
            USER_SEARCH_QUERY_KEY: None,
        }
    )
    await callback_message.edit_text(
        text=_build_user_list_text(user_page),
        reply_markup=user_list_keyboard(user_page),
    )


async def _render_search_prompt(callback_message, state: FSMContext) -> None:
    await state.set_state(UserSearchStates.waiting_search)
    await state.update_data(
        {
            MAIN_MESSAGE_ID_KEY: callback_message.message_id,
            USER_RETURN_CONTEXT_KEY: {"kind": "search_prompt"},
        }
    )
    await callback_message.edit_text(
        text=_("user_editor:message:search"),
        reply_markup=back_keyboard(NavAdminTools.USER_EDITOR),
    )


async def _render_search_results_page(
    *,
    callback_message,
    page: int,
    session: AsyncSession,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    data = await state.get_data()
    query_text = data.get(USER_SEARCH_QUERY_KEY)
    if not query_text:
        await _render_search_prompt(callback_message, state)
        return

    found_users = await services.admin_users.search_users(
        query_text=query_text,
        session=session,
    )
    if not found_users:
        await _render_search_prompt(callback_message, state)
        return

    search_page = services.admin_users.paginate_items(
        found_users,
        filter_type="search",
        page=page,
        limit=USERS_PER_PAGE,
    )
    await state.update_data(
        {
            USER_RETURN_CONTEXT_KEY: {
                "kind": "search_results",
                "page": search_page.page,
            }
        }
    )
    await callback_message.edit_text(
        text=_build_search_results_text(query=query_text, user_page=search_page),
        reply_markup=user_list_keyboard(search_page),
    )


async def _render_user_details(
    *,
    chat_id: int,
    message_id: int,
    tg_id: int,
    session: AsyncSession,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
    bot,
) -> None:
    details = await services.admin_users.get_user_details(
        tg_id=tg_id,
        session=session,
        payment_method_currencies=_get_payment_method_currencies(gateway_factory),
    )
    if not details:
        await bot.edit_message_text(
            text=_("user_editor:message:no_users"),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=back_keyboard(NavAdminTools.USER_EDITOR),
        )
        return

    await bot.edit_message_text(
        text=_build_user_details_text(details),
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=user_details_keyboard(details.tg_id, is_blocked=details.is_blocked, is_lifetime=details.expiry_timestamp == -1, subscription_enabled=details.subscription_active),
    )


def _build_user_list_text(user_page: AdminUserListPage) -> str:
    return _("user_editor:message:list").format(
        total=user_page.total,
        filter=_get_filter_label(user_page.filter_type),
        page=user_page.page + 1,
        pages=user_page.pages,
    )


def _build_search_results_text(*, query: str, user_page: AdminUserListPage) -> str:
    return _("user_editor:message:search_results").format(
        total=user_page.total,
        query=query,
        page=user_page.page + 1,
        pages=user_page.pages,
    )


def _build_user_details_text(details: AdminUserDetails) -> str:
    username_display = f"@{details.username}" if details.username else "—"
    tg_link = f"tg://user?id={details.tg_id}"

    if details.subscription_active and details.expiry_timestamp not in (None, -1):
        sub_status = _("user_editor:detail:active_subscription").format(
            plan=details.subscription_plan_code or "?",
            days_left=format_remaining_time(details.expiry_timestamp),
        )
    elif details.subscription_active:
        sub_status = _("user_editor:detail:active_subscription").format(
            plan=details.subscription_plan_code or "?",
            days_left="∞",
        )
    elif not details.subscription_status_ok:
        sub_status = _("user_editor:detail:panel_unavailable")
    elif details.subscription_plan_code:
        sub_status = _("user_editor:detail:expired_subscription").format(
            plan=details.subscription_plan_code,
        )
    else:
        sub_status = _("user_editor:detail:no_subscription")

    revenue_text = _format_revenue(details.revenue_by_currency)
    referral_info = (
        _("user_editor:detail:referred_by").format(referrer_tg_id=details.referrer_tg_id)
        if details.referrer_tg_id
        else _("user_editor:detail:no_referrer")
    )

    base_text = _("user_editor:message:details").format(
        first_name=details.first_name,
        tg_id=details.tg_id,
        tg_link=tg_link,
        username=username_display,
        vpn_id=details.vpn_id,
        created_at=details.created_at.strftime("%Y-%m-%d %H:%M"),
        language=details.language_code,
        server=details.server_name or "—",
        subscription=sub_status,
        devices=details.devices or "—",
        traffic_used=details.traffic_used or "—",
        total_transactions=details.total_transactions,
        completed_transactions=details.completed_transactions,
        first_payment_at=_format_datetime(details.first_payment_at),
        last_payment_at=_format_datetime(details.last_payment_at),
        revenue_text=revenue_text,
        referrals_count=details.referral_count,
        referral_info=referral_info,
        trial_used="+" if details.trial_used else "—",
        source_invite=details.source_invite_name or "—",
    )
    if details.expiry_timestamp == -1:
        base_text += "\n<b>Вечная подписка:</b> " + ("включена" if details.subscription_active else "отключена")
    block_text = "да" if details.is_blocked else "нет"
    server_status = (
        "🟢 online" if details.server_online else "🔴 offline"
        if details.server_online is not None else "—"
    )
    if details.pending_plan_code:
        pending_start = (
            format_date(details.pending_plan_starts_at)
            if details.pending_plan_starts_at
            else "после текущей подписки"
        )
        pending_duration = (
            format_subscription_period(details.pending_period_duration_days)
            if details.pending_period_duration_days
            else "—"
        )
        pending_text = f"{details.pending_plan_code}, {pending_duration}, старт: {pending_start}"
    else:
        pending_text = "—"
    extra_lines = [
        "",
        f"<b>Блокировка:</b> {block_text}",
        f"<b>Персональная скидка:</b> {details.personal_discount_percent}%",
        f"<b>Запланированный тариф:</b> {escape(pending_text)}",
        f"<b>Server host:</b> <code>{escape(details.server_host or '—')}</code>",
        f"<b>Server status:</b> {server_status}",
        "",
        "<b>Последние платежи:</b>",
        _format_admin_list(details.latest_transactions),
        "",
        "<b>Активированные промокоды:</b>",
        _format_admin_list(details.activated_promocodes),
    ]
    return base_text + "\n".join(extra_lines)


def _resolve_filter_type(callback_data: str) -> str:
    if callback_data in (NavAdminTools.USER_ACTIVE_FILTER, NavAdminTools.USER_PAID_FILTER):
        return "paid"
    if callback_data == NavAdminTools.USER_TRIAL_FILTER:
        return "trial"
    if callback_data == NavAdminTools.USER_INACTIVE_FILTER:
        return "inactive"
    return "all"


def _get_filter_label(filter_type: str) -> str:
    if filter_type in ("paid", "active"):
        return _("user_editor:filter:paid")
    if filter_type == "trial":
        return _("user_editor:filter:trial")
    if filter_type == "inactive":
        return _("user_editor:filter:inactive")
    return _("user_editor:filter:all")


def _format_datetime(value) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M")


def _format_revenue(revenue_by_currency: dict[str, float]) -> str:
    if not revenue_by_currency:
        return "• " + _("statistics:revenue:none")

    lines: list[str] = []
    for currency_code, amount in revenue_by_currency.items():
        try:
            currency_symbol = Currency.from_code(currency_code).symbol
        except ValueError:
            currency_symbol = currency_code
        lines.append(f"• {amount:.2f} {currency_symbol}")
    return "\n".join(lines)


def _get_payment_method_currencies(gateway_factory: GatewayFactory) -> dict[str, str]:
    return {gateway.callback: gateway.currency.code for gateway in gateway_factory.get_gateways()}


def _format_admin_list(items: list[str]) -> str:
    if not items:
        return "• —"
    return "\n".join(f"• <code>{escape(item)}</code>" for item in items)


def _extract_forwarded_user_id(message: Message) -> int | None:
    forward_from = getattr(message, "forward_from", None)
    if forward_from and getattr(forward_from, "id", None):
        return int(forward_from.id)

    forward_origin = getattr(message, "forward_origin", None)
    sender_user = getattr(forward_origin, "sender_user", None)
    if sender_user and getattr(sender_user, "id", None):
        return int(sender_user.id)

    return None
