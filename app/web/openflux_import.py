"""Public static launcher; it never reads or serves the private profile."""

from pathlib import Path

from aiohttp.web import Response

_HTML = (Path(__file__).parent / "templates/openflux_import.html").read_text()


def import_page() -> Response:
    return Response(text=_HTML, content_type="text/html", headers={
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    })
