import asyncio
import bisect
import ipaddress
import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from aiohttp import ClientError, ClientTimeout, web

from app.bot.services.subscription import SubscriptionService

logger = logging.getLogger(__name__)

ADDITIONAL_PROFILE_MIRROR_TIMEOUT_SECONDS = 5
SMART_PROFILE_CACHE_TTL_SECONDS = 5 * 60
SMART_PROFILE_MAX_NODES = 100
SMART_PROFILE_PROBE_TIMEOUT_SECONDS = 2
SMART_PROFILE_PROBE_CONCURRENCY = 32
ADDITIONAL_PROFILE_TITLE = "AFETZ BS Backup"
ADDITIONAL_PROFILE_UPDATE_INTERVAL_HOURS = "1"
ADDITIONAL_PROFILE_PRIMARY_MIRROR_COUNT = 3
ADDITIONAL_PROFILE_MIRROR_URLS = (
    "https://raw.githubusercontent.com/zieng2/wl/main/vless_universal.txt",
    "https://codeberg.org/zieng2/wl/raw/branch/main/vless_universal.txt",
    "https://gitlab.com/zieng2/wl/raw/main/vless_universal.txt",
    "https://hub.mos.ru/zieng2/wl/raw/main/list_universal.txt",
    "https://gitverse.ru/api/repos/zieng2/wl/raw/branch/master/list_universal.txt",
)
FILTERED_ADDITIONAL_PROFILE_TITLE = "AFETZ BS Recommended"
FILTERED_ADDITIONAL_PROFILE_MIRROR_URLS = (
    "https://raw.githack.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://codeberg.org/igareck/vpn-configs-for-russia/raw/branch/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://bitbucket.org/igareck/vpn-configs-for-russia/raw/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/main/Vless-Reality-White-Lists-Rus-Mobile.txt",
)
WHITELIST_CIDR_MIRROR_URLS = (
    "https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main/cidrwhitelist.txt",
    "https://cdn.jsdelivr.net/gh/hxehex/russia-mobile-internet-whitelist@main/cidrwhitelist.txt",
    "https://raw.githack.com/hxehex/russia-mobile-internet-whitelist/main/cidrwhitelist.txt",
)
SUPPORTED_PROFILE_PREFIXES = (
    "vless://",
    "vmess://",
    "trojan://",
    "ss://",
    "socks://",
    "hy2://",
    "hysteria2://",
    "#",
)


