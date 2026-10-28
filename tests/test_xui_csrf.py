from unittest.mock import AsyncMock
import pytest
from py3xui import Client
from app.bot.services.xui_csrf import CsrfAsyncApi


@pytest.mark.asyncio
async def test_v3_update_sends_explicit_disabled_and_unlimited_values():
    api = CsrfAsyncApi("https://panel.example/path", "admin", "password")
    api.client._post = AsyncMock()
    client = Client(id="uuid", email="test@example.org", enable=False, expiry_time=0, limit_ip=1)
    await api.client.update(client.id, client)
    url, _, data = api.client._post.await_args.args
    assert url == "https://panel.example/path/panel/api/clients/update/test%40example.org"
    assert data["enable"] is False
    assert data["expiryTime"] == 0
    assert data["limitIp"] == 1


@pytest.mark.asyncio
async def test_v3_create_keeps_attachment_and_limits():
    api = CsrfAsyncApi("https://panel.example/path", "admin", "password")
    api.client._post = AsyncMock()
    await api.client.add(4, [Client(id="uuid", email="test", enable=True, expiry_time=0)])
    url, _, data = api.client._post.await_args.args
    assert url.endswith("/panel/api/clients/add")
    assert data["inboundIds"] == [4]
    assert data["client"]["enable"] is True
    assert data["client"]["expiryTime"] == 0
