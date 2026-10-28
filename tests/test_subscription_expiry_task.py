import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.tasks import subscription_expiry


class DummySessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.set_calls = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value
        self.set_calls.append((key, value, ex))


class FakeI18n:
    def gettext(self, key, locale=None):
        return f"{key}: {{devices}} {{expiry_time}}"


class FakeVPNService:
    def __init__(self, client_data_by_tg_id):
        self.client_data_by_tg_id = client_data_by_tg_id
        self.inbound_cache_ids = []

    async def get_client_data(self, user, inbound_cache=None):
        self.inbound_cache_ids.append(id(inbound_cache))
        return self.client_data_by_tg_id[user.tg_id]


class FakeNotificationService:
    def __init__(self, config=None):
        self.config = config
        self.messages = []

    async def notify_by_id(self, chat_id, text):
        self.messages.append((chat_id, text))


class TrackingVPNService:
    def __init__(self, client_data_by_tg_id=None, delay=0):
        self.client_data_by_tg_id = dict(client_data_by_tg_id or {})
        self.delay = delay
        self.calls = []
        self.inbound_cache_ids = []
        self.active_calls = 0
        self.max_active_calls = 0

    async def get_client_data(self, user, inbound_cache=None):
        self.calls.append(user.tg_id)
        self.inbound_cache_ids.append(id(inbound_cache))
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.client_data_by_tg_id.get(user.tg_id)
        finally:
            self.active_calls -= 1


def _expiry_ms(hours_left: int) -> int:
    expiry = datetime.now(timezone.utc) + timedelta(hours=hours_left)
    return int(expiry.timestamp() * 1000)


def _client_data(expiry_time: int):
    return SimpleNamespace(
        _expiry_time=expiry_time,
        max_devices=3,
        expiry_time="2 ч.",
    )


async def _run_task(monkeypatch, client_data, redis):
    user = SimpleNamespace(
        tg_id=123,
        language_code="ru",
        server_id=1,
        is_blocked=False,
        current_period_started_at=None,
        current_period_duration_days=None,
    )
    notification_service = FakeNotificationService()

    monkeypatch.setattr(
        subscription_expiry.User,
        "get_all",
        AsyncMock(return_value=[user]),
    )

    await subscription_expiry.notify_users_with_expiring_subscription(
        session_factory=DummySessionFactory(),
        redis=redis,
        i18n=FakeI18n(),
        vpn_service=FakeVPNService({user.tg_id: client_data}),
        notification_service=notification_service,
    )

    return notification_service


@pytest.mark.asyncio
async def test_expiring_in_20_hours_uses_regular_notification(monkeypatch):
    expiry_time = _expiry_ms(20)
    redis = FakeRedis()

    notification_service = await _run_task(
        monkeypatch,
        client_data=_client_data(expiry_time),
        redis=redis,
    )

    assert notification_service.messages == [
        (123, "task:message:subscription_expiry: 3 2 ч.")
    ]
    assert redis.set_calls == [
        (
            "user:notified:subscription_expiry:123:"
            f"{expiry_time}:24h",
            "true",
            subscription_expiry.EXPIRY_NOTIFICATION_TTL,
        )
    ]


@pytest.mark.asyncio
async def test_expiring_in_2_hours_uses_urgent_notification(monkeypatch):
    expiry_time = _expiry_ms(2)
    redis = FakeRedis()

    notification_service = await _run_task(
        monkeypatch,
        client_data=_client_data(expiry_time),
        redis=redis,
    )

    assert notification_service.messages == [
        (123, "task:message:subscription_expiry_urgent: 3 2 ч.")
    ]
    assert redis.set_calls[0][0] == (
        "user:notified:subscription_expiry:123:" f"{expiry_time}:3h"
    )


@pytest.mark.asyncio
async def test_expiry_notification_does_not_include_external_renewal_link(monkeypatch):
    expiry_time = _expiry_ms(20)
    user = SimpleNamespace(
        tg_id=123,
        vpn_id="vpn-123",
        language_code="ru",
        server_id=1,
        is_blocked=False,
        current_period_started_at=None,
        current_period_duration_days=None,
    )
    notification_service = FakeNotificationService()
    monkeypatch.setattr(
        subscription_expiry.User,
        "get_all",
        AsyncMock(return_value=[user]),
    )

    await subscription_expiry.notify_users_with_expiring_subscription(
        session_factory=DummySessionFactory(),
        redis=FakeRedis(),
        i18n=FakeI18n(),
        vpn_service=FakeVPNService({user.tg_id: _client_data(expiry_time)}),
        notification_service=notification_service,
    )

    assert notification_service.messages == [
        (123, "task:message:subscription_expiry: 3 2 ч.")
    ]


@pytest.mark.asyncio
async def test_urgent_notification_is_sent_after_regular_notification(monkeypatch):
    expiry_time = _expiry_ms(2)
    regular_key = "user:notified:subscription_expiry:123:" f"{expiry_time}:24h"
    redis = FakeRedis({regular_key: "true"})

    notification_service = await _run_task(
        monkeypatch,
        client_data=_client_data(expiry_time),
        redis=redis,
    )

    assert notification_service.messages == [
        (123, "task:message:subscription_expiry_urgent: 3 2 ч.")
    ]
    assert redis.set_calls[0][0] == (
        "user:notified:subscription_expiry:123:" f"{expiry_time}:3h"
    )


