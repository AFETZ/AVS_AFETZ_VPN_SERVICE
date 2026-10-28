import base64
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError, web
from aiohttp.test_utils import make_mocked_request

from app.web.primary_profile import PrimaryProfileProxy


class FakeUpstreamResponse:
    def __init__(self, status=200, body=b"profile-data", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def read(self):
        return self._body


class FakeClientSession:
    def __init__(self, response=None, error=None, **kwargs):
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        if self.error:
            raise self.error
        return self.response


class MappingFakeClientSession:
    def __init__(self, responses_by_url, **kwargs):
        self.responses_by_url = responses_by_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.responses_by_url[url]


def make_request(vpn_id="vpn-1", headers=None):
    return make_mocked_request(
        "GET",
        f"/sub/{vpn_id}",
        match_info={"vpn_id": vpn_id},
        headers=headers or {"User-Agent": "Happ"},
    )


def make_raw_request(vpn_id="vpn-1", headers=None):
    return make_mocked_request(
        "GET",
        f"/sub/{vpn_id}?format=raw&client=happ",
        match_info={"vpn_id": vpn_id},
        headers=headers or {"User-Agent": "Happ"},
    )


@pytest.mark.asyncio
async def test_proxy_returns_403_for_unknown_user():
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(None, None))
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    with pytest.raises(web.HTTPForbidden):
        await proxy.handle(make_request())


@pytest.mark.asyncio
async def test_proxy_returns_503_when_status_check_failed():
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=False, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status))
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    with pytest.raises(web.HTTPServiceUnavailable):
        await proxy.handle(make_request())


@pytest.mark.asyncio
async def test_proxy_returns_renewal_profile_when_subscription_inactive():
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-1")
    status = SimpleNamespace(status_check_ok=True, is_active=False)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    response = await proxy.handle(make_request())

    assert response.status == 200
    assert b"Subscription expired" in response.body
    assert "profile-web-page-url" not in response.headers
    assert "support-url" not in response.headers


@pytest.mark.asyncio
async def test_proxy_returns_502_on_missing_upstream_url():
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value=None),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    with pytest.raises(web.HTTPBadGateway):
        await proxy.handle(make_request())


@pytest.mark.asyncio
async def test_proxy_returns_502_on_upstream_errors(monkeypatch):
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value="https://xui.example/sub/vpn-1"),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(error=ClientError("boom")),
    )

    with pytest.raises(web.HTTPBadGateway):
        await proxy.handle(make_request())


@pytest.mark.asyncio
async def test_proxy_decodes_base64_profile_and_drops_empty_base64_title(monkeypatch):
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value="https://xui.example/sub/vpn-1"),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    upstream_body = b"dmxlc3M6Ly9hYmM=\n"
    upstream_headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Profile-Title": "base64:",
        "Profile-Update-Interval": "12",
        "Subscription-Userinfo": "upload=10; download=20; total=0; expire=1780036773",
    }

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            response=FakeUpstreamResponse(body=upstream_body, headers=upstream_headers)
        ),
    )

    response = await proxy.handle(make_request("vpn-allowed"))

    assert response.status == 200
    assert response.body == b"vless://abc"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["profile-title"] == "AVS: AFETZ VPN SERVICE"
    assert response.headers["profile-update-interval"] == "12"
    assert (
        response.headers["subscription-userinfo"]
        == "upload=10; download=20; total=0; expire=1780036773"
    )


@pytest.mark.asyncio
async def test_proxy_keeps_plain_profile_body_unchanged(monkeypatch):
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value="https://xui.example/sub/vpn-1"),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    upstream_body = b"vless://abc\n"
    upstream_headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Profile-Title": "Name VPN",
    }

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            response=FakeUpstreamResponse(body=upstream_body, headers=upstream_headers)
        ),
    )

    response = await proxy.handle(make_request("vpn-allowed"))

    assert response.body == upstream_body
    assert response.headers["profile-title"] == "AVS: AFETZ VPN SERVICE"


