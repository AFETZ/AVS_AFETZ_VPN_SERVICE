from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from py3xui import Client
from app.bot.services.vpn import VPNService


def setup_service():
    connections = [NS(server=NS(id=i)) for i in (3, 4)]
    clients = [Client(id=f"client-{i}", email="470313670", enable=True,
                      expiry_time=1900000000000, limit_ip=1, total_gb=12345,
                      flow="xtls-rprx-vision", sub_id="keep") for i in (3, 4)]
    pool = NS(get_connection_by_server_id=AsyncMock(side_effect=connections))
    service = VPNService(NS(), NS(), pool)
    service.get_upstream_profile_sources = AsyncMock(return_value=[NS(server=c.server) for c in connections])
    service.xui_gateway = NS(
        get_inbound_list=AsyncMock(side_effect=[ [NS(id=i, settings=NS(clients=[c]))] for i,c in zip((3,4),clients)]),
        update_client=AsyncMock(),
    )
    service.refresh_subscription_snapshot = AsyncMock()
    return service, NS(tg_id=470313670, is_blocked=False), clients


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_lifetime_updates_every_node_preserving_limits(enabled):
    service, user, originals = setup_service()
    assert await service.set_lifetime_subscription(user, enabled)
    assert service.xui_gateway.update_client.await_count == 2
    for call in service.xui_gateway.update_client.await_args_list:
        client = call.kwargs["client"]
        assert client.expiry_time == 0
        assert client.enable is enabled
        assert (client.limit_ip, client.total_gb, client.flow, client.sub_id) == (1, 12345, "xtls-rprx-vision", "keep")
    assert all(c.expiry_time == 1900000000000 for c in originals)
    service.refresh_subscription_snapshot.assert_awaited_once_with(user)


@pytest.mark.asyncio
async def test_lifetime_no_writes_if_any_node_unavailable():
    service, user, _ = setup_service()
    service.server_pool_service.get_connection_by_server_id.side_effect = [NS(server=NS(id=3)), None]
    assert not await service.set_lifetime_subscription(user, True)
    service.xui_gateway.update_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifetime_rolls_back_failed_write_without_reporting_success():
    service, user, _ = setup_service()
    service.xui_gateway.update_client.side_effect = [None, RuntimeError("offline"), None, None]
    assert not await service.set_lifetime_subscription(user, True)
    calls = service.xui_gateway.update_client.await_args_list
    assert len(calls) == 4
    assert [c.kwargs["client"].expiry_time for c in calls[2:]] == [1900000000000]*2
    service.refresh_subscription_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifetime_cannot_enable_blocked_user():
    service, user, _ = setup_service()
    user.is_blocked = True
    assert not await service.set_lifetime_subscription(user, True)
    service.get_upstream_profile_sources.assert_not_awaited()
