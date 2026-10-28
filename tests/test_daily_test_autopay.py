from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.web import Application

from app.bot.payment_gateways import yookassa as yookassa_module
from app.bot.payment_gateways.yookassa import Yookassa
from app.bot.payment_gateways.yookassa import AutopayUnavailableError
from app.bot.tasks import daily_test_autopay as autopay_task
from app.bot.utils.constants import TransactionStatus
from app.bot.utils.daily_test_autopay import (
    is_moscow_charge_window,
    next_random_charge_at,
    random_charge_at_for_moscow_date,
)
from app.db.models import DailyTestAutopayPayment, DailyTestAutopayProfile
from app.db.models.daily_test_autopay import (
    AUTOPAY_BINDING_KIND,
    AUTOPAY_DAILY_KIND,
)


class DummySessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _gateway() -> Yookassa:
    gateway = Yookassa.__new__(Yookassa)
    gateway.app = Application()
    gateway.config = SimpleNamespace(
        shop=SimpleNamespace(EMAIL="billing@example.com")
    )
    gateway.session = DummySessionFactory()
    gateway.storage = SimpleNamespace(redis=None)
    gateway.bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot"))
    )
    gateway.services = SimpleNamespace(
        notification=SimpleNamespace(notify_by_id=AsyncMock())
    )
    return gateway


def test_random_charge_time_stays_inside_moscow_window():
    target_date = date(2026, 8, 26)

    first = random_charge_at_for_moscow_date(target_date, randbelow=lambda _: 0)
    last = random_charge_at_for_moscow_date(target_date, randbelow=lambda _: 959)

    assert first == datetime(2026, 8, 26, 3, 0)
    assert last == datetime(2026, 8, 26, 18, 59)


def test_next_random_charge_is_always_on_next_moscow_day():
    now_utc = datetime(2026, 8, 25, 21, 30)  # 26 Aug, 00:30 MSK

    result = next_random_charge_at(now_utc, randbelow=lambda _: 120)

    assert result == datetime(2026, 8, 27, 5, 0)  # 08:00 MSK


def test_moscow_charge_window_is_end_exclusive():
    assert is_moscow_charge_window(datetime(2026, 8, 25, 3, 0)) is True
    assert is_moscow_charge_window(datetime(2026, 8, 25, 18, 59)) is True
    assert is_moscow_charge_window(datetime(2026, 8, 25, 19, 0)) is False


async def test_due_profile_is_charged_once_and_rescheduled(monkeypatch):
    profile = SimpleNamespace(
        user_tg_id=123,
        payment_method_id="pm-token",
        enabled=True,
    )
    monkeypatch.setattr(
        DailyTestAutopayProfile,
        "get_due",
        AsyncMock(return_value=[profile]),
    )
    record_attempt = AsyncMock()
    monkeypatch.setattr(
        DailyTestAutopayProfile,
        "record_attempt",
        record_attempt,
    )
    gateway = SimpleNamespace(create_daily_autopayment=AsyncMock())
    factory = SimpleNamespace(get_gateway=lambda _: gateway)

    await autopay_task.process_due_daily_test_autopayments(
        DummySessionFactory(),
        factory,
        now_utc=datetime(2026, 8, 25, 10, 0),
    )

    gateway.create_daily_autopayment.assert_awaited_once_with(
        profile=profile,
        scheduled_for=date(2026, 8, 25),
    )
    record_attempt.assert_awaited_once()
    assert record_attempt.await_args.kwargs["next_charge_at"] > datetime(2026, 8, 25, 10, 0)


async def test_overdue_profile_is_not_charged_outside_window(monkeypatch):
    profile = SimpleNamespace(
        user_tg_id=123,
        payment_method_id="pm-token",
        enabled=True,
    )
    monkeypatch.setattr(
        DailyTestAutopayProfile,
        "get_due",
        AsyncMock(return_value=[profile]),
    )
    record_attempt = AsyncMock()
    monkeypatch.setattr(
        DailyTestAutopayProfile,
        "record_attempt",
        record_attempt,
    )
    gateway = SimpleNamespace(create_daily_autopayment=AsyncMock())
    factory = SimpleNamespace(get_gateway=lambda _: gateway)

    await autopay_task.process_due_daily_test_autopayments(
        DummySessionFactory(),
        factory,
        now_utc=datetime(2026, 8, 25, 20, 0),  # 23:00 MSK
    )

    gateway.create_daily_autopayment.assert_not_awaited()
    record_attempt.assert_awaited_once()