@pytest.mark.asyncio
async def test_proxy_overrides_profile_title_with_selected_server(monkeypatch):
    user = SimpleNamespace(
        tg_id=1,
        server=SimpleNamespace(name="Kazakhstan", location="KZ"),
    )
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value="https://xui.example/sub/vpn-1"),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            response=FakeUpstreamResponse(
                body=b"vless://abc\n",
                headers={"Profile-Title": "Old title"},
            )
        ),
    )

    response = await proxy.handle(make_request("vpn-allowed"))

    assert response.headers["profile-title"] == "AVS: AFETZ VPN SERVICE"


@pytest.mark.asyncio
async def test_proxy_uses_selected_server_name_when_location_is_unknown(monkeypatch):
    user = SimpleNamespace(
        tg_id=1,
        server=SimpleNamespace(name="Finland", location=None),
    )
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value="https://xui.example/sub/vpn-1"),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            response=FakeUpstreamResponse(body=b"vless://abc\n")
        ),
    )

    response = await proxy.handle(make_request("vpn-allowed"))

    assert response.headers["profile-title"] == "AVS: AFETZ VPN SERVICE"


@pytest.mark.asyncio
async def test_proxy_aggregates_profile_sources_for_happ_switching(monkeypatch):
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-allowed")
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    sources = [
        SimpleNamespace(
            server=SimpleNamespace(name="Kazakhstan", location="KZ"),
            url="https://kz.example/sub/vpn-allowed",
        ),
        SimpleNamespace(
            server=SimpleNamespace(name="Finland", location="FI"),
            url="https://fi.example/sub/vpn-allowed",
        ),
    ]
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_sources=AsyncMock(return_value=sources),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    fi_profile = b"vless://user@fi.example:443?security=reality#old-fi\n"
    encoded_fi_profile = base64.b64encode(fi_profile)
    responses = {
        "https://kz.example/sub/vpn-allowed": FakeUpstreamResponse(
            body=b"vless://user@kz.example:443?security=reality#old-kz\n",
            headers={"Profile-Title": "Old title"},
        ),
        "https://fi.example/sub/vpn-allowed": FakeUpstreamResponse(
            body=encoded_fi_profile,
            headers={"Profile-Title": "Old title"},
        ),
    }

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: MappingFakeClientSession(responses),
    )

    response = await proxy.handle(make_request("vpn-allowed"))

    assert response.status == 200
    assert response.headers["profile-title"] == "AVS: AFETZ VPN SERVICE"
    assert response.headers["subscription-auto-update-enable"] == "1"
    assert response.headers["subscription-ping-onopen-enabled"] == "1"
    assert response.headers["no-limit-xhttp-enabled"] == "1"
    assert response.headers["ping-type"] == "proxy"
    assert (
        response.body
        == (
            f"vless://user@kz.example:443?security=reality#"
            f"{quote('🇰🇿 Казахстан', safe='')}\n"
            f"vless://user@fi.example:443?security=reality#"
            f"{quote('🇫🇮 Финляндия', safe='')}\n"
        ).encode("utf-8")
    )


@pytest.mark.asyncio
async def test_proxy_falls_back_to_text_plain_when_upstream_missing_content_type(monkeypatch):
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value="https://xui.example/sub/vpn-1"),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            response=FakeUpstreamResponse(body=b"vless://abc\n", headers={})
        ),
    )

    response = await proxy.handle(make_request("vpn-allowed"))

    assert response.headers["content-type"] == "text/plain; charset=utf-8"


@pytest.mark.asyncio
async def test_proxy_rejects_html_or_non_profile_upstream(monkeypatch):
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value="https://xui.example/sub/vpn-1"),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            response=FakeUpstreamResponse(
                body=b"<html>auth failed</html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        ),
    )

    with pytest.raises(web.HTTPBadGateway):
        await proxy.handle(make_request("vpn-allowed"))


@pytest.mark.asyncio
async def test_proxy_raw_format_returns_first_supported_node(monkeypatch):
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_url=AsyncMock(return_value="https://xui.example/sub/vpn-1"),
    )
    proxy = PrimaryProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            response=FakeUpstreamResponse(
                body=b"# comment\nvless://first\nvless://second\n",
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
        ),
    )

    response = await proxy.handle(make_raw_request("vpn-allowed"))

    assert response.body == b"vless://first\n"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["profile-title"] == "AVS: AFETZ VPN SERVICE Raw"


