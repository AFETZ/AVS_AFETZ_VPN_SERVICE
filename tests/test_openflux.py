import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.bot.routers.main_menu import keyboard as menu
from app.bot.routers.openflux import handler
from app.bot.utils.navigation import NavAdminTools, NavOpenFlux
from app.config import OpenFluxConfig


@pytest.fixture(autouse=True)
def i18n(monkeypatch):
    from app.bot.routers.misc import keyboard
    monkeypatch.setattr(keyboard, "_", lambda text: text)


def config(*, enabled=True, url="", allowed_user_ids=()):
    return SimpleNamespace(
        openflux=OpenFluxConfig(ENABLED=enabled, DOC_URL=url, ALLOWED_USER_IDS=list(allowed_user_ids)),
        bot=SimpleNamespace(ADMINS=[10], DEV_ID=20, DOMAIN="https://bot.example", SUBSCRIPTION_DOMAIN="https://link.example"),
    )


def callback(data=NavOpenFlux.MAIN, *, user_id=10, chat_type="private", chat_id=10):
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user_id), answer=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            edit_text=AsyncMock(), answer_document=AsyncMock(),
        ),
    )


@pytest.mark.parametrize("admin,enabled,visible", [(False,False,False),(False,True,True),(True,False,False),(True,True,True)])
def test_pilot_button_uses_user_access_without_granting_admin(monkeypatch, admin, enabled, visible):
    monkeypatch.setattr(menu, "_", lambda text: text)
    markup = menu.main_menu_keyboard(is_admin=admin, openflux_enabled=enabled)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.callback_data == NavOpenFlux.MAIN for b in buttons) is visible
    assert any(b.callback_data == NavAdminTools.MAIN for b in buttons) is admin
    # Existing menu callbacks are unchanged when enabling the experiment.
    without = menu.main_menu_keyboard(is_admin=admin)
    assert [b for b in buttons if b.callback_data != NavOpenFlux.MAIN] == [b for row in without.inline_keyboard for b in row]


@pytest.mark.parametrize("enabled,user_id,allowed,expected", [
    (True, 10, (), True),
    (True, 20, (), True),
    (True, 408966771, (408966771,), True),
    (True, 30, (408966771,), False),
    (True, 408966771, (), False),
    (False, 10, (408966771,), False),
    (False, 20, (408966771,), False),
    (False, 408966771, (408966771,), False),
])
def test_pilot_access_scopes_and_global_disable(enabled, user_id, allowed, expected):
    assert handler.pilot_available(config(enabled=enabled, allowed_user_ids=allowed), user_id) is expected


@pytest.mark.parametrize("data", list(NavOpenFlux))
async def test_invited_user_can_use_every_callback_and_revocation_blocks_it(data, monkeypatch, tmp_path):
    monkeypatch.setattr(handler, "ARTIFACT_DIR", tmp_path)
    for filename in handler.ARTIFACTS.values():
        (tmp_path / filename).write_bytes(b"test artifact")
    settings = config(url="https://disk.yandex.ru/i/test", allowed_user_ids=(408966771,))
    query = callback(data, user_id=408966771, chat_id=408966771)
    await handler.callback_openflux(query, settings)
    query.answer.assert_awaited_once_with()
    assert query.message.edit_text.await_count + query.message.answer_document.await_count == 1

    settings.openflux.ALLOWED_USER_IDS.clear()
    stale_query = callback(data, user_id=408966771, chat_id=408966771)
    await handler.callback_openflux(stale_query, settings)
    assert stale_query.answer.call_args.kwargs["show_alert"] is True
    stale_query.message.edit_text.assert_not_awaited()
    stale_query.message.answer_document.assert_not_awaited()