@pytest.mark.asyncio
async def test_legacy_regular_notification_key_does_not_block_urgent(monkeypatch):
    expiry_time = _expiry_ms(2)
    redis = FakeRedis({"user:notified:123": "true"})

    notification_service = await _run_task(
        monkeypatch,
        client_data=_client_data(expiry_time),
        redis=redis,
    )

    assert notification_service.messages == [
        (123, "task:message:subscription_expiry_urgent: 3 2 ч.")
    ]
    assert redis.set_calls[0][0] == (
        "user:notified:subscription_expiry:123:" f"{expiry_time}:3h"
    )


@pytest.mark.asyncio
async def test_legacy_regular_notification_key_blocks_regular_duplicate(monkeypatch):
    expiry_time = _expiry_ms(20)
    redis = FakeRedis({"user:notified:123": "true"})

    notification_service = await _run_task(
        monkeypatch,
        client_data=_client_data(expiry_time),
        redis=redis,
    )

    assert notification_service.messages == []
    assert redis.set_calls == []


@pytest.mark.asyncio
async def test_far_future_local_period_is_skipped_without_panel_check(monkeypatch):
    now = datetime.now(timezone.utc)
    users = [
        SimpleNamespace(
            tg_id=1,
            language_code="ru",
            server_id=1,
            is_blocked=False,
            current_period_started_at=now,
            current_period_duration_days=30,
        ),
        SimpleNamespace(
            tg_id=2,
            language_code="ru",
            server_id=1,
            is_blocked=False,
            current_period_started_at=now - timedelta(days=1),
            current_period_duration_days=2,
        ),
    ]
    vpn_service = TrackingVPNService()
    notification_service = FakeNotificationService()
    monkeypatch.setattr(subscription_expiry.User, "get_all", AsyncMock(return_value=users))

    await subscription_expiry.notify_users_with_expiring_subscription(
        session_factory=DummySessionFactory(),
        redis=FakeRedis(),
        i18n=FakeI18n(),
        vpn_service=vpn_service,
        notification_service=notification_service,
    )

    assert vpn_service.calls == [2]
    assert notification_service.messages == []


@pytest.mark.asyncio
async def test_blocked_users_and_users_without_server_are_skipped(monkeypatch):
    users = [
        SimpleNamespace(
            tg_id=1,
            language_code="ru",
            server_id=None,
            is_blocked=False,
            current_period_started_at=None,
            current_period_duration_days=None,
        ),
        SimpleNamespace(
            tg_id=2,
            language_code="ru",
            server_id=1,
            is_blocked=True,
            current_period_started_at=None,
            current_period_duration_days=None,
        ),
        SimpleNamespace(
            tg_id=3,
            language_code="ru",
            server_id=1,
            is_blocked=False,
            current_period_started_at=None,
            current_period_duration_days=None,
        ),
    ]
    vpn_service = TrackingVPNService()
    monkeypatch.setattr(subscription_expiry.User, "get_all", AsyncMock(return_value=users))

    await subscription_expiry.notify_users_with_expiring_subscription(
        session_factory=DummySessionFactory(),
        redis=FakeRedis(),
        i18n=FakeI18n(),
        vpn_service=vpn_service,
        notification_service=FakeNotificationService(),
    )

    assert vpn_service.calls == [3]


@pytest.mark.asyncio
async def test_background_check_limits_concurrency_and_shares_inbound_cache(monkeypatch):
    users = [
        SimpleNamespace(
            tg_id=tg_id,
            language_code="ru",
            server_id=1,
            is_blocked=False,
            current_period_started_at=None,
            current_period_duration_days=None,
        )
        for tg_id in range(10)
    ]
    vpn_service = TrackingVPNService(delay=0.02)
    monkeypatch.setattr(subscription_expiry.User, "get_all", AsyncMock(return_value=users))

    await subscription_expiry.notify_users_with_expiring_subscription(
        session_factory=DummySessionFactory(),
        redis=FakeRedis(),
        i18n=FakeI18n(),
        vpn_service=vpn_service,
        notification_service=FakeNotificationService(),
    )

    assert sorted(vpn_service.calls) == list(range(10))
    assert vpn_service.max_active_calls <= subscription_expiry.EXPIRY_CHECK_CONCURRENCY
    assert len(set(vpn_service.inbound_cache_ids)) == 1


@pytest.mark.asyncio
async def test_already_sent_urgent_notification_is_not_duplicated(monkeypatch):
    expiry_time = _expiry_ms(2)
    urgent_key = "user:notified:subscription_expiry:123:" f"{expiry_time}:3h"
    redis = FakeRedis({urgent_key: "true"})

    notification_service = await _run_task(
        monkeypatch,
        client_data=_client_data(expiry_time),
        redis=redis,
    )

    assert notification_service.messages == []
    assert redis.set_calls == []