class SequenceFakeClientSession:
    """Returns queued responses per URL; raises queued exceptions."""

    def __init__(self, responses_by_url, **kwargs):
        self.responses_by_url = responses_by_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        queue = self.responses_by_url[url]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def _aggregated_service(user, status, sources):
    return SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status)),
        get_upstream_profile_sources=AsyncMock(return_value=sources),
    )


def _reality_line(host, sid, spx, fragment):
    return (
        f"vless://uuid@{host}:443?security=reality&pbk=PBK&sid={sid}&spx={spx}"
        f"#{fragment}\n"
    ).encode()


@pytest.mark.asyncio
async def test_proxy_body_is_byte_stable_when_upstream_randomizes_params(monkeypatch):
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-stable")
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    sources = [
        SimpleNamespace(
            server=SimpleNamespace(id=1, name="Kazakhstan", location="KZ"),
            url="https://kz.example/sub/vpn-stable",
        ),
        SimpleNamespace(
            server=SimpleNamespace(id=3, name="Finland", location="FI"),
            url="https://fi.example/sub/vpn-stable",
        ),
    ]
    proxy = PrimaryProfileProxy(subscription_service=_aggregated_service(user, status, sources))

    responses = {
        "https://kz.example/sub/vpn-stable": [
            FakeUpstreamResponse(body=_reality_line("kz.example", "aaa111", "%2Fone", "kz-old")),
            FakeUpstreamResponse(body=_reality_line("kz.example", "bbb222", "%2Ftwo", "kz-new")),
        ],
        "https://fi.example/sub/vpn-stable": [
            FakeUpstreamResponse(body=_reality_line("fi.example", "ccc333", "%2Fthree", "fi-old")),
            FakeUpstreamResponse(body=_reality_line("fi.example", "ddd444", "%2Ffour", "fi-new")),
        ],
    }
    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: SequenceFakeClientSession(responses),
    )

    first = await proxy.handle(make_request("vpn-stable"))
    second = await proxy.handle(make_request("vpn-stable"))

    assert first.status == 200
    assert second.body == first.body
    assert b"sid=aaa111" in second.body
    assert b"sid=ccc333" in second.body


@pytest.mark.asyncio
async def test_proxy_serves_cached_nodes_when_source_fetch_fails(monkeypatch):
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-cache")
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    sources = [
        SimpleNamespace(
            server=SimpleNamespace(id=1, name="Kazakhstan", location="KZ"),
            url="https://kz.example/sub/vpn-cache",
        ),
        SimpleNamespace(
            server=SimpleNamespace(id=3, name="Finland", location="FI"),
            url="https://fi.example/sub/vpn-cache",
        ),
    ]
    proxy = PrimaryProfileProxy(subscription_service=_aggregated_service(user, status, sources))

    responses = {
        "https://kz.example/sub/vpn-cache": [
            FakeUpstreamResponse(body=_reality_line("kz.example", "kz1", "%2Fa", "kz")),
        ],
        "https://fi.example/sub/vpn-cache": [
            FakeUpstreamResponse(body=_reality_line("fi.example", "fi1", "%2Fb", "fi")),
            ClientError("fi panel down"),
        ],
    }
    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: SequenceFakeClientSession(responses),
    )

    first = await proxy.handle(make_request("vpn-cache"))
    second = await proxy.handle(make_request("vpn-cache"))

    assert second.status == 200
    assert b"fi.example" in second.body
    assert second.body == first.body


