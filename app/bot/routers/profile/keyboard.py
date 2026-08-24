from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.routers.misc.keyboard import back_to_main_menu_button
from app.bot.utils.navigation import NavDownload, NavProfile, NavSubscription
from app.db.models import Server


class ProfileServerData(CallbackData, prefix="profile_server"):
    server_id: int


def format_server_label(server: Server | None) -> str:
    if not server:
        return _("profile:message:server_unknown")

    labels = {
        "FI": _("profile:server:finland"),
        "FINLAND": _("profile:server:finland"),
        "KZ": _("profile:server:kazakhstan"),
        "KAZAKHSTAN": _("profile:server:kazakhstan"),
    }
    location = (server.location or "").upper()
    return labels.get(location) or server.location or server.name


def buy_subscription_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("profile:button:buy_subscription"),
            callback_data=NavSubscription.MAIN,
        )
    )
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def profile_keyboard(
    *,
    show_additional_profile_key: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("profile:button:show_key"),
            callback_data=NavProfile.SHOW_KEY,
        )
    )
    if show_additional_profile_key:
        builder.row(
            InlineKeyboardButton(
                text=_("profile:button:show_additional_key"),
                callback_data=NavProfile.SHOW_ADDITIONAL_KEY,
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=_("profile:button:connect"),
            callback_data=NavDownload.MAIN,
        )
    )
    builder.row(back_to_main_menu_button())
    return builder.as_markup()


def server_selection_keyboard(
    servers: list[Server],
    current_server_id: int | None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for server in servers:
        prefix = "✅ " if server.id == current_server_id else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{prefix}{format_server_label(server)}",
                callback_data=ProfileServerData(server_id=server.id).pack(),
            )
        )

    builder.row(
        InlineKeyboardButton(
            text=_("profile:button:back_to_profile"),
            callback_data=NavProfile.MAIN,
        )
    )
    builder.row(back_to_main_menu_button())
    return builder.as_markup()
