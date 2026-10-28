import logging
from dataclasses import dataclass

from py3xui import AsyncApi, Inbound
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Config
from app.bot.services.xui_csrf import CsrfAsyncApi
from app.bot.utils.network import ping_url
from app.db.models import Server, User

logger = logging.getLogger(__name__)

PROFILE_SERVER_LOCATION_ORDER = {
    "KZ": 0,
    "KAZAKHSTAN": 0,
    "FI": 1,
    "FINLAND": 1,
}


@dataclass
class Connection:
    server: Server
    api: AsyncApi


@dataclass(frozen=True)
class ServerHealthcheckResult:
    server_name: str
    online: bool
    latency_ms: float | None


def _profile_server_order(server: Server) -> int:
    location = (server.location or "").upper()
    name = (server.name or "").upper()

    if location in {"KZ", "KAZAKHSTAN"} or "KAZAKHSTAN" in name:
        return PROFILE_SERVER_LOCATION_ORDER["KZ"]
    if location in {"FI", "FINLAND"} or "FINLAND" in name:
        return PROFILE_SERVER_LOCATION_ORDER["FI"]
    return len(PROFILE_SERVER_LOCATION_ORDER)


def _is_profile_server(server: Server) -> bool:
    return _profile_server_order(server) < len(PROFILE_SERVER_LOCATION_ORDER)


def _profile_server_sort_key(server: Server) -> tuple[int, str, str]:
    return (
        _profile_server_order(server),
        (server.location or "").upper(),
        server.name,
    )


