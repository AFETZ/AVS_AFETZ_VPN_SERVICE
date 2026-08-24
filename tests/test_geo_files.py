import os
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from app.web.geo_files import (
    GEO_MIN_FILE_SIZE,
    GeoFileProxy,
    GeoFileSnapshot,
    _is_valid_geo_file,
)


GEO_BODY = b"\x0a\x8f\x01" + (b"geo-data" * (GEO_MIN_FILE_SIZE // 4))


def test_geo_validation_rejects_error_pages_and_truncated_files() -> None:
    assert _is_valid_geo_file(GEO_BODY, "application/octet-stream") is True
    assert _is_valid_geo_file(b"short") is False
    assert _is_valid_geo_file(b"<html>" + (b"x" * GEO_MIN_FILE_SIZE)) is False
    assert _is_valid_geo_file(GEO_BODY, "text/html; charset=utf-8") is False


@pytest.mark.asyncio
async def test_geo_proxy_fetches_once_and_reuses_disk_cache(tmp_path) -> None:
    proxy = GeoFileProxy(
        cache_dir=tmp_path,
        sources={"geoip.dat": ("https://mirror.example/geoip.dat",)},
    )
    proxy._fetch_source = AsyncMock(
        return_value=GeoFileSnapshot(
            body=GEO_BODY,
            source_url="https://mirror.example/geoip.dat",
        )
    )

    first = await proxy._get_snapshot("geoip.dat")
    second = await proxy._get_snapshot("geoip.dat")

    assert first.body == GEO_BODY
    assert second.body == GEO_BODY
    assert second.source_url == "disk-cache"
    assert (tmp_path / "geoip.dat").read_bytes() == GEO_BODY
    proxy._fetch_source.assert_awaited_once()


@pytest.mark.asyncio
async def test_geo_proxy_returns_stale_cache_when_every_mirror_fails(tmp_path) -> None:
    cache_path = tmp_path / "geosite.dat"
    cache_path.write_bytes(GEO_BODY)
    old_timestamp = time.time() - 100
    os.utime(cache_path, (old_timestamp, old_timestamp))
    proxy = GeoFileProxy(
        cache_dir=tmp_path,
        sources={"geosite.dat": ("https://mirror.example/geosite.dat",)},
        cache_max_age_seconds=1,
    )
    proxy._fetch_source = AsyncMock(return_value=None)

    snapshot = await proxy._get_snapshot("geosite.dat")

    assert snapshot.body == GEO_BODY
    assert snapshot.is_stale is True
    proxy._fetch_source.assert_awaited_once()


@pytest.mark.asyncio
async def test_geo_handler_sets_download_cache_and_etag_headers(tmp_path) -> None:
    proxy = GeoFileProxy(
        cache_dir=tmp_path,
        sources={"geoip.dat": ("https://mirror.example/geoip.dat",)},
    )
    proxy._get_snapshot = AsyncMock(
        return_value=GeoFileSnapshot(body=GEO_BODY, source_url="disk-cache")
    )
    request = make_mocked_request(
        "GET",
        "/geoip.dat",
        match_info={"filename": "geoip.dat"},
    )

    response = await proxy.handle(request)

    assert response.status == 200
    assert response.body == GEO_BODY
    assert response.content_type == "application/octet-stream"
    assert response.headers["Content-Disposition"] == 'attachment; filename="geoip.dat"'
    assert response.headers["Cache-Control"].startswith("public, max-age=")
    assert response.headers["ETag"].startswith('"')
