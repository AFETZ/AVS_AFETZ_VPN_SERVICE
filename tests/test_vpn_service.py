from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.services.vpn import InboundCache, VPNService
from app.db.models import User


class DummySessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _service(
    subscription_scheme="http",
    server_pool_service=None,
    *,
    subscription_domain=None,
    subscription_backup_domain=None,
):
    config = SimpleNamespace(
        bot=SimpleNamespace(
            DOMAIN="https://bot.example",
            SUBSCRIPTION_DOMAIN=subscription_domain,
            SUBSCRIPTION_BACKUP_DOMAIN=subscription_backup_domain,
        ),
        xui=SimpleNamespace(
            SUBSCRIPTION_PORT=2096,
            SUBSCRIPTION_PATH="/sub/",
            SUBSCRIPTION_SCHEME=subscription_scheme,
        )
    )
    return VPNService(
        config=config,
        session=DummySessionFactory(),
        server_pool_service=server_pool_service or SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_get_key_prefers_dedicated_subscription_domain(monkeypatch):
    user = SimpleNamespace(tg_id=1, server_id=1, vpn_id="vpn-1")
    monkeypatch.setattr(User, "get", AsyncMock(return_value=user))

    service = _service(
        subscription_domain="link.example",
        subscription_backup_domain="bot.example",
    )

    assert await service.get_key(user) == "https://link.example/sub/vpn-1"
    assert await service.get_backup_key(user) == "https://bot.example/sub/vpn-1"


@pytest.mark.asyncio
async def test_get_upstream_key_uses_http_server_scheme(monkeypatch):
    user = SimpleNamespace(
        tg_id=1,
        server_id=1,
        vpn_id="vpn-1",
        server=SimpleNamespace(host="http://kz.example.test:5865/panel/"),
    )
    monkeypatch.setattr(User, "get", AsyncMock(return_value=user))

    assert await _service(subscription_scheme="http").get_upstream_key(user) == (
        "http://kz.example.test:2096/sub/vpn-1"
    )


@pytest.mark.asyncio
async def test_get_upstream_key_keeps_https_server_scheme(monkeypatch):
    user = SimpleNamespace(
        tg_id=1,
        server_id=1,
        vpn_id="vpn-1",
        server=SimpleNamespace(host="https://fi.example.test:5865/panel/"),
    )
    monkeypatch.setattr(User, "get", AsyncMock(return_value=user))

    assert await _service(subscription_scheme="http").get_upstream_key(user) == (
        "https://fi.example.test:2096/sub/vpn-1"
    )


@pytest.mark.asyncio
async def test_switch_server_updates_user_when_client_exists(monkeypatch):
    server = SimpleNamespace(id=2, name="Finland", online=True)
    connection = SimpleNamespace(
        server=server,
        api=SimpleNamespace(client=SimpleNamespace(get_by_email=AsyncMock(return_value=object()))),
    )
    server_pool = SimpleNamespace(
        get_connection_by_server_id=AsyncMock(return_value=connection)
    )
    monkeypatch.setattr(User, "update", AsyncMock())
    user = SimpleNamespace(tg_id=123, server_id=1, server=None)

    result = await _service(server_pool_service=server_pool).switch_server(user, 2)

    assert result.success is True
    assert result.reason == "switched"
    assert result.server is server
    assert user.server_id == 2
    User.update.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_server_rejects_server_without_client(monkeypatch):
    server = SimpleNamespace(id=2, name="Finland", online=True)
    connection = SimpleNamespace(
        server=server,
        api=SimpleNamespace(client=SimpleNamespace(get_by_email=AsyncMock(return_value=None))),
    )
    server_pool = SimpleNamespace(
        get_connection_by_server_id=AsyncMock(return_value=connection)
    )
    monkeypatch.setattr(User, "update", AsyncMock())
    user = SimpleNamespace(tg_id=123, server_id=1, server=None)

    result = await _service(server_pool_service=server_pool).switch_server(user, 2)

    assert result.success is False
    assert result.reason == "client_missing"
    User.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_upstream_profile_sources_built_from_db_servers(monkeypatch):
    kz = SimpleNamespace(id=1, name="Kazakhstan", location="KZ", host="http://kz.example.test:5865/panel/")
    fi = SimpleNamespace(id=3, name="Finland", location="FI", host="https://fi.example.test:5865/panel/")
    user = SimpleNamespace(tg_id=1, server_id=3, vpn_id="vpn-1", server=fi)
    monkeypatch.setattr(User, "get", AsyncMock(return_value=user))

    server_pool = SimpleNamespace(get_profile_servers=AsyncMock(return_value=[kz, fi]))
    sources = await _service(server_pool_service=server_pool).get_upstream_profile_sources(user)

    assert [source.server.id for source in sources] == [1, 3]
    assert sources[0].url == "http://kz.example.test:2096/sub/vpn-1"
    assert sources[1].url == "https://fi.example.test:2096/sub/vpn-1"


@pytest.mark.asyncio
async def test_get_upstream_profile_sources_appends_assigned_server(monkeypatch):
    kz = SimpleNamespace(id=1, name="Kazakhstan", location="KZ", host="http://kz.example.test:5865/panel/")
    legacy = SimpleNamespace(id=2, name="Legacy", location=None, host="http://10.0.0.2:5865/panel/")
    user = SimpleNamespace(tg_id=1, server_id=2, vpn_id="vpn-1", server=legacy)
    monkeypatch.setattr(User, "get", AsyncMock(return_value=user))

    server_pool = SimpleNamespace(get_profile_servers=AsyncMock(return_value=[kz]))
    sources = await _service(server_pool_service=server_pool).get_upstream_profile_sources(user)

    assert [source.server.id for source in sources] == [1, 2]


@pytest.mark.asyncio
async def test_get_client_data_falls_back_to_another_profile_server(monkeypatch):
    finland = SimpleNamespace(id=2, name="Finland")
    kazakhstan = SimpleNamespace(id=1, name="Kazakhstan")
    finland_connection = SimpleNamespace(
        server=finland,
        api=SimpleNamespace(
            client=SimpleNamespace(
                get_by_email=AsyncMock(side_effect=TimeoutError("panel timeout"))
            )
        ),
    )
    client = SimpleNamespace(
        email="123",
        total=0,
        expiry_time=2_000_000_000_000,
        up=100,
        down=200,
        enable=True,
    )
    kazakhstan_connection = SimpleNamespace(
        server=kazakhstan,
        api=SimpleNamespace(
            client=SimpleNamespace(get_by_email=AsyncMock(return_value=client))
        ),
    )
    server_pool = SimpleNamespace(
        get_connection=AsyncMock(return_value=finland_connection),
        get_profile_connections=AsyncMock(
            return_value=[kazakhstan_connection, finland_connection]
        ),
    )
    service = _service(server_pool_service=server_pool)
    monkeypatch.setattr(
        service,
        "_get_limit_ip_from_connection",
        AsyncMock(return_value=3),
    )
    user = SimpleNamespace(tg_id=123, server_id=2)

    result = await service.get_client_data(user, raise_on_error=True)

    assert result is not None
    assert result.max_devices_count == 3
    finland_connection.api.client.get_by_email.assert_awaited_once_with("123")
    kazakhstan_connection.api.client.get_by_email.assert_awaited_once_with("123")
    service._get_limit_ip_from_connection.assert_awaited_once_with(
        connection=kazakhstan_connection,
        client=client,
        inbound_cache=None,
    )


@pytest.mark.asyncio
async def test_get_client_data_skips_fallback_when_assigned_server_returns_data(monkeypatch):
    client = SimpleNamespace(
        email="123",
        total=0,
        expiry_time=2_000_000_000_000,
        up=100,
        down=200,
        enable=True,
    )
    finland_connection = SimpleNamespace(
        server=SimpleNamespace(id=2, name="Finland"),
        api=SimpleNamespace(
            client=SimpleNamespace(get_by_email=AsyncMock(return_value=client))
        ),
    )
    kazakhstan_connection = SimpleNamespace(
        server=SimpleNamespace(id=1, name="Kazakhstan"),
        api=SimpleNamespace(
            client=SimpleNamespace(get_by_email=AsyncMock(side_effect=AssertionError))
        ),
    )
    server_pool = SimpleNamespace(
        get_connection=AsyncMock(return_value=finland_connection),
        get_profile_connections=AsyncMock(
            return_value=[kazakhstan_connection, finland_connection]
        ),
    )
    service = _service(server_pool_service=server_pool)
    monkeypatch.setattr(
        service,
        "_get_limit_ip_from_connection",
        AsyncMock(return_value=3),
    )

    result = await service.get_client_data(
        SimpleNamespace(tg_id=123, server_id=2),
        raise_on_error=True,
    )

    assert result is not None
    assert result.max_devices_count == 3
    finland_connection.api.client.get_by_email.assert_awaited_once_with("123")
    kazakhstan_connection.api.client.get_by_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_client_data_reuses_inbound_cache_for_same_server(monkeypatch):
    server = SimpleNamespace(id=1, name="Kazakhstan")
    first_client = SimpleNamespace(
        email="123",
        total=0,
        expiry_time=2_000_000_000_000,
        up=100,
        down=200,
        enable=True,
    )
    second_client = SimpleNamespace(
        email="456",
        total=0,
        expiry_time=2_000_000_000_000,
        up=300,
        down=400,
        enable=True,
    )
    connection = SimpleNamespace(
        server=server,
        api=SimpleNamespace(
            client=SimpleNamespace(
                get_by_email=AsyncMock(side_effect=[first_client, second_client])
            ),
            inbound=SimpleNamespace(
                get_list=AsyncMock(
                    return_value=[
                        SimpleNamespace(
                            settings=SimpleNamespace(
                                clients=[
                                    SimpleNamespace(email="123", limit_ip=3),
                                    SimpleNamespace(email="456", limit_ip=5),
                                ]
                            )
                        )
                    ]
                )
            ),
        ),
    )
    server_pool = SimpleNamespace(get_connection=AsyncMock(return_value=connection))
    service = _service(server_pool_service=server_pool)
    inbound_cache = InboundCache()

    first_result = await service.get_client_data(
        SimpleNamespace(tg_id=123, server_id=1),
        inbound_cache=inbound_cache,
    )
    second_result = await service.get_client_data(
        SimpleNamespace(tg_id=456, server_id=1),
        inbound_cache=inbound_cache,
    )

    assert first_result.max_devices_count == 3
    assert second_result.max_devices_count == 5
    assert connection.api.inbound.get_list.await_count == 1


@pytest.mark.asyncio
async def test_get_client_data_raises_when_all_profile_servers_fail(monkeypatch):
    finland_connection = SimpleNamespace(
        server=SimpleNamespace(id=2, name="Finland"),
        api=SimpleNamespace(
            client=SimpleNamespace(
                get_by_email=AsyncMock(side_effect=TimeoutError("finland timeout"))
            )
        ),
    )
    kazakhstan_connection = SimpleNamespace(
        server=SimpleNamespace(id=1, name="Kazakhstan"),
        api=SimpleNamespace(
            client=SimpleNamespace(
                get_by_email=AsyncMock(side_effect=TimeoutError("kazakhstan timeout"))
            )
        ),
    )
    server_pool = SimpleNamespace(
        get_connection=AsyncMock(return_value=finland_connection),
        get_profile_connections=AsyncMock(
            return_value=[kazakhstan_connection, finland_connection]
        ),
    )
    service = _service(server_pool_service=server_pool)
    user = SimpleNamespace(tg_id=123, server_id=2)

    with pytest.raises(RuntimeError, match="all available servers"):
        await service.get_client_data(user, raise_on_error=True)
