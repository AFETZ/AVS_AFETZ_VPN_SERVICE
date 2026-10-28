from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server_pool import Connection, ServerPoolService

import asyncio
import logging
import math
from copy import deepcopy
from dataclasses import dataclass, field

from py3xui import Client, Inbound
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.models import ClientData
from app.bot.services.subscription_state import (
    SUBSCRIPTION_SYNC_STATUS_ERROR,
    SUBSCRIPTION_SYNC_STATUS_OK,
    client_data_snapshot_updates,
)
from app.bot.services.xui_gateway import XuiGateway
from app.bot.utils.public_links import (
    subscription_backup_base_url,
    subscription_base_url,
)
from app.bot.utils.network import extract_base_url
from app.bot.utils.time import (
    add_days_to_timestamp,
    days_to_timestamp,
    get_current_timestamp,
)
from app.config import Config
from app.db.models import Promocode, Server, User

logger = logging.getLogger(__name__)


def _redact_identifier(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


@dataclass(frozen=True)
class ServerSwitchResult:
    success: bool
    reason: str
    server: Server | None = None


@dataclass(frozen=True)
class UpstreamProfileSource:
    server: Server
    url: str


@dataclass
class InboundCache:
    inbounds_by_server_id: dict[int, list[Inbound] | None] = field(default_factory=dict)
    locks_by_server_id: dict[int, asyncio.Lock] = field(default_factory=dict)


class VPNService:
    def __init__(
        self,
        config: Config,
        session: async_sessionmaker,
        server_pool_service: ServerPoolService,
    ) -> None:
        self.config = config
        self.session = session
        self.server_pool_service = server_pool_service
        self.xui_gateway = XuiGateway()
        logger.info("VPN Service initialized.")

    @staticmethod
    def _is_client_not_found_error(exception: Exception) -> bool:
        error_text = str(exception).lower()
        return (
            "inbound not found for email" in error_text
            or "not found for email" in error_text
            or "error getting traffics" in error_text
        )

    @staticmethod
    def _resolve_client_flow(inbound: Inbound | None) -> str:
        if not inbound or not inbound.stream_settings:
            return ""

        stream_settings = inbound.stream_settings
        network = getattr(stream_settings, "network", None)
        security = getattr(stream_settings, "security", None)

        if network is None and hasattr(stream_settings, "get"):
            network = stream_settings.get("network")
        if security is None and hasattr(stream_settings, "get"):
            security = stream_settings.get("security")

        if network == "tcp" and security in {"reality", "tls"}:
            return "xtls-rprx-vision"
        return ""

    async def _persist_subscription_snapshot(
        self,
        user: User,
        client_data: ClientData | None,
        *,
        status: str,
    ) -> None:
        updates = client_data_snapshot_updates(client_data=client_data, status=status)
        try:
            async with self.session() as session:
                await User.update(session=session, tg_id=user.tg_id, **updates)
        except Exception as exception:
            logger.warning(
                "Failed to persist local subscription snapshot for user %s: %s",
                user.tg_id,
                exception,
            )
            return

        for key, value in updates.items():
            try:
                setattr(user, key, value)
            except Exception:
                pass

    async def _mark_subscription_sync_failed(self, user: User, exception: Exception) -> None:
        updates = {"subscription_sync_status": SUBSCRIPTION_SYNC_STATUS_ERROR}
        try:
            async with self.session() as session:
                await User.update(session=session, tg_id=user.tg_id, **updates)
        except Exception:
            logger.debug(
                "Failed to mark local subscription sync error for user %s.",
                user.tg_id,
                exc_info=True,
            )
        logger.warning(
            "Subscription snapshot sync failed for user %s: %s",
            user.tg_id,
            exception,
        )

    async def refresh_subscription_snapshot(self, user: User) -> ClientData | None:
        return await self.get_client_data(user=user, raise_on_error=False)

    async def is_client_exists(self, user: User) -> Client | None:
        connection = await self.server_pool_service.get_connection(user)

        if not connection:
            return None

        try:
            client = await self.xui_gateway.get_client_by_email(connection, str(user.tg_id))
        except Exception as exception:
            logger.error(f"Error checking client {user.tg_id} on server {connection.server.name}: {exception}")
            return None

        if client:
            logger.debug(f"Client {user.tg_id} exists on server {connection.server.name}.")
        else:
            logger.critical(f"Client {user.tg_id} not found on server {connection.server.name}.")

        return client

    async def _fetch_inbounds_from_connection(
        self,
        connection: "Connection",
    ) -> list[Inbound] | None:
        try:
            return await self.xui_gateway.get_inbound_list(connection)
        except Exception as exception:
            logger.error(f"Failed to fetch inbounds: {exception}")
            return None

    async def _get_inbounds_from_connection(
        self,
        connection: "Connection",
        inbound_cache: InboundCache | None = None,
    ) -> list[Inbound] | None:
        server_id = getattr(connection.server, "id", None)
        if inbound_cache is None or server_id is None:
            return await self._fetch_inbounds_from_connection(connection)

        if server_id in inbound_cache.inbounds_by_server_id:
            return inbound_cache.inbounds_by_server_id[server_id]

        lock = inbound_cache.locks_by_server_id.setdefault(server_id, asyncio.Lock())
        async with lock:
            if server_id not in inbound_cache.inbounds_by_server_id:
                inbound_cache.inbounds_by_server_id[server_id] = (
                    await self._fetch_inbounds_from_connection(connection)
                )
            return inbound_cache.inbounds_by_server_id[server_id]

    async def _get_limit_ip_from_connection(
        self,
        connection: "Connection",
        client: Client | None,
        inbound_cache: InboundCache | None = None,
    ) -> int | None:
        if client is None:
            logger.warning("Cannot resolve limit_ip: client is missing.")
            return None

        inbounds = await self._get_inbounds_from_connection(
            connection=connection,
            inbound_cache=inbound_cache,
        )
        if inbounds is None:
            return None

        for inbound in inbounds:
            for inbound_client in inbound.settings.clients:
                if inbound_client.email == client.email:
                    logger.debug(f"Client {client.email} limit ip: {inbound_client.limit_ip}")
                    return inbound_client.limit_ip

        logger.critical(f"Client {client.email} not found in inbounds.")
        return None

    async def get_limit_ip(self, user: User, client: Client | None) -> int | None:
        connection = await self.server_pool_service.get_connection(user)

        if not connection:
            return None

        if client is None:
            logger.warning(f"Cannot resolve limit_ip for user {user.tg_id}: client is missing.")
            return None

        return await self._get_limit_ip_from_connection(connection=connection, client=client)

    async def _get_client_data_from_connection(
        self,
        user: User,
        connection: "Connection",
        inbound_cache: InboundCache | None = None,
    ) -> ClientData | None:
        try:
            client = await self.xui_gateway.get_client_by_email(connection, str(user.tg_id))

            if not client:
                logger.critical(
                    f"Client {user.tg_id} not found on server {connection.server.name}."
                )
                return None

            limit_ip = await self._get_limit_ip_from_connection(
                connection=connection,
                client=client,
                inbound_cache=inbound_cache,
            )
            if limit_ip is None:
                logger.warning(
                    "Client %s exists but limit_ip was not found in inbounds.",
                    user.tg_id,
                )
                raise RuntimeError(
                    f"Failed to resolve device limit for user {user.tg_id}."
                )

            max_devices = -1 if limit_ip == 0 else limit_ip
            traffic_total = client.total
            expiry_time = -1 if client.expiry_time == 0 else client.expiry_time

            if traffic_total <= 0:
                traffic_remaining = -1
                traffic_total = -1
            else:
                traffic_remaining = client.total - (client.up + client.down)

            traffic_used = client.up + client.down
            client_data = ClientData(
                max_devices=max_devices,
                traffic_total=traffic_total,
                traffic_remaining=traffic_remaining,
                traffic_used=traffic_used,
                traffic_up=client.up,
                traffic_down=client.down,
                expiry_time=expiry_time,
                enabled=bool(client.enable),
            )
            logger.debug(f"Successfully retrieved client data for {user.tg_id}: {client_data}.")
            return client_data
        except Exception as exception:
            if self._is_client_not_found_error(exception):
                logger.info(
                    "Client %s not found in 3x-ui inbounds. Treating as no active subscription.",
                    user.tg_id,
                )
                return None
            raise RuntimeError(
                f"Failed to retrieve client data for user {user.tg_id} "
                f"from server {connection.server.name}."
            ) from exception

    async def _get_client_data(
        self,
        user: User,
        *,
        raise_on_error: bool,
        inbound_cache: InboundCache | None = None,
    ) -> ClientData | None:
        logger.debug(f"Starting to retrieve client data for {user.tg_id}.")

        assigned_connection = await self.server_pool_service.get_connection(user)
        connections: list["Connection"] = []
        if assigned_connection:
            connections.append(assigned_connection)

        get_profile_connections = getattr(
            self.server_pool_service,
            "get_profile_connections",
            None,
        )
        if get_profile_connections:
            try:
                profile_connections = await get_profile_connections()
            except Exception as exception:
                logger.warning(
                    "Failed to get fallback profile connections for user %s: %s",
                    user.tg_id,
                    exception,
                )
            else:
                seen_server_ids = {
                    getattr(connection.server, "id", None) for connection in connections
                }
                connections.extend(
                    connection
                    for connection in profile_connections
                    if getattr(connection.server, "id", None) not in seen_server_ids
                )

        if not connections:
            if raise_on_error and user.server_id:
                raise RuntimeError(f"Connection for user {user.tg_id} is unavailable.")
            return None

        async def fetch_client_data(
            connection: "Connection",
        ) -> tuple["Connection", ClientData | None, Exception | None]:
            try:
                client_data = await self._get_client_data_from_connection(
                    user=user,
                    connection=connection,
                    inbound_cache=inbound_cache,
                )
            except Exception as exception:
                logger.warning(
                    "Failed to retrieve client data for user %s from server %s: %s",
                    user.tg_id,
                    connection.server.name,
                    exception,
                )
                return connection, None, exception
            return connection, client_data, None

        errors: list[Exception] = []
        for connection in connections:
            connection, client_data, exception = await fetch_client_data(connection)
            if exception:
                errors.append(exception)
                continue

            if client_data is None:
                continue

            if (
                assigned_connection
                and connection.server.id != assigned_connection.server.id
            ):
                logger.debug(
                    "Using server %s as subscription status fallback for user %s "
                    "instead of assigned server %s.",
                    connection.server.name,
                    user.tg_id,
                    assigned_connection.server.name,
                )
            return client_data

        if errors:
            if raise_on_error:
                raise RuntimeError(
                    f"Failed to retrieve client data for user {user.tg_id} "
                    "from all available servers."
                ) from errors[-1]
            logger.error(
                "Error retrieving client data for %s from all available servers.",
                user.tg_id,
            )

        return None

    async def get_client_data(
        self,
        user: User,
        raise_on_error: bool = False,
        inbound_cache: InboundCache | None = None,
    ) -> ClientData | None:
        try:
            client_data = await self._get_client_data(
                user=user,
                raise_on_error=raise_on_error,
                inbound_cache=inbound_cache,
            )
        except Exception as exception:
            await self._mark_subscription_sync_failed(user=user, exception=exception)
            raise

        if getattr(user, "server_id", None) and client_data is not None:
            await self._persist_subscription_snapshot(
                user=user,
                client_data=client_data,
                status=SUBSCRIPTION_SYNC_STATUS_OK,
            )

        return client_data

    def get_upstream_key_for_server(self, user: User, server: Server | None) -> str | None:
        if not server:
            return None

        scheme = (
            self.config.xui.SUBSCRIPTION_SCHEME
            if self.config.xui.SUBSCRIPTION_SCHEME
            and server.host.startswith("http://")
            else None
        )
        subscription = extract_base_url(
            url=server.host,
            port=self.config.xui.SUBSCRIPTION_PORT,
            path=self.config.xui.SUBSCRIPTION_PATH,
            scheme=scheme,
        )
        return f"{subscription}{user.vpn_id}"

    async def _get_profile_connections_for_user(self, user: User) -> list["Connection"]:
        get_profile_connections = getattr(
            self.server_pool_service,
            "get_profile_connections",
            None,
        )
        connections = await get_profile_connections() if get_profile_connections else []

        if user.server_id and all(
            connection.server.id != user.server_id for connection in connections
        ):
            current_connection = await self.server_pool_service.get_connection(user)
            if current_connection:
                connections.append(current_connection)

        unique_connections: dict[int, "Connection"] = {}
        for connection in connections:
            unique_connections[connection.server.id] = connection

        return list(unique_connections.values())

    async def get_upstream_profile_sources(self, user: User) -> list[UpstreamProfileSource]:
        async with self.session() as session:
            user = await User.get(session=session, tg_id=user.tg_id)

        if not user:
            return []

        get_profile_servers = getattr(self.server_pool_service, "get_profile_servers", None)
        servers: list[Server] = await get_profile_servers() if get_profile_servers else []

        if (
            user.server_id
            and user.server
            and all(server.id != user.server_id for server in servers)
        ):
            servers = [*servers, user.server]

        if not servers:
            connections = await self._get_profile_connections_for_user(user)
            servers = [connection.server for connection in connections]

        sources = []
        seen_server_ids: set[int | None] = set()
        for server in servers:
            server_id = getattr(server, "id", None)
            if server_id in seen_server_ids:
                continue
            seen_server_ids.add(server_id)
            key = self.get_upstream_key_for_server(user=user, server=server)
            if key:
                sources.append(UpstreamProfileSource(server=server, url=key))

        return sources

    async def get_upstream_key(self, user: User) -> str | None:
        async with self.session() as session:
            user = await User.get(session=session, tg_id=user.tg_id)

        if not user or not user.server_id:
            logger.debug(f"Server ID for user {getattr(user, 'tg_id', '?')} not found.")
            return None

        key = self.get_upstream_key_for_server(user=user, server=user.server)
        if not key:
            logger.debug(f"Server for user {user.tg_id} not found.")
            return None

        logger.debug(
            "Fetched upstream key for user %s on server %s (vpn_id=%s).",
            user.tg_id,
            getattr(user.server, "name", "?") if user.server else "?",
            _redact_identifier(user.vpn_id),
        )
        return key

    async def switch_server(self, user: User, server_id: int) -> ServerSwitchResult:
        if user.server_id == server_id:
            return ServerSwitchResult(
                success=False,
                reason="already_selected",
                server=user.server,
            )

        connection = await self.server_pool_service.get_connection_by_server_id(server_id)
        if not connection or not connection.server.online:
            return ServerSwitchResult(success=False, reason="unavailable")

        try:
            client = await self.xui_gateway.get_client_by_email(connection, str(user.tg_id))
        except Exception as exception:
            logger.error(
                "Failed to check client %s on server %s: %s",
                user.tg_id,
                connection.server.name,
                exception,
            )
            return ServerSwitchResult(
                success=False,
                reason="unavailable",
                server=connection.server,
            )

        if not client:
            logger.warning(
                "Cannot switch user %s to server %s: client not found.",
                user.tg_id,
                connection.server.name,
            )
            return ServerSwitchResult(
                success=False,
                reason="client_missing",
                server=connection.server,
            )

        async with self.session() as session:
            await User.update(session=session, tg_id=user.tg_id, server_id=server_id)

        user.server_id = server_id
        user.server = connection.server
        logger.info("User %s switched to server %s.", user.tg_id, connection.server.name)
        return ServerSwitchResult(
            success=True,
            reason="switched",
            server=connection.server,
        )

    async def get_key(self, user: User) -> str | None:
        async with self.session() as session:
            user = await User.get(session=session, tg_id=user.tg_id)

        if not user.server_id:
            logger.debug(f"Server ID for user {user.tg_id} not found.")
            return None

        domain = subscription_base_url(self.config)
        if not domain:
            return None
        key = f"{domain}/sub/{user.vpn_id}"
        logger.debug(
            "Fetched public subscription URL for user %s (vpn_id=%s).",
            user.tg_id,
            _redact_identifier(user.vpn_id),
        )
        return key

    async def get_backup_key(self, user: User) -> str | None:
        async with self.session() as session:
            user = await User.get(session=session, tg_id=user.tg_id)

        if not user.server_id:
            return None

        domain = subscription_backup_base_url(self.config)
        primary_domain = subscription_base_url(self.config)
        if not domain or domain == primary_domain:
            return None
        return f"{domain}/sub/{user.vpn_id}"

    async def _remaining_duration_days(self, user: User) -> int:
        client_data = await self.get_client_data(user=user, raise_on_error=False)
        if not client_data or client_data.expiry_timestamp in (None, -1):
            return 0

        remaining_ms = client_data.expiry_timestamp - get_current_timestamp()
        if remaining_ms <= 0:
            return 0

        return max(1, math.ceil(remaining_ms / 86_400_000))

    async def _subscription_connections(self, user: User) -> list["Connection"]:
        if not user.server_id:
            await self.server_pool_service.assign_server_to_user(user)

        connections = await self._get_profile_connections_for_user(user)
        if connections:
            return connections

        connection = await self.server_pool_service.get_connection(user)
        return [connection] if connection else []

    async def _create_client_on_connection(
        self,
        connection: "Connection",
        user: User,
        devices: int,
        duration: int,
        enable: bool = True,
        flow: str | None = None,
        total_gb: int = 0,
    ) -> bool:
        try:
            inbounds = await self.xui_gateway.get_inbound_list(connection)
        except Exception as exception:
            logger.error(
                "Failed to fetch inbound for server %s before creating client %s: %s",
                connection.server.name,
                user.tg_id,
                exception,
            )
            return False
        inbound = inbounds[0] if inbounds else None
        if not inbound:
            return False

        if flow is None:
            flow = self._resolve_client_flow(inbound)

        new_client = Client(
            email=str(user.tg_id),
            enable=enable,
            id=user.vpn_id,
            expiry_time=days_to_timestamp(duration),
            flow=flow,
            limit_ip=devices,
            sub_id=user.vpn_id,
            total_gb=total_gb,
        )

        try:
            await self.xui_gateway.add_client(
                connection,
                inbound_id=inbound.id,
                clients=[new_client],
            )
            logger.info(
                "Successfully created client %s on server %s.",
                user.tg_id,
                connection.server.name,
            )
            return True
        except Exception as exception:
            logger.error(
                "Error creating client %s on server %s: %s",
                user.tg_id,
                connection.server.name,
                exception,
            )
            return False

    async def _update_client_on_connection(
        self,
        connection: "Connection",
        user: User,
        client: Client,
        devices: int,
        duration: int,
        replace_devices: bool = False,
        replace_duration: bool = False,
        enable: bool = True,
        flow: str | None = None,
        total_gb: int = 0,
    ) -> bool:
        if not replace_devices:
            current_device_limit = await self._get_limit_ip_from_connection(
                connection=connection,
                client=client,
            )
            if current_device_limit is None:
                logger.error(
                    "Cannot update client %s on server %s: failed to resolve current device limit.",
                    user.tg_id,
                    connection.server.name,
                )
                return False
            devices = current_device_limit + devices

        current_time = get_current_timestamp()

        if not replace_duration:
            expiry_time_to_use = max(client.expiry_time, current_time)
        else:
            expiry_time_to_use = current_time

        expiry_time = (
            0 if client.expiry_time == 0 and not replace_duration
            else add_days_to_timestamp(timestamp=expiry_time_to_use, days=duration)
        )
        client.enable = enable
        client.id = user.vpn_id
        client.expiry_time = expiry_time
        client.flow = client.flow if flow is None else flow
        client.limit_ip = devices
        client.sub_id = user.vpn_id
        client.total_gb = total_gb

        try:
            await self.xui_gateway.update_client(
                connection,
                client_uuid=client.id,
                client=client,
            )
            logger.info(
                "Client %s updated successfully on server %s.",
                user.tg_id,
                connection.server.name,
            )
            return True
        except Exception as exception:
            logger.error(
                "Error updating client %s on server %s: %s",
                user.tg_id,
                connection.server.name,
                exception,
            )
            return False

    async def create_client(
        self,
        user: User,
        devices: int,
        duration: int,
        enable: bool = True,
        flow: str | None = None,
        total_gb: int = 0,
        inbound_id: int = 1,
    ) -> bool:
        logger.info(f"Creating new client {user.tg_id} | {devices} devices {duration} days.")

        _ = inbound_id
        connections = await self._subscription_connections(user)
        if not connections:
            return False

        results: dict[int, bool] = {}
        for connection in connections:
            try:
                existing_client = await self.xui_gateway.get_client_by_email(
                    connection,
                    str(user.tg_id),
                )
            except Exception as exception:
                if self._is_client_not_found_error(exception):
                    existing_client = None
                else:
                    logger.error(
                        "Failed to check client %s on server %s before create: %s",
                        user.tg_id,
                        connection.server.name,
                        exception,
                    )
                    results[connection.server.id] = False
                    continue

            if existing_client:
                results[connection.server.id] = await self._update_client_on_connection(
                    connection=connection,
                    user=user,
                    client=existing_client,
                    devices=devices,
                    duration=duration,
                    replace_devices=True,
                    replace_duration=True,
                    enable=enable,
                    flow=flow,
                    total_gb=total_gb,
                )
                continue

            results[connection.server.id] = await self._create_client_on_connection(
                connection=connection,
                user=user,
                devices=devices,
                duration=duration,
                enable=enable,
                flow=flow,
                total_gb=total_gb,
            )

        required_result = results.get(user.server_id)
        success = required_result if required_result is not None else any(results.values())
        if success:
            await self.refresh_subscription_snapshot(user)
        return success

    async def update_client(
        self,
        user: User,
        devices: int,
        duration: int,
        replace_devices: bool = False,
        replace_duration: bool = False,
        enable: bool = True,
        flow: str | None = None,
        total_gb: int = 0,
    ) -> bool:
        logger.info(f"Updating client {user.tg_id} | {devices} devices {duration} days.")
        connections = await self._subscription_connections(user)
        if not connections:
            return False

        create_duration = duration
        if create_duration <= 0:
            create_duration = await self._remaining_duration_days(user)

        create_devices = devices
        if not replace_devices or create_devices <= 0:
            current_client_data = await self.get_client_data(user=user, raise_on_error=False)
            if current_client_data:
                create_devices = current_client_data.max_devices_count
                if create_devices == -1:
                    create_devices = 0

        results: dict[int, bool] = {}
        for connection in connections:
            try:
                client = await self.xui_gateway.get_client_by_email(
                    connection,
                    str(user.tg_id),
                )
            except Exception as exception:
                if self._is_client_not_found_error(exception):
                    client = None
                else:
                    logger.error(
                        "Error fetching client %s on server %s: %s",
                        user.tg_id,
                        connection.server.name,
                        exception,
                    )
                    results[connection.server.id] = False
                    continue

            if client is None:
                if create_duration <= 0:
                    logger.critical(
                        "Client %s not found for update on server %s.",
                        user.tg_id,
                        connection.server.name,
                    )
                    results[connection.server.id] = False
                    continue

                results[connection.server.id] = await self._create_client_on_connection(
                    connection=connection,
                    user=user,
                    devices=create_devices,
                    duration=create_duration,
                    enable=enable,
                    flow=flow,
                    total_gb=total_gb,
                )
                continue

            results[connection.server.id] = await self._update_client_on_connection(
                connection=connection,
                user=user,
                client=client,
                devices=devices,
                duration=duration,
                replace_devices=replace_devices,
                replace_duration=replace_duration,
                enable=enable,
                flow=flow,
                total_gb=total_gb,
            )

        required_result = results.get(user.server_id)
        success = required_result if required_result is not None else any(results.values())
        if success:
            await self.refresh_subscription_snapshot(user)
        return success

    async def create_subscription(self, user: User, devices: int, duration: int) -> bool:
        if await self.is_client_exists(user):
            return await self.update_client(
                user=user,
                devices=devices,
                duration=duration,
                replace_devices=True,
                replace_duration=True,
            )
        return await self.create_client(user=user, devices=devices, duration=duration)

    async def extend_subscription(self, user: User, devices: int, duration: int) -> bool:
        return await self.update_client(
            user=user,
            devices=devices,
            duration=duration,
            replace_devices=True,
        )

    async def change_subscription(self, user: User, devices: int) -> bool:
        if await self.is_client_exists(user):
            return await self.update_client(
                user,
                devices,
                0,
                replace_devices=True,
                replace_duration=False,
            )
        return False

    async def set_client_enabled(self, user: User, enabled: bool) -> bool:
        logger.info("Setting client %s enabled=%s.", user.tg_id, enabled)
        connection = await self.server_pool_service.get_connection(user)

        if not connection:
            logger.warning("Cannot set enabled=%s for user %s: no server connection.", enabled, user.tg_id)
            return False


        try:
            client = await self.xui_gateway.get_client_by_email(connection, str(user.tg_id))
            if client is None:
                logger.warning("Cannot set enabled=%s for user %s: client not found.", enabled, user.tg_id)
                return False

            client.enable = enabled
            await self.xui_gateway.update_client(
                connection,
                client_uuid=client.id,
                client=client,
            )
            logger.info("Client %s enabled set to %s.", user.tg_id, enabled)
            await self.refresh_subscription_snapshot(user)
            return True
        except Exception as exception:
            logger.error("Failed to set client %s enabled=%s: %s", user.tg_id, enabled, exception)
            return False

    async def set_lifetime_subscription(self, user: User, enabled: bool) -> bool:
        """Set unlimited expiry on every profile node; keep device/traffic limits.

        Read all nodes before changing any. Restore previous clients on a failed
        write, and never report a partially applied change as successful.
        """
        if enabled and getattr(user, "is_blocked", False):
            return False
        sources = await self.get_upstream_profile_sources(user)
        if not sources:
            return False
        originals = []
        attempted = []
        try:
            for source in sources:
                connection = await self.server_pool_service.get_connection_by_server_id(source.server.id)
                if not connection:
                    raise RuntimeError("Subscription server is unavailable")
                inbounds = await self.xui_gateway.get_inbound_list(connection)
                matches = []
                for inbound in inbounds or []:
                    for existing in inbound.settings.clients:
                        if existing.email == str(user.tg_id):
                            client = deepcopy(existing)
                            client.inbound_id = inbound.id
                            matches.append(client)
                if not matches:
                    raise RuntimeError("Create a subscription on all profile servers first")
                originals.extend((connection, deepcopy(client)) for client in matches)
            for connection, original in originals:
                client = deepcopy(original)
                client.expiry_time = 0
                client.enable = enabled
                attempted.append((connection, original))
                await self.xui_gateway.update_client(connection, client_uuid=client.id, client=client)
        except Exception:
            logger.exception("Lifetime subscription update failed for user %s", user.tg_id)
            for connection, original in reversed(attempted):
                try:
                    await self.xui_gateway.update_client(connection, client_uuid=original.id, client=original)
                except Exception:
                    logger.exception("Lifetime rollback failed for user %s on server %s", user.tg_id, connection.server.id)
            return False
        await self.refresh_subscription_snapshot(user)
        return True

    async def process_bonus_days(self, user: User, duration: int, devices: int) -> bool:
        if await self.is_client_exists(user):
            updated = await self.update_client(user=user, devices=0, duration=duration)
            if updated:
                logger.info(f"Updated client {user.tg_id} with additional {duration} days(-s).")
                return True
        else:
            created = await self.create_client(user=user, devices=devices, duration=duration)
            if created:
                logger.info(f"Created client {user.tg_id} with additional {duration} days(-s)")
                return True

        return False

    async def activate_promocode(self, user: User, promocode: Promocode) -> bool:
        # TODO: consider moving to some 'promocode module services' with usage of vpn-service methods.

        async with self.session() as session:
            activated = await Promocode.set_activated(
                session=session,
                code=promocode.code,
                user_id=user.tg_id,
            )

        if not activated:
            logger.critical(f"Failed to activate promocode {promocode.code} for user {user.tg_id}.")
            return False

        logger.info(f"Begun applying promocode ({promocode.code}) to a client {user.tg_id}.")
        success = await self.process_bonus_days(
            user,
            duration=promocode.duration,
            devices=self.config.shop.BONUS_DEVICES_COUNT,
        )

        if success:
            return True

        async with self.session() as session:
            await Promocode.set_deactivated(session=session, code=promocode.code, user_id=user.tg_id)

        logger.warning(f"Promocode {promocode.code} not activated due to failure.")
        return False