class ServerPoolService:
    def __init__(self, config: Config, session: async_sessionmaker) -> None:
        self.config = config
        self.session = session
        self._servers: dict[int, Connection] = {}
        logger.info("Server Pool Service initialized.")

    @staticmethod
    def _configure_api(api: AsyncApi) -> None:
        # py3xui retries each HTTP request internally. Keeping this low prevents a
        # slow panel from tying up bot resources across full-user background scans.
        for api_part in (
            getattr(api, "client", None),
            getattr(api, "inbound", None),
            getattr(api, "database", None),
            getattr(api, "server", None),
        ):
            if api_part is None or not hasattr(api_part, "max_retries"):
                continue
            api_part.max_retries = 1

    async def _add_server(self, server: Server) -> None:
        if server.id not in self._servers:
            xui_logger = logging.getLogger(f"xui_{server.name}")
            xui_logger.setLevel(logging.WARNING)
            credentials = getattr(self.config.xui, "SERVER_CREDENTIALS", {}).get(server.name, {})
            api_class = CsrfAsyncApi if credentials.get("csrf") else AsyncApi
            api = api_class(
                host=server.host,
                username=credentials.get("username", self.config.xui.USERNAME),
                password=credentials.get("password", self.config.xui.PASSWORD),
                token=credentials.get("token", self.config.xui.TOKEN),
                # use_tls_verify=False,
                logger=xui_logger,
            )
            self._configure_api(api)
            try:
                await api.login()
                server.online = True
                server_conn = Connection(server=server, api=api)
                self._servers[server.id] = server_conn
                logger.info(f"Server {server.name} ({server.host}) added to pool successfully.")
            except Exception as exception:
                server.online = False
                logger.error(f"Failed to add server {server.name} ({server.host}): {exception}")

            async with self.session() as session:
                await Server.update(session=session, name=server.name, online=server.online)

    def _remove_server(self, server: Server) -> None:
        if server.id in self._servers:
            try:
                del self._servers[server.id]
            except Exception as exception:
                logger.error(f"Failed to remove server {server.name}: {exception}")

    async def get_connection_by_server_id(self, server_id: int) -> Connection | None:
        connection = self._servers.get(server_id)
        if connection:
            async with self.session() as session:
                server = await Server.get_by_id(session=session, id=server_id)
            if server:
                connection.server = server
            return connection

        async with self.session() as session:
            server = await Server.get_by_id(session=session, id=server_id)

        if not server:
            logger.error("Server %s not found in database.", server_id)
            return None

        await self._add_server(server)
        return self._servers.get(server_id)

    async def refresh_server(self, server: Server) -> None:
        if server.id in self._servers:
            self._remove_server(server)

        await self._add_server(server)
        logger.info(f"Server {server.name} reinitialized successfully.")

    async def get_inbound(self, api: AsyncApi) -> Inbound | None:
        try:
            inbounds = await api.inbound.get_list()
        except Exception as exception:
            logger.error(f"Failed to fetch inbounds: {exception}")
            return None
        if not inbounds:
            logger.error("No inbounds found on server.")
            return None
        return inbounds[0]

    async def get_inbound_id(self, api: AsyncApi) -> int | None:
        inbound = await self.get_inbound(api)
        if not inbound:
            return None
        return inbound.id

    async def get_connection(self, user: User) -> Connection | None:
        if not user.server_id:
            logger.debug(f"User {user.tg_id} not assigned to any server.")
            return None

        connection = self._servers.get(user.server_id)

        if not connection:
            available_servers = list(self._servers.keys())
            logger.warning(
                f"Server {user.server_id} not found in pool. "
                f"User assigned server: {user.server_id}, "
                f"Available servers in pool: {available_servers}"
            )

            async with self.session() as session:
                server = await Server.get_by_id(session=session, id=user.server_id)

            if server:
                logger.debug(f"Server {server.name} ({server.host}) found in database.")
                logger.info(
                    "Attempting to restore server %s (%s) connection.",
                    server.name,
                    server.host,
                )
                await self._add_server(server)
                restored_connection = self._servers.get(user.server_id)
                if restored_connection:
                    return restored_connection
            else:
                logger.error(f"Server {user.server_id} not found in database.")

            return None

        async with self.session() as session:
            server = await Server.get_by_id(session=session, id=user.server_id)

        connection.server = server
        return connection

    async def sync_servers(self) -> None:
        async with self.session() as session:
            db_servers = await Server.get_all(session)

        if not db_servers and not self._servers:
            logger.warning("No servers found in the database.")
            return

        db_server_map = {server.id: server for server in db_servers}

        for server_id in list(self._servers.keys()):
            if server_id not in db_server_map:
                self._remove_server(self._servers[server_id].server)

        for server_id, conn in list(self._servers.items()):
            if db_server := db_server_map.get(server_id):
                conn.server = db_server
            await self.refresh_server(conn.server)

        for server in db_servers:
            if server.id not in self._servers:
                await self._add_server(server)

        logger.info(f"Sync complete. Currently active servers: {len(self._servers)}")

    async def assign_server_to_user(self, user: User) -> None:
        async with self.session() as session:
            server = await self.get_available_server()
            user.server_id = server.id
            await User.update(session=session, tg_id=user.tg_id, server_id=server.id)

    async def get_available_server(self) -> Server | None:
        await self.sync_servers()

        servers_with_free_slots = [
            conn.server
            for conn in self._servers.values()
            if conn.server.current_clients < conn.server.max_clients
        ]

        if servers_with_free_slots:
            server = sorted(servers_with_free_slots, key=lambda s: s.current_clients)[0]
            logger.debug(
                f"Found server with free slots: {server.name} "
                f"(clients: {server.current_clients}/{server.max_clients})"
            )
            return server

        servers_least_loaded = [conn.server for conn in self._servers.values() if conn.server.max_clients > 0]
        if servers_least_loaded:
            server = sorted(servers_least_loaded, key=lambda s: s.current_clients)[0]
            logger.warning(
                f"No servers with free slots. Using least loaded server: {server.name} "
                f"(clients: {server.current_clients}/{server.max_clients})"
            )
            return server

        logger.critical("No available servers found in pool")
        return None

    async def get_selectable_servers(self) -> list[Server]:
        await self.sync_servers()
        return sorted(
            [conn.server for conn in self._servers.values() if conn.server.online and conn.server.max_clients > 0],
            key=_profile_server_sort_key,
        )

    async def get_profile_servers(self) -> list[Server]:
        """Profile servers for building subscription source URLs.

        Reads the database only: serving /sub must not depend on panel logins,
        otherwise a panel hiccup removes the node from every client's profile
        and drops active connections.
        """
        async with self.session() as session:
            db_servers = await Server.get_all(session)

        profile_servers = [server for server in db_servers if _is_profile_server(server)]
        if not profile_servers:
            profile_servers = [server for server in db_servers if server.online]

        return sorted(profile_servers, key=_profile_server_sort_key)

    async def get_profile_connections(self) -> list[Connection]:
        async with self.session() as session:
            db_servers = await Server.get_all(session)

        db_server_map = {server.id: server for server in db_servers}
        connections: list[Connection] = []

        for server_id, connection in list(self._servers.items()):
            db_server = db_server_map.get(server_id)
            if not db_server:
                continue
            connection.server = db_server
            if connection.server.online:
                connections.append(connection)

        for server in db_servers:
            if server.id in self._servers or not server.online:
                continue
            connection = await self.get_connection_by_server_id(server.id)
            if connection and connection.server.online:
                connections.append(connection)

        if not connections:
            return []

        profile_connections = [
            connection for connection in connections if _is_profile_server(connection.server)
        ]
        return sorted(
            profile_connections or connections,
            key=lambda connection: _profile_server_sort_key(connection.server),
        )

    async def healthcheck_servers(self) -> list[ServerHealthcheckResult]:
        async with self.session() as session:
            servers = await Server.get_all(session)

        results: list[ServerHealthcheckResult] = []
        for server in servers:
            latency_ms = await ping_url(server.host)
            online = latency_ms is not None
            async with self.session() as session:
                await Server.update(session=session, name=server.name, online=online)
            if server.id in self._servers:
                self._servers[server.id].server.online = online
            results.append(
                ServerHealthcheckResult(
                    server_name=server.name,
                    online=online,
                    latency_ms=latency_ms,
                )
            )

        return results
