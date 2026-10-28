from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.routers.misc.keyboard import (
    back_button,
    back_to_main_menu_button,
    cancel_button,
)
from app.bot.utils.navigation import NavAdminTools
from app.bot.models import AdminUserEditorOverview, AdminUserListPage, Plan
from app.db.models import Server
from app.db.models.invite import Invite

PROMOCODE_CUSTOM_DURATION_CALLBACK = "promocode_custom_duration"
PROMOCODE_CUSTOM_ACTIVATIONS_CALLBACK = "promocode_custom_activations"


def admin_tools_keyboard(is_dev: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:server_management"),
            callback_data=NavAdminTools.SERVER_MANAGEMENT,
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:statistics"),
            callback_data=NavAdminTools.STATISTICS,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🩺 Состояние",
            callback_data=NavAdminTools.HEALTH,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:user_editor"),
            callback_data=NavAdminTools.USER_EDITOR,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:invite_editor"),
            callback_data=NavAdminTools.INVITE_EDITOR,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:promocode_editor"),
            callback_data=NavAdminTools.PROMOCODE_EDITOR,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📦 Тарифы и платежи",
            callback_data=NavAdminTools.PLAN_EDITOR,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:notification"),
            callback_data=NavAdminTools.NOTIFICATION,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:create_backup"),
            callback_data=NavAdminTools.CREATE_BACKUP,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:maintenance_mode"),
            callback_data=NavAdminTools.MAINTENANCE_MODE,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:restart_bot"),
            callback_data=NavAdminTools.RESTART_BOT,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("admin_tools:button:test_button"),
            callback_data=NavAdminTools.TEST,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="💳 Автоплатёж 10 ₽",
            callback_data=NavAdminTools.TEST_PURCHASE,
        )
    )

    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def promocode_editor_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("promocode_editor:button:create"),
            callback_data=NavAdminTools.CREATE_PROMOCODE,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("promocode_editor:button:delete"),
            callback_data=NavAdminTools.DELETE_PROMOCODE,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("promocode_editor:button:edit"),
            callback_data=NavAdminTools.EDIT_PROMOCODE,
        )
    )

    builder.adjust(3)
    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def promocode_duration_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    duration_options = [1, 2, 3, 4, 5, 6, 7, 14, 20, 30, 90, 180, 365]

    for duration in duration_options:
        duration_text = _("1 day", "{} days", duration).format(duration)
        builder.add(
            InlineKeyboardButton(
                text=duration_text,
                callback_data=f"{duration}",
            )
        )

    builder.adjust(3)
    builder.row(
        InlineKeyboardButton(
            text=_("promocode_editor:button:manual_duration"),
            callback_data=PROMOCODE_CUSTOM_DURATION_CALLBACK,
        )
    )
    builder.row(back_button(NavAdminTools.PROMOCODE_EDITOR))
    return builder.as_markup()


def promocode_activations_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    options = [1, 10, 25, 50, 100, 0]

    for count in options:
        if count == 0:
            label = _("promocode_editor:button:unlimited")
        elif count == 1:
            label = _("promocode_editor:button:single_use")
        else:
            label = f"{count}"
        builder.add(
            InlineKeyboardButton(
                text=label,
                callback_data=f"promo_act_{count}",
            )
        )

    builder.adjust(3)
    builder.row(
        InlineKeyboardButton(
            text=_("promocode_editor:button:manual_activations"),
            callback_data=PROMOCODE_CUSTOM_ACTIVATIONS_CALLBACK,
        )
    )
    builder.row(back_button(NavAdminTools.CREATE_PROMOCODE))
    return builder.as_markup()


def maintenance_mode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    from app.bot.middlewares import MaintenanceMiddleware

    if MaintenanceMiddleware.active:
        builder.row(
            InlineKeyboardButton(
                text=_("maintenance_mode:button:disable"),
                callback_data=NavAdminTools.MAINTENANCE_MODE_DISABLE,
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text=_("maintenance_mode:button:enable"),
                callback_data=NavAdminTools.MAINTENANCE_MODE_ENABLE,
            )
        )

    builder.adjust(2)
    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def servers_keyboard(servers: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.add(
        InlineKeyboardButton(
            text=_("server_management:button:sync"),
            callback_data=NavAdminTools.SYNC_SERVERS,
        )
    )

    builder.add(
        InlineKeyboardButton(
            text=_("server_management:button:add"),
            callback_data=NavAdminTools.ADD_SERVER,
        )
    )

    server: Server
    for server in servers:
        status = "🟢" if server.online else "🔴"
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {server.name}",
                callback_data=NavAdminTools.SHOW_SERVER + f"_{server.name}",
            )
        )

    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def server_keyboard(server_name: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("server_management:button:ping"),
            callback_data=NavAdminTools.PING_SERVER + f"_{server_name}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("server_management:button:delete"),
            callback_data=NavAdminTools.DELETE_SERVER + f"_{server_name}",
        )
    )

    builder.adjust(2)
    builder.row(back_button(NavAdminTools.SERVER_MANAGEMENT))
    return builder.as_markup()


