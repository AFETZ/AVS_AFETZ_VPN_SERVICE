import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import aiohttp
from aiohttp import ClientError, ClientTimeout, web

from app.bot.services.subscription import SubscriptionService

logger = logging.getLogger(__name__)

PROFILE_TITLE = "AVS: AFETZ VPN SERVICE"
PROFILE_AGGREGATED_TITLE = PROFILE_TITLE
PROFILE_UPDATE_INTERVAL_HOURS = "12"
PROFILE_HEALTH_TTL_SECONDS = 10 * 60
PROFILE_SLOW_LATENCY_MS = 1500
# 3x-ui regenerates REALITY links with random sid/spx on every subscription
# fetch. Clients (Happ) treat a byte-different link as a new node and drop the
# active connection on auto-update, so served lines must stay byte-stable.
PROFILE_VOLATILE_QUERY_PARAMS = {"sid", "spx"}
# XHTTP packet-up (what "auto" resolves to) buffers many upload POSTs in the
# client. On iOS the VPN extension has a ~50MB memory cap, so bursty upload
# traffic (e.g. TikTok) gets the tunnel killed by the OS. stream-up keeps a
# single upload stream and stays within the cap; the server inbounds run
# mode=auto, which accepts any client mode.
XHTTP_FORCED_MODE = "stream-up"
XHTTP_REWRITTEN_MODES = {"", "auto", "packet-up"}
# How long a cached line may be served before adopting the upstream's current
# variant (covers panel-side shortId rotation without a bot restart).
PROFILE_LINE_REFRESH_SECONDS = 24 * 60 * 60
# If a user was recently served an active profile, a sudden "no client data"
# is treated as a transient panel error instead of wiping their nodes.
RECENTLY_ACTIVE_SECONDS = 24 * 60 * 60
PROFILE_UPSTREAM_TIMEOUT_SECONDS = 4
PROFILE_SERVER_LABELS = {
    "FI": "🇫🇮 Финляндия",
    "FINLAND": "🇫🇮 Финляндия",
    "KZ": "🇰🇿 Казахстан",
    "KAZAKHSTAN": "🇰🇿 Казахстан",
}
FORWARDED_HEADERS = (
    "content-type",
    "profile-title",
    "profile-update-interval",
    "subscription-userinfo",
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
PROFILE_NODE_PREFIXES = tuple(prefix for prefix in SUPPORTED_PROFILE_PREFIXES if prefix != "#")


@dataclass(frozen=True)
class UpstreamProfileSnapshot:
    server: object
    url: str
    body_bytes: bytes
    headers: dict[str, str]
    latency_ms: int


@dataclass(frozen=True)
class ProfileSourceHealth:
    ok: bool
    latency_ms: int | None
    checked_at: float
    reason: str | None = None


def _new_error_id() -> str:
    return uuid.uuid4().hex[:10]


def _redact_identifier(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _looks_like_supported_profile_body(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    return all(line.startswith(SUPPORTED_PROFILE_PREFIXES) for line in lines)


def _maybe_decode_base64_profile(body_bytes: bytes) -> bytes:
    try:
        encoded = body_bytes.decode("ascii").strip()
    except UnicodeDecodeError:
        return body_bytes

    if not encoded:
        return body_bytes

    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return body_bytes

    if not _looks_like_supported_profile_body(decoded):
        return body_bytes

    return decoded.encode("utf-8")


def _looks_like_html_body(text: str, content_type: str | None) -> bool:
    if content_type and "html" in content_type.lower():
        return True

    stripped = text.lstrip().lower()
    return stripped.startswith("<!doctype") or stripped.startswith("<html")


def _normalize_profile_body_or_raise(
    body_bytes: bytes,
    headers: dict[str, str],
    *,
    context: str,
) -> bytes:
    error_id = _new_error_id()
    normalized_body = _maybe_decode_base64_profile(body_bytes)
    try:
        text = normalized_body.decode("utf-8")
    except UnicodeDecodeError as exception:
        logger.warning(
            "Profile body rejected: error_id=%s context=%s reason=decode_error body_len=%d",
            error_id,
            context,
            len(body_bytes),
        )
        raise web.HTTPBadGateway(
            text=f"Subscription profile is temporarily unavailable. Code: {error_id}"
        ) from exception

    content_type = headers.get("content-type")
    if _looks_like_html_body(text, content_type):
        logger.warning(
            "Profile body rejected: error_id=%s context=%s reason=html_or_bad_content_type content_type=%r body_len=%d",
            error_id,
            context,
            content_type,
            len(body_bytes),
        )
        raise web.HTTPBadGateway(
            text=f"Subscription profile is temporarily unavailable. Code: {error_id}"
        )

    if not _looks_like_supported_profile_body(text):
        logger.warning(
            "Profile body rejected: error_id=%s context=%s reason=no_supported_nodes body_len=%d",
            error_id,
            context,
            len(body_bytes),
        )
        raise web.HTTPBadGateway(
            text=f"Subscription profile is temporarily unavailable. Code: {error_id}"
        )

    return normalized_body


def _first_raw_profile_line(body_bytes: bytes) -> bytes:
    text = body_bytes.decode("utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(PROFILE_NODE_PREFIXES):
            return f"{stripped}\n".encode("utf-8")
    error_id = _new_error_id()
    logger.warning(
        "Raw profile requested but no supported node line was present: error_id=%s",
        error_id,
    )
    raise web.HTTPBadGateway(
        text=f"Raw profile is temporarily unavailable. Code: {error_id}"
    )


def _normalize_forwarded_headers(upstream_headers: dict[str, str]) -> dict[str, str]:
    response_headers = {}
    for name in FORWARDED_HEADERS:
        value = upstream_headers.get(name)
        if not value:
            continue

        # Some 3x-ui variants emit an empty base64 marker which newer Happ builds
        # can treat as malformed metadata.
        if name == "profile-title" and value.strip().lower() == "base64:":
            continue

        response_headers[name] = value

    return response_headers


def _safe_profile_title_value(value: str) -> str | None:
    title = value.replace("\r", " ").replace("\n", " ").strip()
    return title or None


def _server_label(server) -> str | None:
    if not server:
        return None

    location = (getattr(server, "location", "") or "").upper()
    name = (getattr(server, "name", "") or "").upper()
    return (
        PROFILE_SERVER_LABELS.get(location)
        or PROFILE_SERVER_LABELS.get(name)
        or getattr(server, "name", None)
        or getattr(server, "location", None)
    )


def _quality_marker(latency_ms: int | None) -> str:
    if latency_ms is None:
        return ""
    if latency_ms > PROFILE_SLOW_LATENCY_MS:
        return "[SLOW] "
    return ""


def _build_profile_title_for_server(server, *, marker: str = "") -> str | None:
    label = _server_label(server)
    if not label:
        return None

    return _safe_profile_title_value(f"{marker}{label}")


def _build_profile_title(user) -> str | None:
    return _safe_profile_title_value(PROFILE_TITLE)


def _base64_decode_padded(value: str) -> bytes:
    padded = value + ("=" * (-len(value) % 4))
    return base64.b64decode(padded)


def _rename_vmess_profile(line: str, title: str) -> str:
    payload = line[len("vmess://") :]
    try:
        decoded = _base64_decode_padded(payload).decode("utf-8")
        data = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return line

    if not isinstance(data, dict):
        return line

    data["ps"] = title
    encoded = base64.b64encode(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"vmess://{encoded}"


def _tune_xhttp_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    params = dict(pairs)
    if params.get("type") != "xhttp":
        return query
    if params.get("mode", "") not in XHTTP_REWRITTEN_MODES:
        return query

    tuned_pairs = [(name, value) for name, value in pairs if name != "mode"]
    tuned_pairs.append(("mode", XHTTP_FORCED_MODE))
    return urlencode(sorted(tuned_pairs), quote_via=quote)


def _rename_uri_profile(line: str, title: str) -> str:
    try:
        parts = urlsplit(line)
    except ValueError:
        return line

    if not parts.scheme:
        return line

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            _tune_xhttp_query(parts.query),
            quote(title, safe=""),
        )
    )


def _rename_profile_line(line: str, server, *, marker: str = "") -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if not stripped.startswith(PROFILE_NODE_PREFIXES):
        return None

    title = _build_profile_title_for_server(server, marker=marker) or PROFILE_TITLE
    if stripped.startswith("vmess://"):
        return _rename_vmess_profile(stripped, title)
    return _rename_uri_profile(stripped, title)


def _extract_profile_lines(body_bytes: bytes, server, *, marker: str = "") -> list[str]:
    normalized_body = _maybe_decode_base64_profile(body_bytes)
    try:
        text = normalized_body.decode("utf-8")
    except UnicodeDecodeError:
        return []

    lines: list[str] = []
    for line in text.splitlines():
        renamed = _rename_profile_line(line, server, marker=marker)
        if renamed:
            lines.append(renamed)
    return lines


def _profile_line_identity(line: str) -> str:
    if line.startswith("vmess://"):
        try:
            decoded = _base64_decode_padded(line[len("vmess://") :]).decode("utf-8")
            data = json.loads(decoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return line
        if not isinstance(data, dict):
            return line
        data.pop("ps", None)
        return "vmess:" + json.dumps(data, ensure_ascii=False, sort_keys=True)

    try:
        parts = urlsplit(line)
    except ValueError:
        return line

    if not parts.scheme:
        return line

    query_pairs = sorted(
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name not in PROFILE_VOLATILE_QUERY_PARAMS
    )
    return f"{parts.scheme}|{parts.netloc}|{parts.path}|{query_pairs}"


def _server_cache_key(server) -> tuple:
    return (getattr(server, "id", None), getattr(server, "name", None))


def _apply_happ_stability_headers(headers: dict[str, str]) -> None:
    headers["profile-update-interval"] = PROFILE_UPDATE_INTERVAL_HOURS
    headers["subscription-auto-update-enable"] = "1"
    headers["subscription-ping-onopen-enabled"] = "1"
    headers["ping-type"] = "proxy"
    headers["check-url-via-proxy"] = "https://cp.cloudflare.com/generate_204"
    # Raise the xray-core RAM ceiling inside Happ's iOS Network Extension for
    # xhttp profiles. The NE is capped at ~50MB; bursty traffic (TikTok) makes
    # xray exceed it and iOS kills the tunnel ("VPN turns itself off"). This is
    # Happ's official mitigation, toggled purely via this subscription header.
    headers["no-limit-xhttp-enabled"] = "1"


def _build_inactive_profile_response(
    subscription_service: SubscriptionService,
    user,
) -> web.Response:
    headers = {
        "content-type": "text/plain; charset=utf-8",
        "cache-control": "no-store, no-cache, must-revalidate",
        "pragma": "no-cache",
        "expires": "0",
        "profile-title": f"{PROFILE_TITLE} Expired",
    }
    return web.Response(text="# Subscription expired\n", headers=headers)


class PrimaryProfileProxy:
    def __init__(self, subscription_service: SubscriptionService) -> None:
        self.subscription_service = subscription_service
        # (server_key, vpn_id) -> {line_identity: (stable_line, stored_at)}
        self._stable_profile_lines: dict[tuple, dict[str, tuple[str, float]]] = {}
        self._last_active_at: dict[str, float] = {}
        self._source_health: dict[tuple, ProfileSourceHealth] = {}

    def _record_source_health(
        self,
        server,
        *,
        ok: bool,
        latency_ms: int | None = None,
        reason: str | None = None,
    ) -> None:
        self._source_health[_server_cache_key(server)] = ProfileSourceHealth(
            ok=ok,
            latency_ms=latency_ms,
            checked_at=time.monotonic(),
            reason=reason,
        )

    def _source_sort_key(self, source_snapshot: tuple[object, UpstreamProfileSnapshot | None]) -> tuple[int, int, tuple]:
        source, snapshot = source_snapshot
        server = snapshot.server if snapshot else source.server
        health = self._source_health.get(_server_cache_key(server))
        if health and time.monotonic() - health.checked_at > PROFILE_HEALTH_TTL_SECONDS:
            health = None
        if snapshot and health and health.ok:
            latency = health.latency_ms if health.latency_ms is not None else 999_999
            return (0, latency, _server_cache_key(server))
        if health and not health.ok:
            return (2, 999_999, _server_cache_key(server))
        return (1, 999_999, _server_cache_key(server))

    def _is_recently_unhealthy_source(self, source) -> bool:
        health = self._source_health.get(_server_cache_key(getattr(source, "server", None)))
        if not health or health.ok:
            return False
        return time.monotonic() - health.checked_at <= PROFILE_HEALTH_TTL_SECONDS

    def _stabilize_profile_lines(
        self,
        vpn_id: str,
        server,
        lines: list[str],
    ) -> list[str]:
        cache_key = (_server_cache_key(server), vpn_id)
        previous = self._stable_profile_lines.get(cache_key, {})
        current: dict[str, tuple[str, float]] = {}
        now = time.monotonic()
        stable_lines: list[str] = []

        identity_counts: dict[str, int] = {}
        for line in lines:
            identity = _profile_line_identity(line)
            occurrence = identity_counts.get(identity, 0)
            identity_counts[identity] = occurrence + 1
            if occurrence:
                identity = f"{identity}#{occurrence}"

            cached = previous.get(identity)
            if cached and now - cached[1] < PROFILE_LINE_REFRESH_SECONDS:
                stable_line, stored_at = cached
            else:
                stable_line, stored_at = line, now

            current[identity] = (stable_line, stored_at)
            stable_lines.append(stable_line)

        self._stable_profile_lines[cache_key] = current
        return stable_lines

    def _cached_profile_lines(self, vpn_id: str, server) -> list[str]:
        cache_key = (_server_cache_key(server), vpn_id)
        cached = self._stable_profile_lines.get(cache_key, {})
        return [line for line, _ in cached.values()]

    def _build_stable_profile_body(
        self,
        vpn_id: str,
        fetch_results: list[tuple[object, UpstreamProfileSnapshot | None]],
    ) -> bytes:
        lines: list[str] = []

        for source, snapshot in fetch_results:
            if snapshot is not None:
                extracted = _extract_profile_lines(
                    snapshot.body_bytes,
                    snapshot.server,
                    marker=_quality_marker(snapshot.latency_ms),
                )
                lines.extend(
                    self._stabilize_profile_lines(vpn_id, snapshot.server, extracted)
                )
                continue

            cached_lines = self._cached_profile_lines(vpn_id, source.server)
            if cached_lines:
                logger.warning(
                    "Primary profile source %s is unavailable for vpn_id=%s. "
                    "Serving %d cached node(s) to keep the client connected.",
                    getattr(source.server, "name", "?"),
                    _redact_identifier(vpn_id),
                    len(cached_lines),
                )
                lines.extend(cached_lines)

        unique_lines: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            unique_lines.append(line)

        if not unique_lines:
            return b""
        return ("\n".join(unique_lines) + "\n").encode("utf-8")

    async def _fetch_profile_source(
        self,
        session: aiohttp.ClientSession,
        source,
        user,
        user_agent: str,
    ) -> UpstreamProfileSnapshot | None:
        source_url = getattr(source, "url", "")
        source_server = getattr(source, "server", None)
        if not source_url or not source_server:
            return None

        try:
            started_at = time.monotonic()
            async with session.get(
                source_url,
                ssl=False,
                headers={"User-Agent": user_agent or "Happ"},
            ) as response:
                latency_ms = int((time.monotonic() - started_at) * 1000)
                if response.status != 200:
                    self._record_source_health(
                        source_server,
                        ok=False,
                        latency_ms=latency_ms,
                        reason=f"status_{response.status}",
                    )
                    logger.warning(
                        "Primary profile upstream returned bad status %s for user %s server=%s.",
                        response.status,
                        user.tg_id,
                        getattr(source_server, "name", "?"),
                    )
                    return None

                body_bytes = await response.read()
                upstream_headers = {
                    name.lower(): value for name, value in response.headers.items()
                }
        except (asyncio.TimeoutError, ClientError) as exception:
            self._record_source_health(
                source_server,
                ok=False,
                reason=exception.__class__.__name__,
            )
            logger.warning(
                "Primary profile upstream fetch failed for user %s server=%s: %s",
                user.tg_id,
                getattr(source_server, "name", "?"),
                exception,
            )
            return None

        return UpstreamProfileSnapshot(
            server=source_server,
            url=source_url,
            body_bytes=body_bytes,
            headers=upstream_headers,
            latency_ms=latency_ms,
        )

    async def _fetch_and_validate_profile_sources(
        self,
        *,
        session: aiohttp.ClientSession,
        sources,
        user,
        user_agent: str,
    ) -> tuple[list[tuple[object, UpstreamProfileSnapshot | None]], list[UpstreamProfileSnapshot]]:
        async def fetch_one(source) -> tuple[object, UpstreamProfileSnapshot | None]:
            snapshot = await self._fetch_profile_source(
                session=session,
                source=source,
                user=user,
                user_agent=user_agent,
            )
            if not snapshot:
                return source, None

            try:
                _normalize_profile_body_or_raise(
                    snapshot.body_bytes,
                    snapshot.headers,
                    context="aggregated",
                )
            except web.HTTPBadGateway:
                self._record_source_health(
                    snapshot.server,
                    ok=False,
                    latency_ms=snapshot.latency_ms,
                    reason="invalid_profile",
                )
                return source, None

            self._record_source_health(
                snapshot.server,
                ok=True,
                latency_ms=snapshot.latency_ms,
            )
            return source, snapshot

        fetch_results = await asyncio.gather(*(fetch_one(source) for source in sources))
        snapshots = [snapshot for _, snapshot in fetch_results if snapshot is not None]
        return list(fetch_results), snapshots

    async def _handle_profile_sources(
        self,
        *,
        sources,
        user,
        vpn_id: str,
        user_agent: str,
        raw_format: bool = False,
    ) -> web.Response:
        timeout = ClientTimeout(total=PROFILE_UPSTREAM_TIMEOUT_SECONDS)
        active_sources = [
            source for source in sources if not self._is_recently_unhealthy_source(source)
        ]
        deferred_sources = [
            source for source in sources if self._is_recently_unhealthy_source(source)
        ]

        if not active_sources:
            active_sources = list(sources)
            deferred_sources = []

        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            fetch_results, snapshots = await self._fetch_and_validate_profile_sources(
                session=session,
                sources=active_sources,
                user=user,
                user_agent=user_agent,
            )

            if snapshots:
                fetch_results.extend((source, None) for source in deferred_sources)
            elif deferred_sources:
                (
                    deferred_results,
                    deferred_snapshots,
                ) = await self._fetch_and_validate_profile_sources(
                    session=session,
                    sources=deferred_sources,
                    user=user,
                    user_agent=user_agent,
                )
                fetch_results.extend(deferred_results)
                snapshots.extend(deferred_snapshots)

        if not snapshots:
            error_id = _new_error_id()
            logger.error(
                "Primary profile upstream sources are unavailable for user %s. error_id=%s",
                user.tg_id,
                error_id,
            )
            raise web.HTTPBadGateway(
                text=f"Subscription profile is temporarily unavailable. Code: {error_id}"
            )

        fetch_results = [
            item
            for _, item in sorted(
                enumerate(fetch_results),
                key=lambda indexed: (*self._source_sort_key(indexed[1])[:2], indexed[0]),
            )
        ]
        body = self._build_stable_profile_body(vpn_id, fetch_results)
        if not body:
            error_id = _new_error_id()
            logger.error(
                "Primary profile upstream sources returned no supported nodes for user %s. error_id=%s",
                user.tg_id,
                error_id,
            )
            raise web.HTTPBadGateway(
                text=f"Subscription profile is temporarily unavailable. Code: {error_id}"
            )
        response_body = _first_raw_profile_line(body) if raw_format else body

        logger.info(
            "Primary profile access granted: tg_id=%s vpn_id=%s ua=%r sources=%d body_len=%d",
            user.tg_id,
            _redact_identifier(vpn_id),
            user_agent,
            len(snapshots),
            len(response_body),
        )

        user_server_id = getattr(user, "server_id", None)
        header_snapshot = next(
            (
                snapshot
                for snapshot in snapshots
                if getattr(snapshot.server, "id", None) == user_server_id
            ),
            snapshots[0],
        )
        response_headers = _normalize_forwarded_headers(header_snapshot.headers)
        response_headers["profile-title"] = PROFILE_AGGREGATED_TITLE
        if raw_format:
            response_headers["profile-title"] = f"{PROFILE_TITLE} Raw"
        _apply_happ_stability_headers(response_headers)

        response_headers["content-type"] = "text/plain; charset=utf-8"
        response_headers["cache-control"] = "no-store, no-cache, must-revalidate"
        response_headers["pragma"] = "no-cache"
        response_headers["expires"] = "0"

        return web.Response(body=response_body, headers=response_headers)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        vpn_id = request.match_info["vpn_id"]
        user_agent = request.headers.get("User-Agent", "")
        requested_client = request.query.get("client", "")
        requested_format = request.query.get("format", "")
        client_ip = request.headers.get("X-Forwarded-For") or request.headers.get(
            "CF-Connecting-IP"
        ) or (request.remote or "?")

        logger.info(
            "Primary profile request: vpn_id=%s ua=%r ip=%s client=%r format=%r",
            _redact_identifier(vpn_id),
            user_agent,
            client_ip,
            requested_client,
            requested_format,
        )

        user, status = await self.subscription_service.get_subscription_status_by_vpn_id(
            vpn_id
        )

        if not user or not status:
            logger.info(
                "Primary profile access denied: vpn_id=%s user_not_found=True",
                _redact_identifier(vpn_id),
            )
            raise web.HTTPForbidden(text="Forbidden")

        if not status.status_check_ok:
            logger.error(
                "Primary profile status check failed for user %s (vpn_id=%s).",
                user.tg_id,
                _redact_identifier(vpn_id),
            )
            raise web.HTTPServiceUnavailable(
                text="Subscription status is temporarily unavailable."
            )

        if not status.is_active:
            # A user with an assigned server but no client data most likely hit
            # a transient panel error (e.g. "error getting traffics"), not a real
            # expiry: a real expiry still returns client data. Wiping the profile
            # would make the client delete all nodes and drop the connection.
            recently_active = (
                vpn_id in self._last_active_at
                and time.monotonic() - self._last_active_at[vpn_id]
                < RECENTLY_ACTIVE_SECONDS
            )
            if (
                getattr(status, "client_data", None) is None
                and getattr(user, "server_id", None)
                and recently_active
            ):
                logger.error(
                    "Primary profile lookup returned no client data for recently "
                    "active user %s (vpn_id=%s). Treating as transient failure.",
                    user.tg_id,
                    _redact_identifier(vpn_id),
                )
                raise web.HTTPServiceUnavailable(
                    text="Subscription status is temporarily unavailable."
                )

            logger.info(
                "Primary profile inactive for user %s (vpn_id=%s). Returning renewal profile.",
                user.tg_id,
                _redact_identifier(vpn_id),
            )
            return _build_inactive_profile_response(self.subscription_service, user)

        self._last_active_at[vpn_id] = time.monotonic()

        get_upstream_profile_sources = getattr(
            self.subscription_service,
            "get_upstream_profile_sources",
            None,
        )
        if get_upstream_profile_sources:
            sources = await get_upstream_profile_sources(user)
            if sources:
                return await self._handle_profile_sources(
                    sources=sources,
                    user=user,
                    vpn_id=vpn_id,
                    user_agent=user_agent,
                    raw_format=requested_format == "raw",
                )

        upstream_url = await self.subscription_service.get_upstream_profile_url(user)
        if not upstream_url:
            logger.error("Primary profile upstream URL is missing for user %s.", user.tg_id)
            raise web.HTTPBadGateway(text="Upstream source is unavailable.")

        try:
            timeout = ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(
                    upstream_url,
                    ssl=False,
                    headers={"User-Agent": user_agent or "Happ"},
                ) as response:
                    if response.status != 200:
                        error_id = _new_error_id()
                        logger.error(
                            "Primary profile upstream returned bad status %s for user %s. error_id=%s",
                            response.status,
                            user.tg_id,
                            error_id,
                        )
                        raise web.HTTPBadGateway(
                            text=f"Subscription profile is temporarily unavailable. Code: {error_id}"
                        )

                    body_bytes = await response.read()
                    upstream_headers = {
                        name.lower(): value for name, value in response.headers.items()
                    }
        except web.HTTPException:
            raise
        except (asyncio.TimeoutError, ClientError) as exception:
            error_id = _new_error_id()
            logger.warning(
                "Primary profile upstream fetch failed for user %s: %s error_id=%s",
                user.tg_id,
                exception,
                error_id,
            )
            raise web.HTTPBadGateway(
                text=f"Subscription profile is temporarily unavailable. Code: {error_id}"
            ) from exception

        logger.info(
            "Primary profile access granted: tg_id=%s vpn_id=%s ua=%r body_len=%d title=%r ui=%r",
            user.tg_id,
            _redact_identifier(vpn_id),
            user_agent,
            len(body_bytes),
            upstream_headers.get("profile-title"),
            upstream_headers.get("subscription-userinfo"),
        )

        normalized_body = _normalize_profile_body_or_raise(
            body_bytes,
            upstream_headers,
            context=f"single:{requested_client or '-'}",
        )
        response_body = (
            _first_raw_profile_line(normalized_body)
            if requested_format == "raw"
            else normalized_body
        )
        response_headers = _normalize_forwarded_headers(upstream_headers)
        profile_title = _build_profile_title(user)
        if profile_title:
            response_headers["profile-title"] = profile_title
        if requested_format == "raw":
            response_headers["profile-title"] = f"{PROFILE_TITLE} Raw"
        _apply_happ_stability_headers(response_headers)
        response_headers["content-type"] = "text/plain; charset=utf-8"
        response_headers["cache-control"] = "no-store, no-cache, must-revalidate"
        response_headers["pragma"] = "no-cache"
        response_headers["expires"] = "0"

        return web.Response(body=response_body, headers=response_headers)


def setup_primary_profile_route(
    app: web.Application,
    subscription_service: SubscriptionService,
) -> None:
    proxy = PrimaryProfileProxy(subscription_service=subscription_service)
    app.router.add_get("/sub/{vpn_id}", proxy.handle)
