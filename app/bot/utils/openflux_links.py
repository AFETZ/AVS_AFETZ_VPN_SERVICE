"""Profile links contain the document only in the browser-local fragment."""

from urllib.parse import urlencode, urlsplit


def valid_doc_url(value: str) -> bool:
    if not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname in {
                "docs.yandex.ru", "docs.yandex.com", "disk.yandex.ru",
                "disk.yandex.com", "disk.360.yandex.ru",
            }
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443) and parsed.path not in ("", "/")
        )
    except ValueError:
        return False


def mobile_import_url(base_url: str, document_url: str) -> str:
    if not valid_doc_url(document_url):
        raise ValueError("Invalid OpenFlux document")
    base = urlsplit(base_url)
    if (base.scheme != "https" or not base.hostname or base.username is not None
            or base.password is not None or base.query or base.fragment):
        raise ValueError("An HTTPS connection domain is required")
    payload = urlencode({"version": "1", "document_url": document_url})
    return f"{base_url.rstrip('/')}/connection?client=openflux#{payload}"