def confirm_add_server_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("server_management:button:confirm"),
            callback_data=NavAdminTools.СONFIRM_ADD_SERVER,
        )
    )

    builder.adjust(2)
    builder.row(back_button(NavAdminTools.ADD_SERVER_BACK))
    return builder.as_markup()


def notification_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("notification:button:send_to_user"),
            callback_data=NavAdminTools.SEND_NOTIFICATION_USER,
        ),
        InlineKeyboardButton(
            text=_("notification:button:send_to_all"),
            callback_data=NavAdminTools.SEND_NOTIFICATION_ALL,
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text=_("notification:button:last_notification"),
            callback_data=NavAdminTools.LAST_NOTIFICATION,
        )
    )

    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def last_notification_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.add(
        InlineKeyboardButton(
            text=_("notification:button:edit"),
            callback_data=NavAdminTools.EDIT_NOTIFICATION,
        )
    )

    builder.add(
        InlineKeyboardButton(
            text=_("notification:button:delete"),
            callback_data=NavAdminTools.DELETE_NOTIFICATION,
        )
    )

    builder.row(back_button(NavAdminTools.NOTIFICATION))
    return builder.as_markup()


def confirm_send_notification_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("notification:button:confirm"),
            callback_data=NavAdminTools.CONFIRM_SEND_NOTIFICATION,
        )
    )
    builder.row(cancel_button(NavAdminTools.NOTIFICATION))
    return builder.as_markup()


def invite_editor_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("invite_editor:button:create_invite"),
            callback_data=NavAdminTools.CREATE_INVITE,
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=_("invite_editor:button:list_invites"),
            callback_data=NavAdminTools.LIST_INVITES,
        )
    )

    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def invite_list_keyboard(
    invites: list[Invite], page: int = 0, limit: int = 5
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    total_invites = len(invites)
    start_idx = page * limit
    end_idx = min(start_idx + limit, total_invites)

    for i in range(start_idx, end_idx):
        invite = invites[i]
        builder.row(
            InlineKeyboardButton(
                text=f"{invite.name} ({invite.clicks} clicks)",
                callback_data=NavAdminTools.SHOW_INVITE_DETAILS + f"_{invite.id}",
            )
        )

    row = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                text=_("invite_editor:button:previous_page"),
                callback_data=NavAdminTools.SHOW_INVITE_PAGE + f"_{page-1}",
            )
        )

    if (page + 1) * limit < total_invites:
        row.append(
            InlineKeyboardButton(
                text=_("invite_editor:button:next_page"),
                callback_data=NavAdminTools.SHOW_INVITE_PAGE + f"_{page+1}",
            )
        )

    if row:
        builder.row(*row)

    builder.row(back_button(NavAdminTools.INVITE_EDITOR))

    return builder.as_markup()


