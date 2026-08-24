from __future__ import annotations

from typing import Any


def normalize_base_url(domain: str | None) -> str | None:
    if not domain:
        return None

    value = str(domain).strip().rstrip("/")
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def _bot_config(config: Any) -> Any:
    return getattr(config, "bot", config)


def public_base_url(config: Any) -> str | None:
    return normalize_base_url(getattr(_bot_config(config), "DOMAIN", None))


def subscription_base_url(config: Any) -> str | None:
    bot_config = _bot_config(config)
    return normalize_base_url(
        getattr(bot_config, "SUBSCRIPTION_DOMAIN", None)
        or getattr(bot_config, "DOMAIN", None)
    )


def subscription_backup_base_url(config: Any) -> str | None:
    return normalize_base_url(
        getattr(_bot_config(config), "SUBSCRIPTION_BACKUP_DOMAIN", None)
    )
