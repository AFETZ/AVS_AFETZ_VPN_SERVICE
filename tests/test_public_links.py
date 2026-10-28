from types import SimpleNamespace

from app.bot.utils.public_links import (
    normalize_base_url,
    public_base_url,
    subscription_backup_base_url,
    subscription_base_url,
)


def test_normalize_base_url_adds_https_and_trims_slash():
    assert normalize_base_url("link.example/") == "https://link.example"
    assert normalize_base_url(" https://link.example/path/ ") == (
        "https://link.example/path"
    )
    assert normalize_base_url(None) is None


def test_public_base_url_uses_bot_domain():
    config = SimpleNamespace(bot=SimpleNamespace(DOMAIN="bot.example/"))

    assert public_base_url(config) == "https://bot.example"


def test_subscription_base_urls_can_be_separated_from_bot_domain():
    config = SimpleNamespace(
        bot=SimpleNamespace(
            DOMAIN="bot.example",
            SUBSCRIPTION_DOMAIN="link.example/",
            SUBSCRIPTION_BACKUP_DOMAIN="backup.example/",
        )
    )

    assert subscription_base_url(config) == "https://link.example"
    assert subscription_backup_base_url(config) == "https://backup.example"


def test_subscription_base_url_falls_back_to_bot_domain():
    config = SimpleNamespace(bot=SimpleNamespace(DOMAIN="bot.example"))

    assert subscription_base_url(config) == "https://bot.example"
    assert subscription_backup_base_url(config) is None
