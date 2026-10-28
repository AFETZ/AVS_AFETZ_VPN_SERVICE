from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import make_mocked_request
from aiohttp.web import HTTPFound

from app.bot.routers.download.handler import redirect_to_connection
from app.bot.utils.openflux_links import mobile_import_url


async def test_launcher_returns_static_page_without_profile_or_redirect():
    request = make_mocked_request("GET", "/connection?client=openflux&document_url=private-test")
    response = await redirect_to_connection(request)
    assert response.status == 200
    assert response.content_type == "text/html"
    assert "private-test" not in response.text
    assert "location.hash" in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


async def test_existing_happ_redirect_still_works():
    key = "https://example.test/sub/private"
    request = make_mocked_request("GET", "/connection?" + urlencode({"scheme": "happ://add/", "key": key}))
    with pytest.raises(HTTPFound) as response:
        await redirect_to_connection(request)
    assert response.value.location == "happ://add/" + key


@pytest.mark.parametrize("base", ["http://example.test", "https://user@example.test", "https://example.test/?x=1", "https://example.test/#x"])
def test_import_link_rejects_unsafe_connection_base(base):
    with pytest.raises(ValueError):
        mobile_import_url(base, "https://disk.yandex.ru/i/test")
