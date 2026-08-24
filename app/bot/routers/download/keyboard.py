import base64
import json
from urllib.parse import urlencode

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.routers.misc.keyboard import back_button, back_to_main_menu_button
from app.bot.utils.constants import (
    APPLE_ACCOUNT_REGION_HELP_LINK,
    APP_ANDROID_LINK,
    APP_ANDROID_SCHEME,
    APP_HAPP_ROUTING_SCHEME,
    APP_IOS_LINK,
    APP_IOS_SCHEME,
    APP_WINDOWS_LINK,
    APP_WINDOWS_SCHEME,
    CONNECTION_WEBHOOK,
)
from app.bot.utils.navigation import NavDownload, NavMain, NavSubscription


def _unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def build_connect_url(url: str, scheme: str, key: str, platform_param: str) -> str:
    connect_params = urlencode({"scheme": scheme, "key": key, "platform": platform_param})
    return f"{url}{CONNECTION_WEBHOOK}?{connect_params}"


def build_happ_routing_base64(geo_base_url: str) -> str:
    geo_base_url = geo_base_url.rstrip("/")
    if not geo_base_url:
        raise ValueError("geo_base_url is required for the Happ routing profile")
    profile = {
        "Name": "AFETZ РФ-сервисы напрямую",
        "GlobalProxy": "true",
        "RemoteDNSType": "DoH",
        "RemoteDNSDomain": "https://cloudflare-dns.com/dns-query",
        "RemoteDNSIP": "1.1.1.1",
        "DomesticDNSType": "DoH",
        "DomesticDNSDomain": "https://dns.google/dns-query",
        "DomesticDNSIP": "8.8.8.8",
        "Geoipurl": f"{geo_base_url}/geoip.dat",
        "Geositeurl": f"{geo_base_url}/geosite.dat",
        "LastUpdated": "",
        "DnsHosts": {},
        "DirectSites": _unique_nonempty(["geosite:CATEGORY-RU"]),
        "DirectIp": _unique_nonempty(
            [
                "geoip:RU",
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "169.254.0.0/16",
                "224.0.0.0/4",
                "255.255.255.255",
            ]
        ),
        "DomainStrategy": "IPIfNonMatch",
        "FakeDNS": "false",
    }
    raw = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def build_happ_routing_connection_url(
    url: str,
    platform_param: str,
    *,
    geo_base_url: str | None = None,
) -> str:
    resolved_geo_base_url = geo_base_url or url
    return build_connect_url(
        url=url,
        scheme=APP_HAPP_ROUTING_SCHEME,
        key=build_happ_routing_base64(resolved_geo_base_url),
        platform_param=platform_param,
    )


def platforms_keyboard(previous_callback: str = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=_("download:button:ios"),
            callback_data=NavDownload.PLATFORM_IOS,
        ),
        InlineKeyboardButton(
            text=_("download:button:android"),
            callback_data=NavDownload.PLATFORM_ANDROID,
        ),
        InlineKeyboardButton(
            text=_("download:button:windows"),
            callback_data=NavDownload.PLATFORM_WINDOWS,
        ),
    )

    if previous_callback == NavMain.MAIN_MENU:
        builder.row(back_to_main_menu_button())
    else:
        back_callback = previous_callback if previous_callback else NavMain.MAIN_MENU
        builder.row(back_button(back_callback))

    return builder.as_markup()


def download_keyboard(
    platform: NavDownload,
    url: str,
    key: str | None,
    backup_key: str | None = None,
    additional_profile_key: str | None = None,
    filtered_additional_profile_key: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    match platform:
        case NavDownload.PLATFORM_IOS:
            scheme = APP_IOS_SCHEME
            download = APP_IOS_LINK
            download_text = _("download:button:download_ios_global")
            show_apple_region_help = True
            platform_param = "ios"
        case NavDownload.PLATFORM_ANDROID:
            scheme = APP_ANDROID_SCHEME
            download = APP_ANDROID_LINK
            download_text = _("download:button:download")
            show_apple_region_help = False
            platform_param = "android"
        case _:
            scheme = APP_WINDOWS_SCHEME
            download = APP_WINDOWS_LINK
            download_text = _("download:button:download")
            show_apple_region_help = False
            platform_param = "windows"

    connect = (
        build_connect_url(url=url, scheme=scheme, key=key, platform_param=platform_param)
        if key
        else None
    )
    backup_connect = (
        build_connect_url(
            url=url,
            scheme=scheme,
            key=backup_key,
            platform_param=platform_param,
        )
        if backup_key
        else None
    )
    routing_connect = (
        build_happ_routing_connection_url(url=url, platform_param=platform_param)
        if key
        else None
    )
    additional_connect = (
        build_connect_url(
            url=url,
            scheme=scheme,
            key=additional_profile_key,
            platform_param=platform_param,
        )
        if additional_profile_key
        else None
    )
    filtered_additional_connect = (
        build_connect_url(
            url=url,
            scheme=scheme,
            key=filtered_additional_profile_key,
            platform_param=platform_param,
        )
        if filtered_additional_profile_key
        else None
    )

    builder.row(
        InlineKeyboardButton(
            text=_("download:button:connect_primary_profile"),
            url=connect if key else None,
            callback_data=NavSubscription.MAIN if not key else None,
        )
    )
    if backup_connect:
        builder.row(
            InlineKeyboardButton(
                text=_("download:button:connect_primary_profile_backup"),
                url=backup_connect,
            )
        )
    if routing_connect:
        builder.row(
            InlineKeyboardButton(
                text=_("download:button:setup_ru_direct"),
                url=routing_connect,
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=_("download:button:connect_filtered_additional_profile"),
            url=filtered_additional_connect if filtered_additional_profile_key else None,
            callback_data=NavSubscription.ADDITIONAL_PROFILE
            if not filtered_additional_profile_key
            else None,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=_("download:button:connect_additional_profile"),
            url=additional_connect if additional_profile_key else None,
            callback_data=NavSubscription.ADDITIONAL_PROFILE if not additional_profile_key else None,
        )
    )
    if show_apple_region_help:
        builder.row(
            InlineKeyboardButton(
                text=_("download:button:change_apple_region"),
                url=APPLE_ACCOUNT_REGION_HELP_LINK,
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=download_text,
            url=download,
        )
    )

    builder.row(back_button(NavDownload.MAIN))
    return builder.as_markup()
