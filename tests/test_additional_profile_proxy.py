from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError, web
from aiohttp.test_utils import make_mocked_request

from app.web.additional_profile import (
    ADDITIONAL_PROFILE_TITLE,
    ADDITIONAL_PROFILE_MIRROR_URLS,
    AdditionalProfileSnapshot,
    AdditionalProfileProxy,
    CurrentWhitelistProfileProxy,
    FILTERED_ADDITIONAL_PROFILE_MIRROR_URLS,
    FILTERED_ADDITIONAL_PROFILE_TITLE,
    _build_current_whitelist_profile,
    _parse_whitelist_cidrs,
)


PROFILE_TEXT = "vless://live-profile\n# profile comment"


class FakeUpstreamResponse:
    def __init__(self, status=200, text=PROFILE_TEXT):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self, encoding="utf-8"):
        return self._text


class FakeClientSession:
    def __init__(self, responses=None, error=None, calls=None, **kwargs):
        self.responses = list(responses or [])
        self.error = error
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        if not self.responses:
            raise ClientError("unexpected mirror request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_request(vpn_id="vpn-1", path_prefix="/wl"):
    return make_mocked_request(
        "GET",
        f"{path_prefix}/{vpn_id}",
        match_info={"vpn_id": vpn_id},
    )


def make_active_service():
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True, has_additional_profile=True)
    return SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status))
    )


@pytest.mark.asyncio
async def test_proxy_returns_403_for_unknown_user():
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(None, None))
    )
    proxy = AdditionalProfileProxy(subscription_service=service)

    with pytest.raises(web.HTTPForbidden):
        await proxy.handle(make_request())


@pytest.mark.asyncio
async def test_proxy_returns_503_when_status_check_failed():
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=False, is_active=True, has_additional_profile=True)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status))
    )
    proxy = AdditionalProfileProxy(subscription_service=service)

    with pytest.raises(web.HTTPServiceUnavailable):
        await proxy.handle(make_request())


@pytest.mark.asyncio
async def test_proxy_returns_403_when_user_has_no_entitlement():
    user = SimpleNamespace(tg_id=1)
    status = SimpleNamespace(status_check_ok=True, is_active=True, has_additional_profile=False)
    service = SimpleNamespace(
        get_subscription_status_by_vpn_id=AsyncMock(return_value=(user, status))
    )
    proxy = AdditionalProfileProxy(subscription_service=service)

    with pytest.raises(web.HTTPForbidden):
        await proxy.handle(make_request())


@pytest.mark.asyncio
async def test_proxy_returns_502_on_upstream_errors(monkeypatch):
    service = make_active_service()
    proxy = AdditionalProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.additional_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(error=ClientError("boom")),
    )

    with pytest.raises(web.HTTPBadGateway):
        await proxy.handle(make_request())


@pytest.mark.asyncio
async def test_proxy_falls_back_from_first_mirror_to_second(monkeypatch):
    calls = []
    service = make_active_service()
    proxy = AdditionalProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.additional_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            calls=calls,
            responses=[
                FakeUpstreamResponse(status=503),
                FakeUpstreamResponse(text=PROFILE_TEXT),
            ],
        ),
    )

    response = await proxy.handle(make_request("vpn-allowed"))

    assert response.status == 200
    assert response.text == PROFILE_TEXT
    assert response.content_type == "text/plain"
    assert response.charset == "utf-8"
    assert response.headers["Cache-Control"] == "no-store, no-cache, must-revalidate"
    assert response.headers["profile-title"] == ADDITIONAL_PROFILE_TITLE
    assert response.headers["profile-update-interval"] == "1"
    assert response.headers["subscription-auto-update-enable"] == "1"
    assert response.headers["subscription-ping-onopen-enabled"] == "1"
    assert response.headers["ping-type"] == "proxy"
    assert response.headers["no-limit-xhttp-enabled"] == "1"
    assert response.headers["check-url-via-proxy"] == "https://cp.cloudflare.com/generate_204"
    assert "subscription-autoconnect" not in response.headers
    assert "subscription-autoconnect-type" not in response.headers
    assert response.headers["X-Profile-Source"] == ADDITIONAL_PROFILE_MIRROR_URLS[1]
    assert response.headers["X-Profile-Stale"] == "0"
    assert [url for url, _ in calls] == list(ADDITIONAL_PROFILE_MIRROR_URLS[:3])
    assert calls[0][1]["timeout"].total == 5