@pytest.mark.parametrize("data", list(NavOpenFlux))
@pytest.mark.parametrize("enabled,chat_type,chat_id", [
    (False, "private", 408966771),
    (True, "group", 408966771),
    (True, "private", 99),
])
async def test_invited_user_still_requires_enabled_pilot_and_own_private_chat(data, enabled, chat_type, chat_id):
    query = callback(data, user_id=408966771, chat_type=chat_type, chat_id=chat_id)
    await handler.callback_openflux(query, config(enabled=enabled, allowed_user_ids=(408966771,)))
    query.answer.assert_awaited_once()
    query.message.edit_text.assert_not_awaited()
    query.message.answer_document.assert_not_awaited()


@pytest.mark.parametrize("url", ["", "http://disk.yandex.ru/i/a", "https://evil.test/x", "https://disk.yandex.ru.evil.test/x", "https://a@disk.yandex.ru/i/x", "https://disk.yandex.ru:22/i/x", "https://disk.yandex.ru/i/x\n--debug", "https://[bad/x"])
def test_invalid_document_urls_are_rejected(url):
    assert not handler.valid_doc_url(url)
    with pytest.raises(ValueError):
        handler.client_command(url, windows=False)


def test_commands_quote_document_url_and_bind_only_loopback():
    url = "https://disk.yandex.ru/i/example?a='$(echo)';x=&b=1"
    args = shlex.split(handler.client_command(url, windows=False))
    assert args[args.index("--url") + 1] == url
    assert args[-2:] == ["--socks5", "127.0.0.1:1080"]
    windows = handler.client_command(url, windows=True)
    assert "a=''$(echo)'';x=&b=1'" in windows
    assert windows.startswith(".\\openflux.exe --client")
    assert url not in repr(OpenFluxConfig(DOC_URL=url))


@pytest.mark.parametrize("data", list(NavOpenFlux))
@pytest.mark.parametrize("enabled,user_id,chat_type,chat_id", [(False,10,"private",10),(True,30,"private",30),(True,10,"group",10),(True,10,"private",99)])
async def test_every_callback_rechecks_access(data, enabled, user_id, chat_type, chat_id):
    query = callback(data, user_id=user_id, chat_type=chat_type, chat_id=chat_id)
    await handler.callback_openflux(query, config(enabled=enabled, url="https://disk.yandex.ru/i/secret"))
    query.answer.assert_awaited_once()
    query.message.edit_text.assert_not_awaited()
    query.message.answer_document.assert_not_awaited()


async def test_unconfigured_pilot_does_not_claim_connection():
    query = callback()
    await handler.callback_openflux(query, config())
    assert "Подготовка:" in query.message.edit_text.call_args.args[0]


async def test_windows_instruction_contains_command_without_vpn_services(monkeypatch):
    # Keep a real markup button while bypassing the i18n context.
    from aiogram.types import InlineKeyboardButton
    monkeypatch.setattr(handler, "back_button", lambda x: InlineKeyboardButton(text="Back", callback_data=x))
    query = callback(NavOpenFlux.WINDOWS)
    await handler.callback_openflux(query, config(url="https://disk.yandex.ru/i/test?a=1&b=2"))
    text = query.message.edit_text.call_args.args[0]
    assert "--client" in text and "127.0.0.1:1080" in text
    assert "a=1&amp;b=2" in text


async def test_download_handles_missing_and_existing_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(handler, "ARTIFACT_DIR", tmp_path)
    query = callback(NavOpenFlux.DOWNLOAD_LINUX)
    await handler.callback_openflux(query, config())
    query.message.answer_document.assert_not_awaited()
    artifact = tmp_path / handler.ARTIFACTS[NavOpenFlux.DOWNLOAD_LINUX]
    artifact.write_bytes(b"test archive")
    query = callback(NavOpenFlux.DOWNLOAD_LINUX)
    await handler.callback_openflux(query, config())
    query.message.answer_document.assert_awaited_once()
    assert query.message.answer_document.call_args.args[0].path == artifact


