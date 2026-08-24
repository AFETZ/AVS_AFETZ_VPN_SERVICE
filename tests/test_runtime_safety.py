import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe

import app.__main__ as runtime
from app.__main__ import _redact_proxy_url, resolve_telegram_proxy_url
from app.config import normalize_bot_domain
from app.logger import ArchiveRotatingFileHandler


def test_normalize_bot_domain_accepts_plain_host_and_full_url():
    assert normalize_bot_domain("example.com") == "https://example.com"
    assert normalize_bot_domain("https://example.com/") == "https://example.com"
    assert normalize_bot_domain("http://example.com") == "http://example.com"


class FakeSession:
    def __init__(self, *, error=None):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _):
        if self.error:
            raise self.error
        return None


class FakeSessionFactory:
    def __init__(self, *, error=None):
        self.error = error

    def __call__(self):
        return FakeSession(error=self.error)


class FakeRedis:
    def __init__(self, *, error=None):
        self.error = error

    async def ping(self):
        if self.error:
            raise self.error
        return True


def _readiness_services(*, circuit_open=False):
    return SimpleNamespace(
        server_pool=SimpleNamespace(_servers={1: object(), 2: object()}),
        vpn=SimpleNamespace(
            xui_gateway=SimpleNamespace(
                get_health_snapshot=lambda: [
                    {"server_id": 1, "circuit_open": circuit_open}
                ]
            )
        ),
    )


def test_internal_request_detection_rejects_reverse_proxy_clients():
    assert runtime._is_internal_request(SimpleNamespace(remote="127.0.0.1")) is True
    assert runtime._is_internal_request(SimpleNamespace(remote="172.22.0.1")) is False


async def test_readiness_handler_returns_ok_when_dependencies_answer():
    handler = runtime.make_readiness_handler(
        db=SimpleNamespace(session=FakeSessionFactory()),
        redis=FakeRedis(),
        services=_readiness_services(circuit_open=True),
    )

    response = await handler(None)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "ok"
    assert payload["checks"]["database"]["ok"] is True
    assert payload["checks"]["redis"]["ok"] is True
    assert payload["runtime"]["servers_in_pool"] == 2
    assert payload["runtime"]["xui_circuits_open"] == 1


async def test_readiness_handler_returns_503_when_dependency_fails():
    handler = runtime.make_readiness_handler(
        db=SimpleNamespace(session=FakeSessionFactory(error=RuntimeError("db down"))),
        redis=FakeRedis(),
        services=_readiness_services(),
    )

    response = await handler(None)
    payload = json.loads(response.text)

    assert response.status == 503
    assert payload["status"] == "degraded"
    assert payload["checks"]["database"] == {
        "ok": False,
        "error": "RuntimeError",
    }
    assert payload["checks"]["redis"]["ok"] is True


async def test_resolve_telegram_proxy_url_keeps_reachable_proxy():
    async def handle_connection(_, writer):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy_url = f"socks5://user:secret@127.0.0.1:{port}"

    try:
        assert _redact_proxy_url(proxy_url) == f"socks5://127.0.0.1:{port}"
        assert (
            await resolve_telegram_proxy_url(
                proxy_url,
                strict=False,
                timeout=0.5,
            )
            == proxy_url
        )
    finally:
        server.close()
        await server.wait_closed()


async def test_resolve_telegram_proxy_url_keeps_configured_unreachable_proxy():
    server = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    proxy_url = f"socks5://127.0.0.1:{port}"

    assert (
        await resolve_telegram_proxy_url(
            proxy_url,
            strict=False,
            timeout=0.05,
        )
        == proxy_url
    )


async def test_resolve_telegram_proxy_url_strict_mode_aborts_startup():
    with pytest.raises(RuntimeError, match="BOT_PROXY_STRICT=True"):
        await resolve_telegram_proxy_url(
            "socks5://127.0.0.1:bad-port",
            strict=True,
            timeout=0.05,
        )


async def test_retry_telegram_operation_retries_network_errors(monkeypatch):
    sleep_delays = []
    attempts = 0

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TelegramNetworkError(method=GetMe(), message="network down")
        return "ok"

    monkeypatch.setattr(runtime.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(runtime, "TELEGRAM_RETRY_DELAY", 1)
    monkeypatch.setattr(runtime, "TELEGRAM_RETRY_MAX_DELAY", 2)

    assert (
        await runtime.retry_telegram_operation("Test Telegram call", operation) == "ok"
    )
    assert sleep_delays == [1]
    assert attempts == 2


def test_archive_rotating_file_handler_limits_current_log_file(tmp_path):
    log_path = tmp_path / "app.log"
    handler = ArchiveRotatingFileHandler(
        filename=str(log_path),
        maxBytes=256,
        backupCount=2,
        encoding="utf-8",
    )
    test_logger = logging.getLogger("tests.runtime_safety.archive")
    old_handlers = test_logger.handlers[:]
    old_level = test_logger.level
    old_propagate = test_logger.propagate

    try:
        test_logger.handlers = [handler]
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False

        for index in range(30):
            test_logger.info("line %02d %s", index, "x" * 40)
    finally:
        handler.close()
        test_logger.handlers = old_handlers
        test_logger.setLevel(old_level)
        test_logger.propagate = old_propagate

    assert log_path.stat().st_size <= 256
    assert 1 <= len(list(tmp_path.glob("*.zip"))) <= 2