def test_proxy_mirror_order_keeps_experimental_sources_last():
    assert ADDITIONAL_PROFILE_MIRROR_URLS[:3] == (
        "https://raw.githubusercontent.com/zieng2/wl/main/vless_universal.txt",
        "https://codeberg.org/zieng2/wl/raw/branch/main/vless_universal.txt",
        "https://gitlab.com/zieng2/wl/raw/main/vless_universal.txt",
    )
    assert ADDITIONAL_PROFILE_MIRROR_URLS[3:] == (
        "https://hub.mos.ru/zieng2/wl/raw/main/list_universal.txt",
        "https://gitverse.ru/api/repos/zieng2/wl/raw/branch/master/list_universal.txt",
    )


@pytest.mark.asyncio
async def test_filtered_proxy_uses_igareck_sources_and_title(monkeypatch):
    calls = []
    service = make_active_service()
    proxy = AdditionalProfileProxy(
        subscription_service=service,
        mirror_urls=FILTERED_ADDITIONAL_PROFILE_MIRROR_URLS,
        profile_title=FILTERED_ADDITIONAL_PROFILE_TITLE,
        profile_label="filtered additional profile",
    )

    monkeypatch.setattr(
        "app.web.additional_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            calls=calls,
            responses=[FakeUpstreamResponse(text=PROFILE_TEXT)],
        ),
    )

    response = await proxy.handle(make_request("vpn-allowed", path_prefix="/wl-filtered"))

    assert response.status == 200
    assert response.text == PROFILE_TEXT
    assert response.headers["profile-title"] == FILTERED_ADDITIONAL_PROFILE_TITLE
    assert response.headers["X-Profile-Source"] == FILTERED_ADDITIONAL_PROFILE_MIRROR_URLS[0]
    assert [url for url, _ in calls] == list(FILTERED_ADDITIONAL_PROFILE_MIRROR_URLS[:3])


def test_filtered_profile_mirror_order_prefers_light_mobile_subscription():
    assert FILTERED_ADDITIONAL_PROFILE_MIRROR_URLS[0] == (
        "https://raw.githack.com/igareck/vpn-configs-for-russia/main/"
        "Vless-Reality-White-Lists-Rus-Mobile.txt"
    )
    assert all(
        url.endswith("/Vless-Reality-White-Lists-Rus-Mobile.txt")
        for url in FILTERED_ADDITIONAL_PROFILE_MIRROR_URLS
    )


def test_current_whitelist_profile_prefers_xhttp_and_rejects_unlisted_ips():
    cidrs = _parse_whitelist_cidrs(
        "1.1.1.0/24\n8.8.8.8/32\n",
        "https://cidr.example/list.txt",
    )
    assert cidrs is not None
    profile = _build_current_whitelist_profile(
        [
            "\n".join(
                [
                    "vless://id@1.1.1.3:443?type=grpc&security=reality#grpc",
                    "vless://id@1.1.1.2:443?type=xhttp&mode=auto#auto",
                    "vless://id@1.1.1.1:443?type=xhttp&mode=packet-up#packet",
                    "vless://id@9.9.9.9:443?type=xhttp&mode=packet-up#blocked",
                    "vless://id@1.1.1.1:443?mode=packet-up&type=xhttp#duplicate",
                ]
            )
        ],
        cidrs,
    )

    lines = profile.splitlines()
    assert lines[0].endswith("#packet")
    assert lines[1].endswith("#auto")
    assert lines[2].endswith("#grpc")
    assert all("9.9.9.9" not in line for line in lines)
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_current_whitelist_profile_drops_unreachable_endpoints(monkeypatch):
    proxy = CurrentWhitelistProfileProxy(subscription_service=make_active_service())

    async def probe(host, port):
        return (host, port) == ("1.1.1.1", 443)

    monkeypatch.setattr(proxy, "_probe_endpoint", probe)
    profile = await proxy._filter_reachable_nodes(
        "\n".join(
            [
                "vless://id@1.1.1.1:443?type=xhttp#reachable",
                "vless://id@8.8.8.8:443?type=xhttp#unreachable",
                "vless://id@1.1.1.1:443?type=grpc#same-endpoint",
            ]
        )
    )

    assert profile.splitlines() == [
        "vless://id@1.1.1.1:443?type=xhttp#reachable",
        "vless://id@1.1.1.1:443?type=grpc#same-endpoint",
    ]