def invite_details_keyboard(invite: Invite) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if invite.is_active:
        builder.row(
            InlineKeyboardButton(
                text=_("invite_editor:button:disable"),
                callback_data=NavAdminTools.TOGGLE_INVITE_STATUS + f"_{invite.id}",
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(
                text=_("invite_editor:button:enable"),
                callback_data=NavAdminTools.TOGGLE_INVITE_STATUS + f"_{invite.id}",
            )
        )

    builder.row(
        InlineKeyboardButton(
            text=_("invite_editor:button:delete"),
            callback_data=NavAdminTools.CONFIRM_DELETE_INVITE + f"_{invite.id}",
        )
    )

    builder.row(back_button(NavAdminTools.LIST_INVITES))

    return builder.as_markup()


def user_editor_keyboard(
    overview: AdminUserEditorOverview | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    total_suffix = f" ({overview.total_users})" if overview else ""
    paid_suffix = f" ({overview.paid_users})" if overview else ""
    trial_suffix = f" ({overview.trial_users})" if overview else ""
    inactive_suffix = f" ({overview.inactive_users})" if overview else ""

    builder.row(
        InlineKeyboardButton(
            text=_("user_editor:button:list_users") + total_suffix,
            callback_data=NavAdminTools.USER_LIST,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("user_editor:button:search"),
            callback_data=NavAdminTools.USER_SEARCH,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("user_editor:button:paid_users") + paid_suffix,
            callback_data=NavAdminTools.USER_PAID_FILTER,
        ),
        InlineKeyboardButton(
            text=_("user_editor:button:trial_users") + trial_suffix,
            callback_data=NavAdminTools.USER_TRIAL_FILTER,
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=_("user_editor:button:inactive_users") + inactive_suffix,
            callback_data=NavAdminTools.USER_INACTIVE_FILTER,
        ),
    )

    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def user_list_keyboard(
    user_page: AdminUserListPage,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for item in user_page.items:
        if item.has_paid:
            marker = "💳"
        elif item.current_plan_code:
            marker = "🎁"
        else:
            marker = "—"
        label = f"[{marker}] {item.display_name} ({item.tg_id})"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=NavAdminTools.USER_DETAILS + f"_{item.tg_id}",
            )
        )

    nav_row = []
    if user_page.page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text=_("user_editor:button:prev_page"),
                callback_data=(
                    NavAdminTools.USER_LIST_PAGE + f"_{user_page.filter_type}_{user_page.page - 1}"
                ),
            )
        )

    if user_page.page + 1 < user_page.pages:
        nav_row.append(
            InlineKeyboardButton(
                text=_("user_editor:button:next_page"),
                callback_data=(
                    NavAdminTools.USER_LIST_PAGE + f"_{user_page.filter_type}_{user_page.page + 1}"
                ),
            )
        )

    if nav_row:
        builder.row(*nav_row)

    builder.row(back_button(NavAdminTools.USER_EDITOR))
    return builder.as_markup()


def user_details_keyboard(tg_id: int, *, is_blocked: bool = False, is_lifetime: bool = False, subscription_enabled: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(InlineKeyboardButton(
        text="⏸ Отключить вечную подписку" if is_lifetime and subscription_enabled else "∞ Включить вечную подписку",
        callback_data=f"user_lifetime:{tg_id}:{0 if is_lifetime and subscription_enabled else 1}",
    ))
    builder.row(
        InlineKeyboardButton(
            text=_("user_editor:button:send_message"),
            callback_data=NavAdminTools.USER_SEND_MESSAGE + f"_{tg_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✏️ Изменить подписку",
            callback_data=f"{NavAdminTools.USER_EDIT_SUBSCRIPTION.value}:{tg_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔓 Разблокировать" if is_blocked else "🔒 Заблокировать",
            callback_data=f"{NavAdminTools.USER_TOGGLE_BLOCK.value}:{tg_id}",
        ),
        InlineKeyboardButton(
            text="🏷️ Персональная скидка",
            callback_data=f"{NavAdminTools.USER_SET_DISCOUNT.value}:{tg_id}",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=_("user_editor:button:open_in_tg"),
            url=f"tg://user?id={tg_id}",
        )
    )

    builder.row(back_button(NavAdminTools.USER_BACK))
    builder.row(back_button(NavAdminTools.USER_EDITOR, _("user_editor:button:back_to_editor")))
    return builder.as_markup()


def user_plan_keyboard(tg_id: int, plans: list[Plan]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        visibility = "👁" if plan.is_public else "🙈"
        profile = " + обход" if plan.includes_additional_profile else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{visibility} {plan.code} · {plan.title or plan.devices}{profile}",
                callback_data=f"{NavAdminTools.USER_SET_PLAN.value}:{tg_id}:{plan.code}",
            )
        )

    builder.row(back_button(NavAdminTools.USER_DETAILS + f"_{tg_id}"))
    return builder.as_markup()


