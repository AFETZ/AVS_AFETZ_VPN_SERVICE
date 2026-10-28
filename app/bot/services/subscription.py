from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.bot.models import ClientData, Plan
    from app.bot.services import VPNService
    from app.bot.services.plan import PlanService

import math
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.services.subscription_state import client_data_from_user_snapshot
from app.bot.utils.public_links import subscription_base_url
from app.bot.utils.constants import Currency
from app.bot.utils.formatting import format_date, normalize_price
from app.bot.utils.time import get_current_timestamp
from app.config import Config
from app.db.models import Referral, Transaction, User

logger = logging.getLogger(__name__)


@dataclass
class SubscriptionStatus:
    user: User
    client_data: ClientData | None
    plan: Plan | None
    is_active: bool
    status_check_ok: bool
    period_duration_days: int | None
    expiry_timestamp: int | None

    @property
    def expiry_date(self) -> str:
        if self.expiry_timestamp is None:
            return ""
        return format_date(self.expiry_timestamp)

    @property
    def has_additional_profile(self) -> bool:
        return bool(
            self.is_active
            and self.plan
            and self.plan.includes_additional_profile
        )


@dataclass
class UpgradeQuote:
    current_plan: Plan
    target_plan: Plan
    price: float | int
    renewal_price: float | int
    renewal_duration_days: int
    expiry_timestamp: int

    @property
    def expiry_date(self) -> str:
        return format_date(self.expiry_timestamp)