@pytest.mark.asyncio
async def test_proxy_skips_recently_unhealthy_source_when_other_source_is_available(
    monkeypatch,
):
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-skip")
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    kz_server = SimpleNamespace(id=1, name="Kazakhstan", location="KZ")
    fi_server = SimpleNamespace(id=3, name="Finland", location="FI")
    sources = [
        SimpleNamespace(
            server=kz_server,
            url="https://kz.example/sub/vpn-skip",
        ),
        SimpleNamespace(
            server=fi_server,
            url="https://fi.example/sub/vpn-skip",
        ),
    ]
    proxy = PrimaryProfileProxy(subscription_service=_aggregated_service(user, status, sources))
    proxy._record_source_health(kz_server, ok=False, reason="TimeoutError")

    responses = {
        "https://fi.example/sub/vpn-skip": FakeUpstreamResponse(
            body=_reality_line("fi.example", "fi1", "%2Fb", "fi")
        ),
    }
    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: MappingFakeClientSession(responses),
    )

    response = await proxy.handle(make_request("vpn-skip"))

    assert response.status == 200
    assert b"fi.example" in response.body
    assert b"kz.example" not in response.body


@pytest.mark.asyncio
async def test_proxy_returns_503_instead_of_wiping_recently_active_user(monkeypatch):
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-guard", server_id=3)
    active_status = SimpleNamespace(status_check_ok=True, is_active=True)
    sources = [
        SimpleNamespace(
            server=SimpleNamespace(id=3, name="Finland", location="FI"),
            url="https://fi.example/sub/vpn-guard",
        ),
    ]
    service = _aggregated_service(user, active_status, sources)
    proxy = PrimaryProfileProxy(subscription_service=service)

    responses = {
        "https://fi.example/sub/vpn-guard": [
            FakeUpstreamResponse(body=_reality_line("fi.example", "fi1", "%2Fb", "fi")),
        ],
    }
    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: SequenceFakeClientSession(responses),
    )

    first = await proxy.handle(make_request("vpn-guard"))
    assert first.status == 200

    transient_status = SimpleNamespace(
        status_check_ok=True, is_active=False, client_data=None
    )
    service.get_subscription_status_by_vpn_id = AsyncMock(
        return_value=(user, transient_status)
    )

    with pytest.raises(web.HTTPServiceUnavailable):
        await proxy.handle(make_request("vpn-guard"))

    expired_status = SimpleNamespace(
        status_check_ok=True, is_active=False, client_data=object()
    )
    service.get_subscription_status_by_vpn_id = AsyncMock(
        return_value=(user, expired_status)
    )

    response = await proxy.handle(make_request("vpn-guard"))
    assert b"Subscription expired" in response.body


@pytest.mark.asyncio
async def test_proxy_rewrites_xhttp_auto_mode_to_stream_up(monkeypatch):
    user = SimpleNamespace(tg_id=1, vpn_id="vpn-xhttp")
    status = SimpleNamespace(status_check_ok=True, is_active=True)
    sources = [
        SimpleNamespace(
            server=SimpleNamespace(id=1, name="Kazakhstan", location="KZ"),
            url="https://kz.example/sub/vpn-xhttp",
        ),
    ]
    proxy = PrimaryProfileProxy(subscription_service=_aggregated_service(user, status, sources))

    line = (
        b"vless://uuid@kz.example:443?encryption=none&security=reality&pbk=PBK"
        b"&sid=aaa111&spx=%2Fone&type=xhttp&mode=auto&path=%2F#kz\n"
    )
    line2 = (
        b"vless://uuid@kz.example:443?encryption=none&security=reality&pbk=PBK"
        b"&sid=bbb222&spx=%2Ftwo&type=xhttp&mode=auto&path=%2F#kz\n"
    )
    responses = {
        "https://kz.example/sub/vpn-xhttp": [
            FakeUpstreamResponse(body=line),
            FakeUpstreamResponse(body=line2),
        ],
    }
    monkeypatch.setattr(
        "app.web.primary_profile.aiohttp.ClientSession",
        lambda **kwargs: SequenceFakeClientSession(responses),
    )

    first = await proxy.handle(make_request("vpn-xhttp"))
    second = await proxy.handle(make_request("vpn-xhttp"))

    assert b"mode=stream-up" in first.body
    assert b"mode=auto" not in first.body
    assert b"type=xhttp" in first.body
    assert second.body == first.body
