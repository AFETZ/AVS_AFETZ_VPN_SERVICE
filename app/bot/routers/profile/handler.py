import asyncio
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.i18n import gettext as _

from app.bot.models import ClientData
from app.bot.services import ServicesContainer
from app.bot.utils.constants import PREVIOUS_CALLBACK_KEY
from app.bot.utils.navigation import NavProfile
from app.db.models import User

from .keyboard import (
    ProfileServerData,
    buy_subscription_keyboard,
    format_server_label,
    profile_keyboard,
    server_selection_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name=__name__)


async def prepare_message(user: User, client_data: ClientData | None) -> str:
    profile = _("profile:message:main").format(name=user.first_name, id=user.tg_id)

    if not client_data:
        subscription = _("profile:message:subscription_none")
        return profile + subscription

    subscription = _("profile:message:subscription").format(devices=client_data.max_devices)

    subscription += (
        _("profile:message:subscription_expiry_time").format(expiry_time=client_data.expiry_time)
        if not client_data.has_subscription_expired
        else _("profile:message:subscription_expired")
    )

    statistics = _("profile:message:statistics").format(
        total=client_data.traffic_used,
        up=client_data.traffic_up,
        down=client_data.traffic_down,
    )

    return profile + subscription + statistics


async def show_temporary_key(callback: CallbackQuery, key: str) -> None:
    key_text = _("profile:message:key")
    message = await callback.message.answer(key_text.format(key=key, seconds_text=_("10 seconds")))

    for seconds in range(9, 0, -1):
        seconds_text = _("1 second", "{} seconds", seconds).format(seconds)
        await asyncio.sleep(1)
        await message.edit_text(text=key_text.format(key=key, seconds_text=seconds_text))
    await message.delete()


@router.callback_query(F.data == NavProfile.MAIN)
async def callback_profile(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
    state: FSMContext,
) -> None:
    logger.info(f"User {user.tg_id} opened profile page.")
    await state.update_data({PREVIOUS_CALLBACK_KEY: NavProfile.MAIN})

    status = await services.subscription.get_subscription_status(user)
    client_data = status.client_data
    has_additional_profile = bool(status.status_check_ok and status.has_additional_profile)

    reply_markup = (
        profile_keyboard(
            show_additional_profile_key=has_additional_profile,
        )
        if client_data and not client_data.has_subscription_expired
        else buy_subscription_keyboard()
    )
    await callback.message.edit_text(
        text=await prepare_message(user=user, client_data=client_data),
        reply_markup=reply_markup,
    )


@router.callback_query(F.data == NavProfile.SHOW_KEY)
async def callback_show_key(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
) -> None:
    logger.info(f"User {user.tg_id} looked key.")
    key = await services.vpn.get_key(user)
    await show_temporary_key(callback=callback, key=key)


@router.callback_query(F.data == NavProfile.SELECT_SERVER)
async def callback_select_server(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
) -> None:
    logger.info("User %s opened server selection.", user.tg_id)

    if not user.server_id:
        await callback.answer(_("profile:popup:no_subscription_for_server_select"), show_alert=True)
        return

    servers = await services.server_pool.get_selectable_servers()
    if not servers:
        await callback.answer(_("profile:popup:no_servers_available"), show_alert=True)
        return

    current_server = next((server for server in servers if server.id == user.server_id), user.server)
    await callback.message.edit_text(
        text=_("profile:message:server_selection").format(
            server=format_server_label(current_server),
        ),
        reply_markup=server_selection_keyboard(
            servers=servers,
            current_server_id=user.server_id,
        ),
    )


@router.callback_query(ProfileServerData.filter())
async def callback_server_selected(
    callback: CallbackQuery,
    user: User,
    callback_data: ProfileServerData,
    services: ServicesContainer,
) -> None:
    logger.info(
        "User %s selected server %s.",
        user.tg_id,
        callback_data.server_id,
    )

    result = await services.vpn.switch_server(
        user=user,
        server_id=callback_data.server_id,
    )

    if not result.success:
        message_by_reason = {
            "already_selected": _("profile:popup:server_already_selected"),
            "client_missing": _("profile:popup:server_client_missing"),
            "unavailable": _("profile:popup:server_unavailable"),
        }
        await callback.answer(
            message_by_reason.get(
                result.reason,
                _("profile:popup:server_unavailable"),
            ),
            show_alert=True,
        )
        return

    await callback.answer(
        _("profile:popup:server_switched").format(
            server=format_server_label(result.server),
        ),
        show_alert=True,
    )

    status = await services.subscription.get_subscription_status(user)
    await callback.message.edit_text(
        text=await prepare_message(user=user, client_data=status.client_data),
        reply_markup=profile_keyboard(
            show_additional_profile_key=bool(
                status.status_check_ok and status.has_additional_profile
            ),
        ),
    )


@router.callback_query(F.data == NavProfile.SHOW_ADDITIONAL_KEY)
async def callback_show_additional_key(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
) -> None:
    logger.info("User %s looked whitelist bypass key.", user.tg_id)
    if not await services.subscription.has_additional_profile_access(user):
        await callback.answer(_("profile:popup:additional_key_unavailable"), show_alert=True)
        return

    key = services.subscription.get_additional_profile_url(user)
    await show_temporary_key(callback=callback, key=key)
