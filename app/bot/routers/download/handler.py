import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.i18n import gettext as _
from aiohttp.web import HTTPFound, Request, Response

from app.bot.models import ServicesContainer
from app.bot.utils.constants import (
    APP_ANDROID_SCHEME,
    APP_HAPP_ROUTING_SCHEME,
    APP_IOS_SCHEME,
    APP_WINDOWS_SCHEME,
    MAIN_MESSAGE_ID_KEY,
    PREVIOUS_CALLBACK_KEY,
)
from app.bot.utils.public_links import subscription_base_url
from app.bot.utils.navigation import NavDownload, NavMain
from app.bot.utils.network import parse_redirect_url
from app.config import Config
from app.db.models import User
from app.web.openflux_import import import_page

from .keyboard import download_keyboard, platforms_keyboard

logger = logging.getLogger(__name__)
router = Router(name=__name__)


async def redirect_to_connection(request: Request) -> Response:
    if request.query.get("client") == "openflux":
        return import_page()
    query_string = request.query_string

    if not query_string:
        return Response(status=400, reason="Missing query string.")

    params = parse_redirect_url(query_string)
    scheme = params.get("scheme")
    key = params.get("key")

    if not scheme or not key:
        raise Response(status=400, reason="Invalid parameters.")

    redirect_url = f"{scheme}{key}"
    if scheme in {
        APP_IOS_SCHEME,
        APP_ANDROID_SCHEME,
        APP_WINDOWS_SCHEME,
        APP_HAPP_ROUTING_SCHEME,
    }:
        raise HTTPFound(redirect_url)

    return Response(status=400, reason="Unsupported application.")


@router.callback_query(F.data == NavDownload.MAIN)
async def callback_download(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    logger.info(f"User {user.tg_id} opened download apps page.")

    previous_callback = await state.get_value(PREVIOUS_CALLBACK_KEY)
    if callback.data == NavMain.REDIRECT_TO_DOWNLOAD or not previous_callback:
        previous_callback = NavMain.MAIN_MENU

    message = callback.message
    if message is None:
        await callback.answer()
        return

    await state.update_data({
        MAIN_MESSAGE_ID_KEY: message.message_id,
        PREVIOUS_CALLBACK_KEY: previous_callback,
    })

    try:
        await message.edit_text(
            text=_("download:message:choose_platform"),
            reply_markup=platforms_keyboard(previous_callback),
        )
    except Exception as exception:
        logger.warning(
            "Failed to edit download message for user %s: %s. Sending a new one.",
            user.tg_id,
            exception,
        )
        sent_message = await callback.bot.send_message(
            chat_id=user.tg_id,
            text=_("download:message:choose_platform"),
            reply_markup=platforms_keyboard(previous_callback),
        )
        await state.update_data({MAIN_MESSAGE_ID_KEY: sent_message.message_id})


async def _show_platform_download(
    *,
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
    config: Config,
    platform_nav: NavDownload | str,
) -> None:
    logger.info("User %s selected platform: %s", user.tg_id, platform_nav)
    key = await services.vpn.get_key(user)
    get_backup_key = getattr(services.vpn, "get_backup_key", None)
    backup_key = await get_backup_key(user) if get_backup_key else None
    status = await services.subscription.get_subscription_status(user)
    additional_profile_key = (
        services.subscription.get_additional_profile_url(user)
        if status.status_check_ok and status.has_additional_profile
        else None
    )
    filtered_additional_profile_key = (
        services.subscription.get_filtered_additional_profile_url(user)
        if status.status_check_ok and status.has_additional_profile
        else None
    )

    match platform_nav:
        case NavDownload.PLATFORM_IOS:
            platform = _("download:message:platform_ios")
            app_store_notice = "\n\n" + _("download:message:ios_app_store_notice")
        case NavDownload.PLATFORM_ANDROID:
            platform = _("download:message:platform_android")
            app_store_notice = ""
        case _:
            platform = _("download:message:platform_windows")
            app_store_notice = ""

    await callback.message.edit_text(
        text=_("download:message:connect_to_vpn").format(platform=platform)
        + app_store_notice,
        reply_markup=download_keyboard(
            platform=platform_nav,
            key=key,
            backup_key=backup_key,
            url=subscription_base_url(config) or config.bot.DOMAIN,
            additional_profile_key=additional_profile_key,
            filtered_additional_profile_key=filtered_additional_profile_key,
        ),
    )


@router.callback_query(F.data.startswith(NavDownload.PLATFORM))
async def callback_platform(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
    config: Config,
) -> None:
    await _show_platform_download(
        callback=callback,
        user=user,
        services=services,
        config=config,
        platform_nav=callback.data,
    )


@router.callback_query(F.data.startswith(NavDownload.CLIENT))
async def callback_android_client(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
    config: Config,
) -> None:
    logger.info("User %s clicked legacy Android client button: %s", user.tg_id, callback.data)
    await _show_platform_download(
        callback=callback,
        user=user,
        services=services,
        config=config,
        platform_nav=NavDownload.PLATFORM_ANDROID,
    )
