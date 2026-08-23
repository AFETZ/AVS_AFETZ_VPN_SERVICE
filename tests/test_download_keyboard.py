import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.bot.routers.download.keyboard import (
    build_connect_url,
    build_happ_routing_base64,
    build_happ_routing_connection_url,
    download_keyboard,
)
from app.bot.utils.constants import APP_IOS_LINK, APPLE_ACCOUNT_REGION_HELP_LINK
from app.bot.utils.navigation import NavDownload, NavSubscription


def fake_gettext(message, plural=None, n=1):
    if plural is None:
        return message
    return message if n == 1 else plural


@pytest.fixture(autouse=True)
def patch_i18n(monkeypatch):
    monkeypatch.setattr("app.bot.routers.misc.keyboard._", fake_gettext)
    monkeypatch.setattr("app.bot.routers.download.keyboard._", fake_gettext)


def flatten_keyboard_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_build_connect_url_uses_happ_scheme_key_and_platform() -> None:
    url = build_connect_url(
        url="https://example.test",
        scheme="happ://add/",
        key="https://example.test/sub/user-id",
        platform_param="android",
    )

    parsed = urlsplit(url)
    params = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://example.test/connection"
    )
    assert params["scheme"] == ["happ://add/"]
    assert params["key"] == ["https://example.test/sub/user-id"]
    assert params["platform"] == ["android"]


def test_build_happ_routing_base64_keeps_ru_direct_and_rest_vpn() -> None:
    profile = json.loads(
        base64.b64decode(
            build_happ_routing_base64("https://example.test")
        ).decode("utf-8")
    )

    assert profile["Name"] == "AFETZ РФ-сервисы напрямую"
    assert profile["GlobalProxy"] == "true"
    assert profile["DirectSites"] == ["geosite:CATEGORY-RU"]
    assert "geoip:RU" in profile["DirectIp"]
    assert profile["DomainStrategy"] == "IPIfNonMatch"


def test_build_happ_routing_base64_rejects_missing_local_geo_domain() -> None:
    with pytest.raises(ValueError, match="geo_base_url"):
        build_happ_routing_base64("")


def test_build_happ_routing_connection_url_uses_routing_scheme() -> None:
    url = build_happ_routing_connection_url(
        url="https://example.test",
        platform_param="android",
    )

    parsed = urlsplit(url)
    params = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://example.test/connection"
    )
    assert params["scheme"] == ["happ://routing/onadd/"]
    assert params["platform"] == ["android"]
    routing = json.loads(base64.b64decode(params["key"][0]).decode("utf-8"))
    assert routing["DirectSites"] == ["geosite:CATEGORY-RU"]
    assert routing["Geoipurl"] == "https://example.test/geoip.dat"
    assert routing["Geositeurl"] == "https://example.test/geosite.dat"


def test_download_keyboard_shows_filtered_additional_profile_button() -> None:
    markup = download_keyboard(
        platform=NavDownload.PLATFORM_IOS,
        url="https://example.test",
        key="https://example.test/sub/user-id",
        additional_profile_key="https://example.test/wl/user-id",
        filtered_additional_profile_key="https://example.test/wl-filtered/user-id",
    )
    buttons = flatten_keyboard_buttons(markup)
    texts = [button.text for button in buttons]

    assert "download:button:connect_additional_profile" in texts
    assert "download:button:connect_filtered_additional_profile" in texts
    assert texts.index("download:button:connect_filtered_additional_profile") < texts.index(
        "download:button:connect_additional_profile"
    )

    filtered_button = next(
        button
        for button in buttons
        if button.text == "download:button:connect_filtered_additional_profile"
    )
    params = parse_qs(urlsplit(filtered_button.url).query)

    assert params["key"] == ["https://example.test/wl-filtered/user-id"]


def test_download_keyboard_shows_backup_subscription_on_primary_connection_domain() -> None:
    markup = download_keyboard(
        platform=NavDownload.PLATFORM_WINDOWS,
        url="https://link.example",
        key="https://link.example/sub/user-id",
        backup_key="https://backup.example/sub/user-id",
    )
    backup_button = next(
        button
        for button in flatten_keyboard_buttons(markup)
        if button.text == "download:button:connect_primary_profile_backup"
    )
    parsed = urlsplit(backup_button.url)
    params = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://link.example/connection"
    )
    assert params["key"] == ["https://backup.example/sub/user-id"]


def test_ios_download_button_uses_global_app_store_listing() -> None:
    markup = download_keyboard(
        platform=NavDownload.PLATFORM_IOS,
        url="https://example.test",
        key="https://example.test/sub/user-id",
    )
    download_button = next(
        button
        for button in flatten_keyboard_buttons(markup)
        if button.text == "download:button:download_ios_global"
    )
    region_help_button = next(
        button
        for button in flatten_keyboard_buttons(markup)
        if button.text == "download:button:change_apple_region"
    )

    assert APP_IOS_LINK == (
        "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215?l=ru"
    )
    assert download_button.url == APP_IOS_LINK
    assert region_help_button.url == APPLE_ACCOUNT_REGION_HELP_LINK


def test_android_keyboard_matches_happ_only_flow_without_raw_or_client_choice() -> None:
    markup = download_keyboard(
        platform=NavDownload.PLATFORM_ANDROID,
        url="https://example.test",
        key="https://example.test/sub/user-id",
        additional_profile_key="https://example.test/wl/user-id",
        filtered_additional_profile_key="https://example.test/wl-filtered/user-id",
    )
    texts = [button.text for button in flatten_keyboard_buttons(markup)]

    assert "download:button:connect_primary_profile" in texts
    assert "download:button:setup_ru_direct" in texts
    assert "download:button:connect_filtered_additional_profile" in texts
    assert "download:button:connect_additional_profile" in texts
    assert "Выдать raw-конфигурацию" not in texts
    assert "Happ" not in texts
    assert "V2RayNG" not in texts

    filtered_button = next(
        button
        for button in flatten_keyboard_buttons(markup)
        if button.text == "download:button:connect_filtered_additional_profile"
    )
    params = parse_qs(urlsplit(filtered_button.url).query)

    assert params["scheme"] == ["happ://add/"]
    assert params["platform"] == ["android"]
    assert params["key"] == ["https://example.test/wl-filtered/user-id"]


def test_download_keyboard_keeps_bypass_entrypoints_without_entitlement_url() -> None:
    markup = download_keyboard(
        platform=NavDownload.PLATFORM_ANDROID,
        url="https://example.test",
        key="https://example.test/sub/user-id",
    )
    buttons = flatten_keyboard_buttons(markup)
    texts = [button.text for button in buttons]

    assert "download:button:connect_additional_profile" in texts
    assert "download:button:connect_filtered_additional_profile" in texts

    filtered_button = next(
        button
        for button in buttons
        if button.text == "download:button:connect_filtered_additional_profile"
    )
    assert filtered_button.url is None
    assert filtered_button.callback_data == NavSubscription.ADDITIONAL_PROFILE
