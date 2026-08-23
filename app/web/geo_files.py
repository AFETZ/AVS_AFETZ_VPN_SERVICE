from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import ClientError, ClientTimeout, web

from app.config import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

GEO_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
GEO_CLIENT_CACHE_SECONDS = 6 * 60 * 60
GEO_DOWNLOAD_TIMEOUT_SECONDS = 45
GEO_MIN_FILE_SIZE = 1024
GEO_FILE_SOURCES = {
    "geoip.dat": (
        "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat",
        "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geoip.dat",
        "https://fastly.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geoip.dat",
    ),
    "geosite.dat": (
        "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat",
        "https://cdn.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geosite.dat",
        "https://fastly.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geosite.dat",
    ),
}


@dataclass(frozen=True)
class GeoFileSnapshot:
    body: bytes
    source_url: str
    is_stale: bool = False


def _is_valid_geo_file(body: bytes, content_type: str | None = None) -> bool:
    if len(body) < GEO_MIN_FILE_SIZE:
        return False
    if content_type and "html" in content_type.lower():
        return False
    prefix = body[:256].lstrip().lower()
    return not prefix.startswith((b"<!doctype", b"<html"))


class GeoFileProxy:
    def __init__(
        self,
        *,
        cache_dir: Path = DEFAULT_DATA_DIR / "geo",
        sources: dict[str, tuple[str, ...]] = GEO_FILE_SOURCES,
        cache_max_age_seconds: int = GEO_CACHE_MAX_AGE_SECONDS,
    ) -> None:
        self.cache_dir = cache_dir
        self.sources = sources
        self.cache_max_age_seconds = cache_max_age_seconds
        self._locks = {name: asyncio.Lock() for name in sources}

    def _cache_path(self, filename: str) -> Path:
        return self.cache_dir / filename

    def _read_cache(
        self,
        filename: str,
        *,
        allow_stale: bool,
    ) -> GeoFileSnapshot | None:
        path = self._cache_path(filename)
        try:
            stat = path.stat()
            if not allow_stale and time.time() - stat.st_mtime > self.cache_max_age_seconds:
                return None
            body = path.read_bytes()
        except OSError:
            return None

        if not _is_valid_geo_file(body):
            logger.warning("Ignoring invalid cached geo file: %s", path)
            return None
        return GeoFileSnapshot(
            body=body,
            source_url="disk-cache",
            is_stale=allow_stale,
        )

    async def _fetch_source(
        self,
        session: aiohttp.ClientSession,
        source_url: str,
    ) -> GeoFileSnapshot | None:
        try:
            async with session.get(source_url, allow_redirects=True) as response:
                if response.status != 200:
                    logger.warning(
                        "Geo mirror returned bad status: url=%s status=%s",
                        source_url,
                        response.status,
                    )
                    return None
                body = await response.read()
                content_type = response.headers.get("content-type")
        except (asyncio.TimeoutError, ClientError) as exception:
            logger.warning(
                "Geo mirror fetch failed: url=%s error=%s",
                source_url,
                exception,
            )
            return None

        if not _is_valid_geo_file(body, content_type):
            logger.warning(
                "Geo mirror returned invalid data: url=%s body_len=%d content_type=%r",
                source_url,
                len(body),
                content_type,
            )
            return None
        return GeoFileSnapshot(body=body, source_url=source_url)

    def _store_cache(self, filename: str, body: bytes) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self._cache_path(filename)
        temporary_path = cache_path.with_suffix(f"{cache_path.suffix}.tmp")
        temporary_path.write_bytes(body)
        temporary_path.replace(cache_path)

    async def _get_snapshot(self, filename: str) -> GeoFileSnapshot:
        cached = self._read_cache(filename, allow_stale=False)
        if cached:
            return cached

        async with self._locks[filename]:
            cached = self._read_cache(filename, allow_stale=False)
            if cached:
                return cached

            timeout = ClientTimeout(total=GEO_DOWNLOAD_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for source_url in self.sources[filename]:
                    snapshot = await self._fetch_source(session, source_url)
                    if not snapshot:
                        continue
                    try:
                        self._store_cache(filename, snapshot.body)
                    except OSError as exception:
                        logger.warning(
                            "Could not persist geo cache file %s: %s",
                            filename,
                            exception,
                        )
                    return snapshot

            stale = self._read_cache(filename, allow_stale=True)
            if stale:
                logger.warning(
                    "All geo mirrors failed; returning stale cache for %s",
                    filename,
                )
                return stale

        raise web.HTTPBadGateway(text="Geo file is temporarily unavailable.")

    async def handle(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        if filename not in self.sources:
            raise web.HTTPNotFound()

        snapshot = await self._get_snapshot(filename)
        etag = hashlib.sha256(snapshot.body).hexdigest()
        if request.headers.get("If-None-Match", "").strip('"') == etag:
            return web.Response(status=304, headers={"ETag": f'"{etag}"'})

        cache_status = "stale" if snapshot.is_stale else "miss"
        if snapshot.source_url == "disk-cache" and not snapshot.is_stale:
            cache_status = "hit"

        return web.Response(
            body=snapshot.body,
            content_type="application/octet-stream",
            headers={
                "Cache-Control": f"public, max-age={GEO_CLIENT_CACHE_SECONDS}",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "ETag": f'"{etag}"',
                "X-Geo-Cache": cache_status,
            },
        )


def setup_geo_file_routes(app: web.Application) -> None:
    proxy = GeoFileProxy()
    app.router.add_get("/{filename:geoip\\.dat|geosite\\.dat}", proxy.handle)