def _redact_identifier(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


@dataclass(frozen=True)
class AdditionalProfileSnapshot:
    text: str
    source_url: str
    is_stale: bool = False


@dataclass(frozen=True)
class WhitelistCidrSnapshot:
    starts: tuple[int, ...]
    ends: tuple[int, ...]
    source_url: str

    def contains(self, address: ipaddress.IPv4Address) -> bool:
        value = int(address)
        index = bisect.bisect_right(self.starts, value) - 1
        return index >= 0 and value <= self.ends[index]


def _looks_like_supported_profile_body(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    return all(line.startswith(SUPPORTED_PROFILE_PREFIXES) for line in lines)


def _parse_whitelist_cidrs(text: str, source_url: str) -> WhitelistCidrSnapshot | None:
    ranges: list[tuple[int, int]] = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return None
        if network.version != 4:
            continue
        ranges.append((int(network.network_address), int(network.broadcast_address)))

    if not ranges:
        return None

    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
            continue
        merged.append([start, end])

    return WhitelistCidrSnapshot(
        starts=tuple(item[0] for item in merged),
        ends=tuple(item[1] for item in merged),
        source_url=source_url,
    )


def _profile_line_identity(line: str) -> tuple | str:
    try:
        parts = urlsplit(line)
    except ValueError:
        return line
    if not parts.scheme:
        return line
    return (
        parts.scheme,
        parts.netloc,
        parts.path,
        tuple(sorted(parse_qsl(parts.query, keep_blank_values=True))),
    )


def _whitelist_profile_rank(line: str) -> tuple[int, int]:
    parts = urlsplit(line)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    transport = params.get("type", "")
    mode = params.get("mode", "")

    if transport == "xhttp" and mode == "packet-up":
        return (0, 0)
    if transport == "xhttp":
        mode_rank = {"auto": 0, "": 1, "stream-one": 2, "stream-up": 3}
        return (1, mode_rank.get(mode, 4))
    transport_rank = {"grpc": 2, "raw": 3, "tcp": 3, "ws": 4}
    return (transport_rank.get(transport, 5), 0)


def _build_current_whitelist_profile(
    profile_texts: list[str],
    cidrs: WhitelistCidrSnapshot,
) -> str:
    ranked_lines: list[tuple[tuple[int, int], int, str]] = []
    seen: set[tuple | str] = set()
    source_order = 0

    for profile_text in profile_texts:
        for line in profile_text.splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            try:
                parts = urlsplit(value)
                address = ipaddress.ip_address(parts.hostname or "")
            except ValueError:
                continue
            if (
                not isinstance(address, ipaddress.IPv4Address)
                or not address.is_global
                or not cidrs.contains(address)
            ):
                continue

            identity = _profile_line_identity(value)
            if identity in seen:
                continue
            seen.add(identity)
            ranked_lines.append((_whitelist_profile_rank(value), source_order, value))
            source_order += 1

    ranked_lines.sort(key=lambda item: (item[0], item[1]))
    selected = [item[2] for item in ranked_lines]
    if not selected:
        return ""
    return "\n".join(selected) + "\n"


def _build_response_headers(
    snapshot: AdditionalProfileSnapshot,
    *,
    profile_title: str = ADDITIONAL_PROFILE_TITLE,
) -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        "profile-title": profile_title,
        "profile-update-interval": ADDITIONAL_PROFILE_UPDATE_INTERVAL_HOURS,
        "subscription-auto-update-enable": "1",
        "subscription-ping-onopen-enabled": "1",
        "ping-type": "proxy",
        "check-url-via-proxy": "https://cp.cloudflare.com/generate_204",
        # iOS Network Extension memory ceiling workaround — see primary_profile.
        "no-limit-xhttp-enabled": "1",
        "X-Profile-Source": snapshot.source_url,
        "X-Profile-Stale": "1" if snapshot.is_stale else "0",
    }


class AdditionalProfileProxy:
    def __init__(
        self,
        subscription_service: SubscriptionService,
        *,
        mirror_urls: tuple[str, ...] = ADDITIONAL_PROFILE_MIRROR_URLS,
        profile_title: str = ADDITIONAL_PROFILE_TITLE,
        profile_label: str = "additional profile",
    ) -> None:
        self.subscription_service = subscription_service
        self.mirror_urls = mirror_urls
        self.profile_title = profile_title
        self.profile_label = profile_label
        self._cached_profile: AdditionalProfileSnapshot | None = None

    async def _fetch_mirror(
        self,
        session: aiohttp.ClientSession,
        mirror_url: str,
        request_timeout: ClientTimeout,
    ) -> AdditionalProfileSnapshot | None:
        try:
            async with session.get(mirror_url, timeout=request_timeout) as response:
                if response.status != 200:
                    logger.warning(
                        "Additional profile mirror returned bad status: profile=%s url=%s status=%s",
                        self.profile_label,
                        mirror_url,
                        response.status,
                    )
                    return None

                text = await response.text(encoding="utf-8")
        except (asyncio.TimeoutError, ClientError, UnicodeDecodeError) as exception:
            logger.warning(
                "Additional profile mirror fetch failed: profile=%s url=%s error=%s",
                self.profile_label,
                mirror_url,
                exception,
            )
            return None

        if not _looks_like_supported_profile_body(text):
            logger.warning(
                "Additional profile mirror returned invalid profile body: profile=%s url=%s body_len=%d",
                self.profile_label,
                mirror_url,
                len(text),
            )
            return None

        return AdditionalProfileSnapshot(
            text=text,
            source_url=mirror_url,
        )

    async def _fetch_first_available_mirror(
        self,
        session: aiohttp.ClientSession,
        mirror_urls: tuple[str, ...],
        request_timeout: ClientTimeout,
    ) -> AdditionalProfileSnapshot | None:
        tasks = [
            asyncio.create_task(
                self._fetch_mirror(
                    session=session,
                    mirror_url=mirror_url,
                    request_timeout=request_timeout,
                )
            )
            for mirror_url in mirror_urls
        ]

        try:
            for task in asyncio.as_completed(tasks):
                snapshot = await task
                if snapshot:
                    return snapshot
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        return None

    def _mirror_groups(self) -> tuple[tuple[str, ...], ...]:
        primary = self.mirror_urls[:ADDITIONAL_PROFILE_PRIMARY_MIRROR_COUNT]
        fallback = self.mirror_urls[ADDITIONAL_PROFILE_PRIMARY_MIRROR_COUNT:]
        return tuple(group for group in (primary, fallback) if group)

    async def _fetch_profile(self) -> AdditionalProfileSnapshot:
        request_timeout = ClientTimeout(total=ADDITIONAL_PROFILE_MIRROR_TIMEOUT_SECONDS)

        async with aiohttp.ClientSession(trust_env=True) as session:
            for mirror_group in self._mirror_groups():
                snapshot = await self._fetch_first_available_mirror(
                    session=session,
                    mirror_urls=mirror_group,
                    request_timeout=request_timeout,
                )
                if not snapshot:
                    continue

                self._cached_profile = snapshot
                return snapshot

        if self._cached_profile:
            logger.warning(
                "All additional profile mirrors failed for %s. Returning stale cached profile from %s.",
                self.profile_label,
                self._cached_profile.source_url,
            )
            return AdditionalProfileSnapshot(
                text=self._cached_profile.text,
                source_url=self._cached_profile.source_url,
                is_stale=True,
            )

        raise web.HTTPBadGateway(text="Upstream source is unavailable.")

    async def handle(self, request: web.Request) -> web.Response:
        vpn_id = request.match_info["vpn_id"]
        user, status = await self.subscription_service.get_subscription_status_by_vpn_id(vpn_id)

        if not user or not status:
            logger.info(
                "Additional profile access denied: profile=%s vpn_id=%s user_not_found=True",
                self.profile_label,
                _redact_identifier(vpn_id),
            )
            raise web.HTTPForbidden(text="Forbidden")

        if not status.status_check_ok:
            logger.error(
                "Additional profile status check failed for user %s (profile=%s vpn_id=%s).",
                user.tg_id,
                self.profile_label,
                _redact_identifier(vpn_id),
            )
            raise web.HTTPServiceUnavailable(text="Subscription status is temporarily unavailable.")

        if not status.is_active or not status.has_additional_profile:
            logger.info(
                "Additional profile access denied for user %s (profile=%s vpn_id=%s). active=%s entitled=%s",
                user.tg_id,
                self.profile_label,
                _redact_identifier(vpn_id),
                status.is_active,
                status.has_additional_profile,
            )
            raise web.HTTPForbidden(text="Forbidden")

        snapshot = await self._fetch_profile()

        logger.info(
            "Additional profile access granted for user %s (profile=%s vpn_id=%s source=%s stale=%s).",
            user.tg_id,
            self.profile_label,
            _redact_identifier(vpn_id),
            snapshot.source_url,
            snapshot.is_stale,
        )
        return web.Response(
            text=snapshot.text,
            content_type="text/plain",
            charset="utf-8",
            headers=_build_response_headers(snapshot, profile_title=self.profile_title),
        )


class CurrentWhitelistProfileProxy(AdditionalProfileProxy):
    def __init__(
        self,
        subscription_service: SubscriptionService,
        *,
        cidr_mirror_urls: tuple[str, ...] = WHITELIST_CIDR_MIRROR_URLS,
    ) -> None:
        super().__init__(
            subscription_service=subscription_service,
            mirror_urls=(),
            profile_title=FILTERED_ADDITIONAL_PROFILE_TITLE,
            profile_label="current CIDR whitelist profile",
        )
        self.cidr_mirror_urls = cidr_mirror_urls
        self._candidate_proxies = (
            AdditionalProfileProxy(
                subscription_service=subscription_service,
                mirror_urls=ADDITIONAL_PROFILE_MIRROR_URLS,
                profile_label="current CIDR candidate feed",
            ),
            AdditionalProfileProxy(
                subscription_service=subscription_service,
                mirror_urls=FILTERED_ADDITIONAL_PROFILE_MIRROR_URLS,
                profile_label="secondary CIDR candidate feed",
            ),
        )
        self._cached_profile_at: float | None = None
        self._refresh_lock = asyncio.Lock()

    async def _probe_endpoint(self, host: str, port: int) -> bool:
        try:
            async with asyncio.timeout(SMART_PROFILE_PROBE_TIMEOUT_SECONDS):
                _, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            return False
        return True

    async def _filter_reachable_nodes(self, text: str) -> str:
        lines_with_endpoints: list[tuple[str, tuple[str, int]]] = []
        endpoints: list[tuple[str, int]] = []
        seen_endpoints: set[tuple[str, int]] = set()

        for line in text.splitlines():
            try:
                parts = urlsplit(line)
                host = parts.hostname
                port = parts.port
            except ValueError:
                continue
            if not host or port is None:
                continue

            endpoint = (host, port)
            lines_with_endpoints.append((line, endpoint))
            if endpoint not in seen_endpoints:
                seen_endpoints.add(endpoint)
                endpoints.append(endpoint)

        semaphore = asyncio.Semaphore(SMART_PROFILE_PROBE_CONCURRENCY)

        async def probe(endpoint: tuple[str, int]) -> tuple[tuple[str, int], bool]:
            async with semaphore:
                return endpoint, await self._probe_endpoint(*endpoint)

        results = await asyncio.gather(*(probe(endpoint) for endpoint in endpoints))
        reachable = {endpoint for endpoint, is_reachable in results if is_reachable}
        selected = [
            line
            for line, endpoint in lines_with_endpoints
            if endpoint in reachable
        ][:SMART_PROFILE_MAX_NODES]
        if not selected:
            return ""
        return "\n".join(selected) + "\n"

    def _fresh_cached_profile(self) -> AdditionalProfileSnapshot | None:
        if not self._cached_profile or self._cached_profile_at is None:
            return None
        if time.monotonic() - self._cached_profile_at >= SMART_PROFILE_CACHE_TTL_SECONDS:
            return None
        return self._cached_profile

    async def _fetch_cidr_mirror(
        self,
        session: aiohttp.ClientSession,
        url: str,
        request_timeout: ClientTimeout,
    ) -> WhitelistCidrSnapshot | None:
        try:
            async with session.get(url, timeout=request_timeout) as response:
                if response.status != 200:
                    logger.warning(
                        "Whitelist CIDR mirror returned bad status: url=%s status=%s",
                        url,
                        response.status,
                    )
                    return None
                text = await response.text(encoding="utf-8")
        except (asyncio.TimeoutError, ClientError, UnicodeDecodeError) as exception:
            logger.warning(
                "Whitelist CIDR mirror fetch failed: url=%s error=%s",
                url,
                exception,
            )
            return None

        snapshot = _parse_whitelist_cidrs(text, url)
        if not snapshot:
            logger.warning("Whitelist CIDR mirror returned invalid data: url=%s", url)
        return snapshot

    async def _fetch_cidr_snapshot(self) -> WhitelistCidrSnapshot:
        timeout = ClientTimeout(total=ADDITIONAL_PROFILE_MIRROR_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(trust_env=True) as session:
            tasks = [
                asyncio.create_task(self._fetch_cidr_mirror(session, url, timeout))
                for url in self.cidr_mirror_urls
            ]
            try:
                for task in asyncio.as_completed(tasks):
                    snapshot = await task
                    if snapshot:
                        return snapshot
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        raise web.HTTPBadGateway(text="Current whitelist data is unavailable.")

    async def _refresh_profile(self) -> AdditionalProfileSnapshot:
        results = await asyncio.gather(
            *(proxy._fetch_profile() for proxy in self._candidate_proxies),
            self._fetch_cidr_snapshot(),
            return_exceptions=True,
        )
        cidr_result = results[-1]
        if isinstance(cidr_result, BaseException):
            raise web.HTTPBadGateway(text="Current whitelist data is unavailable.")

        profile_snapshots = [
            result
            for result in results[:-1]
            if isinstance(result, AdditionalProfileSnapshot)
        ]
        if not profile_snapshots:
            raise web.HTTPBadGateway(text="Whitelist profile sources are unavailable.")

        cidr_filtered_text = _build_current_whitelist_profile(
            [snapshot.text for snapshot in profile_snapshots],
            cidr_result,
        )
        text = await self._filter_reachable_nodes(cidr_filtered_text)
        if not text:
            raise web.HTTPBadGateway(text="No current whitelist nodes are available.")

        selected_lines = text.splitlines()
        xhttp_nodes = sum(
            1
            for line in selected_lines
            if dict(parse_qsl(urlsplit(line).query, keep_blank_values=True)).get("type")
            == "xhttp"
        )
        logger.info(
            "Current CIDR whitelist profile refreshed: candidates=%d selected=%d xhttp=%d",
            sum(len(snapshot.text.splitlines()) for snapshot in profile_snapshots),
            len(selected_lines),
            xhttp_nodes,
        )

        snapshot = AdditionalProfileSnapshot(
            text=text,
            source_url=",".join(
                [snapshot.source_url for snapshot in profile_snapshots]
                + [cidr_result.source_url]
            ),
            is_stale=any(snapshot.is_stale for snapshot in profile_snapshots),
        )
        self._cached_profile = snapshot
        self._cached_profile_at = time.monotonic()
        return snapshot

    async def _fetch_profile(self) -> AdditionalProfileSnapshot:
        cached_profile = self._fresh_cached_profile()
        if cached_profile:
            return cached_profile

        async with self._refresh_lock:
            cached_profile = self._fresh_cached_profile()
            if cached_profile:
                return cached_profile
            try:
                return await self._refresh_profile()
            except web.HTTPException:
                if not self._cached_profile:
                    raise
                logger.warning(
                    "Current whitelist profile refresh failed. Returning stale cached profile."
                )
                return AdditionalProfileSnapshot(
                    text=self._cached_profile.text,
                    source_url=self._cached_profile.source_url,
                    is_stale=True,
                )


def setup_additional_profile_route(
    app: web.Application,
    subscription_service: SubscriptionService,
) -> None:
    proxy = AdditionalProfileProxy(subscription_service=subscription_service)
    app.router.add_get("/wl/{vpn_id}", proxy.handle)

    filtered_proxy = CurrentWhitelistProfileProxy(
        subscription_service=subscription_service,
    )
    app.router.add_get("/wl-filtered/{vpn_id}", filtered_proxy.handle)