@pytest.mark.asyncio
async def test_current_whitelist_proxy_builds_and_reuses_cached_profile(monkeypatch):
    service = make_active_service()
    proxy = CurrentWhitelistProfileProxy(subscription_service=service)
    candidate = SimpleNamespace(
        _fetch_profile=AsyncMock(
            return_value=AdditionalProfileSnapshot(
                text="vless://id@1.1.1.1:443?type=xhttp&mode=packet-up#current\n",
                source_url="https://profiles.example/current.txt",
            )
        )
    )
    proxy._candidate_proxies = (candidate,)
    cidrs = _parse_whitelist_cidrs(
        "1.1.1.0/24\n",
        "https://cidr.example/current.txt",
    )
    assert cidrs is not None
    monkeypatch.setattr(proxy, "_fetch_cidr_snapshot", AsyncMock(return_value=cidrs))
    monkeypatch.setattr(
        proxy,
        "_filter_reachable_nodes",
        AsyncMock(side_effect=lambda text: text),
    )

    first = await proxy._fetch_profile()
    second = await proxy._fetch_profile()

    assert first == second
    assert "mode=packet-up" in first.text
    candidate._fetch_profile.assert_awaited_once()
    proxy._fetch_cidr_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_current_whitelist_proxy_serves_stale_cache_when_refresh_fails(monkeypatch):
    service = make_active_service()
    proxy = CurrentWhitelistProfileProxy(subscription_service=service)
    proxy._cached_profile = AdditionalProfileSnapshot(
        text="vless://cached@1.1.1.1:443?type=xhttp&mode=packet-up#cached\n",
        source_url="https://profiles.example/cached.txt",
    )
    proxy._cached_profile_at = 0
    proxy._candidate_proxies = (
        SimpleNamespace(_fetch_profile=AsyncMock(side_effect=ClientError("down"))),
    )
    monkeypatch.setattr(
        proxy,
        "_fetch_cidr_snapshot",
        AsyncMock(side_effect=ClientError("down")),
    )

    snapshot = await proxy._fetch_profile()

    assert snapshot.is_stale is True
    assert "cached@1.1.1.1" in snapshot.text


@pytest.mark.asyncio
async def test_proxy_returns_cached_profile_when_all_mirrors_fail_after_success(monkeypatch):
    service = make_active_service()
    proxy = AdditionalProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.additional_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            responses=[FakeUpstreamResponse(text=PROFILE_TEXT)],
        ),
    )

    first_response = await proxy.handle(make_request("vpn-allowed"))
    assert first_response.headers["X-Profile-Stale"] == "0"

    monkeypatch.setattr(
        "app.web.additional_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(error=ClientError("boom")),
    )

    stale_response = await proxy.handle(make_request("vpn-allowed"))

    assert stale_response.status == 200
    assert stale_response.text == PROFILE_TEXT
    assert stale_response.headers["X-Profile-Source"] == ADDITIONAL_PROFILE_MIRROR_URLS[0]
    assert stale_response.headers["X-Profile-Stale"] == "1"


@pytest.mark.asyncio
async def test_proxy_rejects_empty_and_html_like_profile_without_caching(monkeypatch):
    service = make_active_service()
    proxy = AdditionalProfileProxy(subscription_service=service)

    monkeypatch.setattr(
        "app.web.additional_profile.aiohttp.ClientSession",
        lambda **kwargs: FakeClientSession(
            responses=[
                FakeUpstreamResponse(text=""),
                FakeUpstreamResponse(text="<html>not a profile</html>"),
                FakeUpstreamResponse(status=404),
                ClientError("boom"),
                FakeUpstreamResponse(text="plain text"),
            ],
        ),
    )

    with pytest.raises(web.HTTPBadGateway):
        await proxy.handle(make_request("vpn-allowed"))

    assert proxy._cached_profile is None