async def test_binding_payment_requests_saved_method(monkeypatch):
    gateway = _gateway()
    captured = {}

    def create_payment(request, idempotency_key):
        captured["request"] = request
        captured["idempotency_key"] = idempotency_key
        return SimpleNamespace(
            id="bind-payment",
            confirmation={"confirmation_url": "https://pay.example/bind"},
        )

    monkeypatch.setattr(yookassa_module.Payment, "create", create_payment)
    create_record = AsyncMock()
    monkeypatch.setattr(DailyTestAutopayPayment, "create", create_record)

    result = await gateway.create_autopay_binding(user_tg_id=123)

    request = captured["request"]
    assert result == "https://pay.example/bind"
    assert request.save_payment_method is True
    assert request.metadata["kind"] == AUTOPAY_BINDING_KIND
    assert str(request.amount.value) == "10.00"
    create_record.assert_awaited_once()


async def test_binding_reports_when_recurring_payments_are_disabled(monkeypatch):
    gateway = _gateway()

    def create_payment(request, idempotency_key):
        raise yookassa_module.ForbiddenError(
            {
                "type": "error",
                "description": "This store can't make recurring payments",
                "code": "forbidden",
            }
        )

    monkeypatch.setattr(yookassa_module.Payment, "create", create_payment)

    try:
        await gateway.create_autopay_binding(user_tg_id=123)
    except AutopayUnavailableError:
        pass
    else:
        raise AssertionError("Expected AutopayUnavailableError")


async def test_saved_method_token_is_stored_after_binding(monkeypatch):
    gateway = _gateway()
    autopay_payment = SimpleNamespace(
        payment_id="bind-payment",
        user_tg_id=123,
        kind=AUTOPAY_BINDING_KIND,
        status=TransactionStatus.PENDING.value,
    )
    monkeypatch.setattr(
        yookassa_module.Payment,
        "find_one",
        lambda _: SimpleNamespace(
            payment_method=SimpleNamespace(id="pm-token", saved=True)
        ),
    )
    save_binding = AsyncMock()
    set_status = AsyncMock()
    monkeypatch.setattr(DailyTestAutopayProfile, "save_binding", save_binding)
    monkeypatch.setattr(DailyTestAutopayPayment, "set_status", set_status)

    await gateway._process_autopay_succeeded(autopay_payment)

    assert save_binding.await_args.kwargs["payment_method_id"] == "pm-token"
    assert set_status.await_args.kwargs["status"] == TransactionStatus.COMPLETED
    gateway.services.notification.notify_by_id.assert_awaited_once()


async def test_daily_payment_uses_only_saved_method_token(monkeypatch):
    gateway = _gateway()
    profile = SimpleNamespace(
        user_tg_id=123,
        payment_method_id="pm-token",
        enabled=True,
    )
    captured = {}

    def create_payment(request, idempotency_key):
        captured["request"] = request
        return SimpleNamespace(
            id="daily-payment",
            status="pending",
            paid=False,
        )

    monkeypatch.setattr(yookassa_module.Payment, "create", create_payment)
    monkeypatch.setattr(
        DailyTestAutopayPayment,
        "get_for_day",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(DailyTestAutopayPayment, "create", AsyncMock())

    result = await gateway.create_daily_autopayment(
        profile=profile,
        scheduled_for=date(2026, 8, 25),
    )

    request = captured["request"]
    assert result == "daily-payment"
    assert request.payment_method_id == "pm-token"
    assert request.confirmation is None
    assert request.metadata["kind"] == AUTOPAY_DAILY_KIND