class SubscriptionService:
    def __init__(
        self,
        config: Config,
        session_factory: async_sessionmaker,
        vpn_service: VPNService,
        plan_service: PlanService,
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self.vpn_service = vpn_service
        self.plan_service = plan_service
        logger.info("Subscription Service initialized")

    async def _get_fallback_period_duration_days(self, user: User) -> int | None:
        async with self.session_factory() as session:
            transaction = await Transaction.get_latest_completed_by_user(
                session=session,
                tg_id=user.tg_id,
            )

        if not transaction:
            return None

        try:
            from app.bot.models import SubscriptionData

            data = SubscriptionData.unpack(transaction.subscription)
        except Exception as exception:
            logger.warning(
                "Failed to unpack latest transaction for user %s while resolving period duration: %s",
                user.tg_id,
                exception,
            )
            return None

        return data.duration or None

    def _get_default_duration_days(self) -> int:
        durations = self.plan_service.get_durations()
        return min(durations) if durations else 30

    def _resolve_plan_from_user(self, user: User, client_data: ClientData | None) -> Plan | None:
        if user.current_plan_code:
            plan = self.plan_service.get_plan_by_code(user.current_plan_code)
            if plan and (
                client_data is None or plan.devices == client_data.max_devices_count
            ):
                return plan

        if client_data is None:
            return None

        return self.plan_service.get_plan(client_data.max_devices_count)

    async def get_subscription_status(self, user: User) -> SubscriptionStatus:
        client_data = client_data_from_user_snapshot(user, require_fresh=True)
        fallback_client_data = client_data_from_user_snapshot(user, require_fresh=False)
        status_check_ok = True

        if client_data is None and user.server_id:
            try:
                client_data = await self.vpn_service.get_client_data(user=user, raise_on_error=True)
            except Exception as exception:
                status_check_ok = False
                client_data = fallback_client_data
                logger.error(
                    "Failed to resolve active subscription state for user %s: %s",
                    user.tg_id,
                    exception,
                )

        plan = self._resolve_plan_from_user(user=user, client_data=client_data)
        is_active = bool(
            client_data
            and client_data.enabled
            and not client_data.has_subscription_expired
            and not getattr(user, "is_blocked", False)
        )
        expiry_timestamp = client_data.expiry_timestamp if client_data else None

        period_duration_days = user.current_period_duration_days
        if period_duration_days is None:
            period_duration_days = await self._get_fallback_period_duration_days(user)

        if period_duration_days is None and is_active and expiry_timestamp is not None:
            remaining_ms = max(expiry_timestamp - get_current_timestamp(), 0)
            period_duration_days = max(1, math.ceil(remaining_ms / 86_400_000))

        return SubscriptionStatus(
            user=user,
            client_data=client_data,
            plan=plan,
            is_active=is_active,
            status_check_ok=status_check_ok,
            period_duration_days=period_duration_days,
            expiry_timestamp=expiry_timestamp,
        )

    async def get_subscription_status_by_vpn_id(
        self,
        vpn_id: str,
    ) -> tuple[User | None, SubscriptionStatus | None]:
        async with self.session_factory() as session:
            user = await User.get_by_vpn_id(session=session, vpn_id=vpn_id)

        if not user:
            return None, None

        return user, await self.get_subscription_status(user)

    async def has_active_subscription(self, user: User) -> bool:
        status = await self.get_subscription_status(user)
        return status.status_check_ok and status.is_active

    async def has_additional_profile_access(self, user: User) -> bool:
        status = await self.get_subscription_status(user)
        return status.status_check_ok and status.has_additional_profile

    def _public_base_url(self) -> str:
        return subscription_base_url(self.config) or ""

    def get_additional_profile_url(self, user: User) -> str:
        return f"{self._public_base_url()}/wl/{user.vpn_id}"

    def get_filtered_additional_profile_url(self, user: User) -> str:
        return f"{self._public_base_url()}/wl-filtered/{user.vpn_id}"

    async def get_upstream_profile_url(self, user: User) -> str | None:
        return await self.vpn_service.get_upstream_key(user)

    async def get_upstream_profile_sources(self, user: User):
        return await self.vpn_service.get_upstream_profile_sources(user)

    def can_upgrade_plan(self, status: SubscriptionStatus) -> bool:
        if not status.is_active or not status.plan or status.plan.includes_additional_profile:
            return False

        return self.plan_service.get_upgrade_plan(status.plan) is not None

    def get_payment_plan(self, plan_code: str | None, devices: int) -> Plan | None:
        return self.plan_service.get_plan_by_code(plan_code) or self.plan_service.get_plan(devices)

    @staticmethod
    def apply_personal_discount(
        *,
        user: User,
        price: float | int,
        currency: Currency | str,
    ) -> float | int:
        discount_percent = max(
            0,
            min(int(getattr(user, "personal_discount_percent", 0) or 0), 95),
        )
        if discount_percent <= 0 or price <= 0:
            return price

        discounted_price = float(price) * (100 - discount_percent) / 100
        normalized_price = normalize_price(discounted_price, currency)
        if normalized_price <= 0:
            return 1
        return normalized_price

    def _get_supported_plan_durations(self, *plans: Plan) -> list[int]:
        durations: set[int] = set()
        default_durations = self.plan_service.get_durations()

        for plan in plans:
            durations.update(plan.get_available_durations(default_durations))

        return sorted(durations)

    async def _resolve_upgrade_duration_days(
        self,
        *,
        user: User,
        current_plan: Plan,
        target_plan: Plan,
        requested_duration_days: int | None,
    ) -> int:
        supported_durations = self._get_supported_plan_durations(current_plan, target_plan)
        if not supported_durations:
            return self._get_default_duration_days()

        if requested_duration_days in supported_durations:
            return requested_duration_days

        fallback_duration_days = await self._get_fallback_period_duration_days(user)
        if fallback_duration_days in supported_durations:
            logger.info(
                "Resolved unsupported upgrade duration for user %s via latest transaction: requested=%s resolved=%s",
                user.tg_id,
                requested_duration_days,
                fallback_duration_days,
            )
            return fallback_duration_days

        reference_duration = requested_duration_days or fallback_duration_days
        if reference_duration:
            resolved_duration = min(
                supported_durations,
                key=lambda duration: (abs(duration - reference_duration), duration),
            )
            logger.info(
                "Resolved unsupported upgrade duration for user %s via nearest plan duration: requested=%s resolved=%s",
                user.tg_id,
                requested_duration_days,
                resolved_duration,
            )
            return resolved_duration

        return supported_durations[0]

    def calculate_upgrade_price(
        self,
        current_plan: Plan,
        target_plan: Plan,
        *,
        duration_days: int,
        currency: Currency | str,
        remaining_seconds: float,
    ) -> float | int:
        current_price = current_plan.get_price(currency=currency, duration=duration_days)
        target_price = target_plan.get_price(currency=currency, duration=duration_days)
        price_difference = max(target_price - current_price, 0)
        full_period_seconds = duration_days * 86_400

        if remaining_seconds <= 0 or full_period_seconds <= 0:
            upgrade_price = target_price
        else:
            upgrade_price = price_difference * remaining_seconds / full_period_seconds
            upgrade_price = min(upgrade_price, price_difference)

        normalized_price = normalize_price(upgrade_price, currency)
        logger.info(
            "Calculated tariff upgrade price: current_plan=%s target_plan=%s duration_days=%s "
            "remaining_seconds=%.2f current_price=%s target_price=%s result=%s",
            current_plan.code,
            target_plan.code,
            duration_days,
            remaining_seconds,
            current_price,
            target_price,
            normalized_price,
        )
        return normalized_price

    def calculate_plan_change_price(
        self,
        current_plan: Plan,
        target_plan: Plan,
        *,
        duration_days: int,
        currency: Currency | str,
        remaining_seconds: float,
    ) -> float | int:
        """Calculate price for plan change with period reset.

        Formula: target_full_price - credit_for_unused_old_plan.
        The subscription period resets to a full new period.
        """
        current_price = current_plan.get_price(currency=currency, duration=duration_days)
        target_price = target_plan.get_price(currency=currency, duration=duration_days)
        full_period_seconds = duration_days * 86_400

        if remaining_seconds <= 0 or full_period_seconds <= 0:
            charge = target_price
        else:
            credit = current_price * remaining_seconds / full_period_seconds
            charge = max(target_price - credit, 0)

        normalized_price = normalize_price(charge, currency)
        logger.info(
            "Calculated plan change price: current_plan=%s target_plan=%s duration_days=%s "
            "remaining_seconds=%.2f current_price=%s target_price=%s credit=%.2f result=%s",
            current_plan.code,
            target_plan.code,
            duration_days,
            remaining_seconds,
            current_price,
            target_price,
            current_price * remaining_seconds / full_period_seconds if full_period_seconds > 0 else 0,
            normalized_price,
        )
        return normalized_price

    async def get_upgrade_quote(
        self,
        user: User,
        *,
        currency: Currency | str,
        target_plan: "Plan | None" = None,
        reset_period: bool = False,
    ) -> UpgradeQuote | None:
        status = await self.get_subscription_status(user)
        if not status.status_check_ok or not status.is_active or not status.plan:
            return None

        if target_plan is None:
            target_plan = self.plan_service.get_upgrade_plan(status.plan)
        if not target_plan:
            return None

        if status.expiry_timestamp is None:
            return None

        duration_days = await self._resolve_upgrade_duration_days(
            user=user,
            current_plan=status.plan,
            target_plan=target_plan,
            requested_duration_days=status.period_duration_days,
        )
        remaining_seconds = max((status.expiry_timestamp - get_current_timestamp()) / 1000, 0)

        if reset_period:
            price = self.calculate_plan_change_price(
                current_plan=status.plan,
                target_plan=target_plan,
                duration_days=duration_days,
                currency=currency,
                remaining_seconds=remaining_seconds,
            )
        else:
            price = self.calculate_upgrade_price(
                current_plan=status.plan,
                target_plan=target_plan,
                duration_days=duration_days,
                currency=currency,
                remaining_seconds=remaining_seconds,
            )

        renewal_price = normalize_price(
            target_plan.get_price(currency=currency, duration=duration_days),
            currency,
        )

        return UpgradeQuote(
            current_plan=status.plan,
            target_plan=target_plan,
            price=price,
            renewal_price=renewal_price,
            renewal_duration_days=duration_days,
            expiry_timestamp=status.expiry_timestamp,
        )

    async def get_plan_change_quotes(
        self,
        user: User,
        *,
        currency: Currency | str,
    ) -> list[UpgradeQuote]:
        status = await self.get_subscription_status(user)
        if not status.status_check_ok or not status.is_active or not status.plan:
            return []
        if status.expiry_timestamp is None:
            return []

        duration_days = await self._resolve_upgrade_duration_days(
            user=user,
            current_plan=status.plan,
            target_plan=status.plan,
            requested_duration_days=status.period_duration_days,
        )

        available_plans = self.plan_service.get_plan_changes(
            current_plan=status.plan,
            duration=duration_days,
            currency=Currency.from_code(currency).code if isinstance(currency, str) else currency.code,
        )

        quotes: list[UpgradeQuote] = []
        for target in available_plans:
            quote = await self.get_upgrade_quote(
                user=user,
                currency=currency,
                target_plan=target,
            )
            if quote:
                quotes.append(quote)

        return quotes

    async def update_current_plan(
        self,
        user: User,
        plan_code: str,
        *,
        refresh_period: bool,
        period_duration_days: int | None = None,
        clear_pending: bool = True,
    ) -> None:
        updates: dict[str, object] = {"current_plan_code": plan_code}
        if clear_pending:
            updates.update(
                {
                    "pending_plan_code": None,
                    "pending_period_duration_days": None,
                    "pending_plan_starts_at": None,
                }
            )

        if refresh_period:
            try:
                client_data = await self.vpn_service.get_client_data(
                    user=user,
                    raise_on_error=True,
                )
            except Exception as exception:
                logger.warning(
                    "Failed to refresh billing period metadata for user %s after plan update: %s",
                    user.tg_id,
                    exception,
                )
                client_data = None

            if client_data and client_data.expiry_timestamp != -1:
                updates["current_period_started_at"] = datetime.now(timezone.utc)
                updates["current_period_duration_days"] = (
                    period_duration_days
                    if period_duration_days is not None
                    else max(
                        1,
                        math.ceil(
                            max(client_data.expiry_timestamp - get_current_timestamp(), 0)
                            / 86_400_000
                        ),
                    )
                )
            else:
                updates["current_period_started_at"] = None
                updates["current_period_duration_days"] = None

        async with self.session_factory() as session:
            await User.update(session=session, tg_id=user.tg_id, **updates)

        user.current_plan_code = plan_code
        if clear_pending:
            user.pending_plan_code = None
            user.pending_period_duration_days = None
            user.pending_plan_starts_at = None
        if "current_period_started_at" in updates:
            user.current_period_started_at = updates["current_period_started_at"]  # type: ignore[assignment]
            user.current_period_duration_days = updates["current_period_duration_days"]  # type: ignore[assignment]

    async def schedule_next_plan(
        self,
        user: User,
        *,
        plan_code: str,
        period_duration_days: int,
        starts_at: int | None,
    ) -> None:
        updates = {
            "pending_plan_code": plan_code,
            "pending_period_duration_days": period_duration_days,
            "pending_plan_starts_at": starts_at,
        }
        async with self.session_factory() as session:
            await User.update(session=session, tg_id=user.tg_id, **updates)

        user.pending_plan_code = plan_code
        user.pending_period_duration_days = period_duration_days
        user.pending_plan_starts_at = starts_at
        logger.info(
            "Scheduled next tariff for user %s: plan=%s duration_days=%s starts_at=%s",
            user.tg_id,
            plan_code,
            period_duration_days,
            starts_at,
        )

    async def clear_scheduled_plan(self, user: User) -> None:
        updates = {
            "pending_plan_code": None,
            "pending_period_duration_days": None,
            "pending_plan_starts_at": None,
        }
        async with self.session_factory() as session:
            await User.update(session=session, tg_id=user.tg_id, **updates)

        user.pending_plan_code = None
        user.pending_period_duration_days = None
        user.pending_plan_starts_at = None

    async def is_trial_available(self, user: User) -> bool:
        is_first_check_ok = (
            self.config.shop.TRIAL_ENABLED and not user.server_id and not user.is_trial_used
        )

        if not is_first_check_ok:
            return False

        async with self.session_factory() as session:
            referral = await Referral.get_referral(session, user.tg_id)

        return not referral or (referral and not self.config.shop.REFERRED_TRIAL_ENABLED)

    async def gift_trial(self, user: User) -> bool:
        if not await self.is_trial_available(user=user):
            logger.warning(
                f"Failed to activate trial for user {user.tg_id}. Trial period is not available."
            )
            return False

        async with self.session_factory() as session:
            trial_used = await User.update_trial_status(
                session=session, tg_id=user.tg_id, used=True
            )

        if not trial_used:
            logger.critical(f"Failed to activate trial for user {user.tg_id}.")
            return False

        trial_devices = self.config.shop.TRIAL_DEVICES_COUNT
        trial_plan_code = self.config.shop.TRIAL_PLAN_CODE

        logger.info(f"Begun giving trial period for user {user.tg_id}.")
        try:
            trial_success = await self.vpn_service.process_bonus_days(
                user,
                duration=self.config.shop.TRIAL_PERIOD,
                devices=trial_devices,
            )
        except Exception as exception:
            logger.exception(
                f"Unexpected error while giving trial to user {user.tg_id}: {exception}"
            )
            async with self.session_factory() as session:
                await User.update_trial_status(session=session, tg_id=user.tg_id, used=False)
            return False

        if trial_success:
            if trial_plan_code:
                await self.update_current_plan(
                    user=user,
                    plan_code=trial_plan_code,
                    refresh_period=True,
                    period_duration_days=self.config.shop.TRIAL_PERIOD,
                )
            logger.info(
                f"Successfully gave {self.config.shop.TRIAL_PERIOD} days to a user {user.tg_id}"
            )
            return True

        async with self.session_factory() as session:
            await User.update_trial_status(session=session, tg_id=user.tg_id, used=False)

        logger.warning(f"Failed to apply trial period for user {user.tg_id} due to failure.")
        return False
