from types import SimpleNamespace

import pytest
from aiohttp import web

from app.bot.utils.network import ping_url
from app.web.additional_profile import AdditionalProfileProxy
from app.web.geo_files import GeoFileProxy
from app.web.primary_profile import PrimaryProfileProxy


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["health", "primary", "additional", "geo"])
async def test_outbound_fetch_uses_configured_proxy(consumer, monkeypatch, tmp_path):
    """Unresolvable upstreams must work through the configured HTTP proxy."""
    received = []
    profile = b"vless://test@vpn.example:443?security=reality#test\n"

    async def serve(request):
        received.append(request.raw_path)
        return web.Response(body=b"x" * 2048 if consumer == "geo" else profile)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "localhost,127.0.0.1")
    url = "http://unreachable.invalid/profile"
    try:
        if consumer == "health":
            assert await ping_url(url) is not None
        elif consumer == "primary":
            proxy = PrimaryProfileProxy(subscription_service=SimpleNamespace())
            source = SimpleNamespace(
                url=url, server=SimpleNamespace(id=1, name="Finland", location="FI")
            )
            response = await proxy._handle_profile_sources(
                sources=[source], user=SimpleNamespace(tg_id=1),
                vpn_id="test", user_agent="test",
            )
            assert response.status == 200
            assert b"vless://" in response.body
        elif consumer == "additional":
            proxy = AdditionalProfileProxy(SimpleNamespace(), mirror_urls=(url,))
            assert (await proxy._fetch_profile()).text.startswith("vless://")
        else:
            proxy = GeoFileProxy(cache_dir=tmp_path, sources={"geoip.dat": (url,)})
            assert len((await proxy._get_snapshot("geoip.dat")).body) == 2048
        assert received == [url]
    finally:
        await runner.cleanup()