def user_plan_duration_keyboard(tg_id: int, plan: Plan, durations: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for duration in plan.get_available_durations(durations):
        builder.add(
            InlineKeyboardButton(
                text=f"{duration} дн.",
                callback_data=f"{NavAdminTools.USER_SET_PLAN_DURATION.value}:{tg_id}:{plan.code}:{duration}",
            )
        )
    builder.adjust(3)
    builder.row(back_button(f"{NavAdminTools.USER_EDIT_SUBSCRIPTION.value}:{tg_id}"))
    return builder.as_markup()


def user_plan_confirm_keyboard(tg_id: int, plan_code: str, duration: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Применить",
            callback_data=f"{NavAdminTools.USER_CONFIRM_PLAN.value}:{tg_id}:{plan_code}:{duration}",
        )
    )
    builder.row(back_button(f"{NavAdminTools.USER_SET_PLAN.value}:{tg_id}:{plan_code}"))
    return builder.as_markup()


def plan_editor_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 Список тарифов", callback_data=NavAdminTools.PLAN_LIST),
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить тариф", callback_data=NavAdminTools.PLAN_ADD),
    )
    builder.row(
        InlineKeyboardButton(text="💳 Порядок платежей", callback_data=NavAdminTools.PAYMENT_ORDER),
    )
    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def plan_list_keyboard(plans: list[Plan]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        visibility = "👁" if plan.is_public else "🙈"
        addon = " + обход" if plan.includes_additional_profile else ""
        popular = " 🔥" if plan.is_popular else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{visibility} {plan.code}{popular} · {plan.title or plan.devices}{addon}",
                callback_data=f"{NavAdminTools.PLAN_SHOW.value}:{plan.code}",
            )
        )
    builder.row(back_button(NavAdminTools.PLAN_EDITOR))
    return builder.as_markup()


def plan_details_keyboard(plan_code: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✏️ Редактировать JSON",
            callback_data=f"{NavAdminTools.PLAN_EDIT_JSON.value}:{plan_code}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=f"{NavAdminTools.PLAN_DELETE.value}:{plan_code}",
        )
    )
    builder.row(back_button(NavAdminTools.PLAN_LIST))
    return builder.as_markup()


def confirm_delete_plan_keyboard(plan_code: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Да, удалить",
            callback_data=f"{NavAdminTools.PLAN_CONFIRM_DELETE.value}:{plan_code}",
        )
    )
    builder.row(cancel_button(f"{NavAdminTools.PLAN_SHOW.value}:{plan_code}"))
    return builder.as_markup()


def payment_order_keyboard(gateways: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, gateway in enumerate(gateways, start=1):
        builder.row(
            InlineKeyboardButton(
                text=f"{index}. {gateway.name}",
                callback_data=NavAdminTools.PAYMENT_ORDER,
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="⬆️",
                callback_data=f"{NavAdminTools.PAYMENT_MOVE_UP.value}:{gateway.callback}",
            ),
            InlineKeyboardButton(
                text="⬇️",
                callback_data=f"{NavAdminTools.PAYMENT_MOVE_DOWN.value}:{gateway.callback}",
            ),
        )
    builder.row(back_button(NavAdminTools.PLAN_EDITOR))
    return builder.as_markup()


def statistics_keyboard(period_code: str = "7d") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=("• " if period_code == "today" else "") + _("statistics:button:today"),
            callback_data=NavAdminTools.STATISTICS_PERIOD + "_today",
        ),
        InlineKeyboardButton(
            text=("• " if period_code == "7d" else "") + _("statistics:button:7d"),
            callback_data=NavAdminTools.STATISTICS_PERIOD + "_7d",
        ),
        InlineKeyboardButton(
            text=("• " if period_code == "30d" else "") + _("statistics:button:30d"),
            callback_data=NavAdminTools.STATISTICS_PERIOD + "_30d",
        ),
        InlineKeyboardButton(
            text=("• " if period_code == "all" else "") + _("statistics:button:all"),
            callback_data=NavAdminTools.STATISTICS_PERIOD + "_all",
        ),
    )

    builder.row(
        InlineKeyboardButton(
            text=_("statistics:button:refresh"),
            callback_data=NavAdminTools.STATISTICS_PERIOD + f"_{period_code}",
        )
    )

    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def health_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="Обновить",
            callback_data=NavAdminTools.HEALTH,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Проверить узлы",
            callback_data=NavAdminTools.HEALTH_CHECK_NODES,
        )
    )
    builder.row(back_button(NavAdminTools.MAIN))
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def confirm_delete_invite_keyboard(invite_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("invite_editor:button:confirm_delete"),
            callback_data=NavAdminTools.DELETE_INVITE + f"_{invite_id}",
        ),
    )
    builder.row(cancel_button(NavAdminTools.SHOW_INVITE_DETAILS + f"_{invite_id}"))
    return builder.as_markup()