async def test_mobile_profile_issued_only_when_configured():
    query = callback(NavOpenFlux.DOWNLOAD_PROFILE)
    await handler.callback_openflux(query, config())
    query.message.answer_document.assert_not_awaited()
    query = callback(NavOpenFlux.DOWNLOAD_PROFILE)
    url = "https://disk.yandex.ru/i/private-test"
    await handler.callback_openflux(query, config(url=url))
    sent = query.message.answer_document.call_args
    assert sent.kwargs["protect_content"] is True
    assert sent.args[0].filename == "afz-openflux.json"
    assert json.loads(sent.args[0].data) == {"version": 1, "document_url": url}


async def test_android_download_and_ios_signing_status(monkeypatch, tmp_path):
    from aiogram.types import InlineKeyboardButton
    monkeypatch.setattr(handler, "back_button", lambda x: InlineKeyboardButton(text="Back", callback_data=x))
    monkeypatch.setattr(handler, "ARTIFACT_DIR", tmp_path)
    (tmp_path / handler.ARTIFACTS[NavOpenFlux.DOWNLOAD_ANDROID]).write_bytes(b"apk")
    android = callback(NavOpenFlux.ANDROID)
    await handler.callback_openflux(android, config(url="https://disk.yandex.ru/i/test"))
    kwargs = android.message.edit_text.call_args.kwargs
    actions = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert NavOpenFlux.DOWNLOAD_ANDROID in actions and NavOpenFlux.DOWNLOAD_PROFILE in actions
    assert "127.0.0.1:1080" not in android.message.edit_text.call_args.args[0]
    ios = callback(NavOpenFlux.IOS)
    await handler.callback_openflux(ios, config())
    assert "Готовой подписанной версии для установки пока нет" in ios.message.edit_text.call_args.args[0]
    download = callback(NavOpenFlux.DOWNLOAD_ANDROID)
    await handler.callback_openflux(download, config())
    assert "APK" in download.message.answer_document.call_args.kwargs["caption"]
    assert "Распакуйте" not in download.message.answer_document.call_args.kwargs["caption"]


@pytest.mark.parametrize("platform", [NavOpenFlux.ANDROID, NavOpenFlux.IOS])
async def test_mobile_button_passes_exact_profile_without_server_query(platform):
    url = "https://disk.yandex.ru/i/test?a=1&b=%2B+#part"
    query = callback(platform)
    await handler.callback_openflux(query, config(url=url))
    buttons = [b for row in query.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
    link = next(b for b in buttons if b.text == "Добавить в AFZ OpenFlux")
    parsed = urlsplit(link.url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "link.example", "/connection")
    assert parse_qs(parsed.query) == {"client": ["openflux"]}
    assert parse_qs(parsed.fragment) == {"version": ["1"], "document_url": [url]}
    assert next(b for b in buttons if b.copy_text).copy_text.text == url


async def test_apk_and_profile_messages_include_direct_import_button(monkeypatch, tmp_path):
    monkeypatch.setattr(handler, "ARTIFACT_DIR", tmp_path)
    (tmp_path / handler.ARTIFACTS[NavOpenFlux.DOWNLOAD_ANDROID]).write_bytes(b"apk")
    for action in (NavOpenFlux.DOWNLOAD_ANDROID, NavOpenFlux.DOWNLOAD_PROFILE):
        query = callback(action)
        await handler.callback_openflux(query, config(url="https://disk.yandex.ru/i/test"))
        sent = query.message.answer_document.call_args.kwargs
        assert sent["protect_content"] is True
        assert any(b.text == "Добавить в AFZ OpenFlux" for row in sent["reply_markup"].inline_keyboard for b in row)


def test_no_import_button_without_valid_profile_and_copy_limit():
    assert handler.mobile_import_markup(config()) is None
    assert handler.mobile_import_buttons(config(url="https://evil.test/x")) == []
    buttons = handler.mobile_import_buttons(config(url="https://disk.yandex.ru/i/" + "x" * 260))
    assert len(buttons) == 1 and buttons[0][0].url
