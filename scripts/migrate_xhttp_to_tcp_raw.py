from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from py3xui import AsyncApi, Inbound
from py3xui.inbound.stream_settings import StreamSettings

from app.config import BASE_DIR, Config, load_config
from app.db.database import Database
from app.db.models import Server


DEFAULT_TARGET_NETWORK = "tcp"
DEFAULT_TARGET_FLOW = "xtls-rprx-vision"
DEFAULT_TCP_SETTINGS = {"acceptProxyProtocol": False, "header": {"type": "none"}}
DEFAULT_BACKUP_DIR = BASE_DIR / "data" / "xui_migration_backups"

logger = logging.getLogger("migrate_xhttp_to_tcp_raw")


@dataclass(frozen=True)
class InboundPlan:
    inbound: Inbound
    old_network: str
    new_network: str
    clients_total: int
    clients_to_update: int
    change_network: bool
    fix_flow_only: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate 3x-ui VLESS inbounds from XHTTP to TCP/RAW and update "
            "client flow for VLESS+REALITY/TLS."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag the script only prints a dry-run plan.",
    )
    parser.add_argument(
        "--server-id",
        type=int,
        action="append",
        help="Limit migration to a server id from the bot database. Can be passed multiple times.",
    )
    parser.add_argument(
        "--inbound-id",
        type=int,
        action="append",
        help="Limit migration to a 3x-ui inbound id. Can be passed multiple times.",
    )
    parser.add_argument(
        "--from-network",
        default="xhttp",
        help="Source streamSettings.network to migrate. Default: xhttp.",
    )
    parser.add_argument(
        "--target-network",
        default=DEFAULT_TARGET_NETWORK,
        help="Target 3x-ui streamSettings.network. Default: tcp (3x-ui name for RAW).",
    )
    parser.add_argument(
        "--target-flow",
        default=DEFAULT_TARGET_FLOW,
        help="Flow for VLESS TCP+REALITY/TLS clients. Default: xtls-rprx-vision.",
    )
    parser.add_argument(
        "--fix-existing-tcp",
        dest="fix_existing_tcp",
        action="store_true",
        default=True,
        help="Also fix flow on already-TCP VLESS inbounds. Enabled by default.",
    )
    parser.add_argument(
        "--no-fix-existing-tcp",
        dest="fix_existing_tcp",
        action="store_false",
        help="Do not touch already-TCP VLESS inbounds.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help=f"Directory for 3x-ui DB backups and JSON snapshots. Default: {DEFAULT_BACKUP_DIR}",
    )
    parser.add_argument(
        "--skip-db-backup",
        action="store_true",
        help="Skip 3x-ui DB download before applying changes.",
    )
    parser.add_argument(
        "--continue-without-backup",
        action="store_true",
        help="Apply changes even if 3x-ui DB backup download fails.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("._") or "server"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _configure_api(api: AsyncApi) -> None:
    for api_part in (
        getattr(api, "client", None),
        getattr(api, "inbound", None),
        getattr(api, "database", None),
        getattr(api, "server", None),
    ):
        if api_part is not None and hasattr(api_part, "max_retries"):
            api_part.max_retries = 1


async def _load_servers(config: Config, server_ids: set[int] | None) -> list[Server]:
    db = Database(config.database)
    try:
        async with db.session() as session:
            servers = await Server.get_all(session)
    finally:
        await db.close()

    if server_ids is not None:
        servers = [server for server in servers if server.id in server_ids]

    return sorted(servers, key=lambda server: server.id)


def _stream_settings_dict(inbound: Inbound) -> dict[str, Any]:
    stream_settings = inbound.stream_settings
    if isinstance(stream_settings, StreamSettings):
        return stream_settings.model_dump(by_alias=True)
    if isinstance(stream_settings, str) and stream_settings.strip():
        try:
            data = json.loads(stream_settings)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    if isinstance(stream_settings, dict):
        return stream_settings
    return {}


def _ensure_stream_settings(inbound: Inbound) -> StreamSettings:
    stream_settings = inbound.stream_settings
    if isinstance(stream_settings, StreamSettings):
        return stream_settings

    data = _stream_settings_dict(inbound)
    if "network" not in data:
        data["network"] = DEFAULT_TARGET_NETWORK
    if "security" not in data:
        data["security"] = "none"

    inbound.stream_settings = StreamSettings(**data)
    return inbound.stream_settings


def _network(inbound: Inbound) -> str:
    return str(_stream_settings_dict(inbound).get("network") or "").lower()


def _security(inbound: Inbound) -> str:
    return str(_stream_settings_dict(inbound).get("security") or "").lower()


def _is_vless(inbound: Inbound) -> bool:
    return (inbound.protocol or "").lower() == "vless"


def _client_count(inbound: Inbound) -> int:
    return len(getattr(inbound.settings, "clients", None) or [])


def _is_target_flow_applicable(inbound: Inbound, target_network: str) -> bool:
    return _is_vless(inbound) and target_network in {"tcp", "raw"} and _security(inbound) in {
        "reality",
        "tls",
    }


def _count_clients_to_update(inbound: Inbound, target_flow: str) -> int:
    clients = getattr(inbound.settings, "clients", None) or []
    return sum(1 for client in clients if getattr(client, "flow", "") != target_flow)


def _set_inbound_tcp_raw(inbound: Inbound, target_network: str) -> None:
    stream_settings = _ensure_stream_settings(inbound)
    stream_settings.network = target_network
    stream_settings.tcp_settings = dict(DEFAULT_TCP_SETTINGS)


def _set_clients_flow(inbound: Inbound, target_flow: str) -> None:
    for client in getattr(inbound.settings, "clients", None) or []:
        client.flow = target_flow


def _inbound_snapshot(inbounds: list[Inbound]) -> list[dict[str, Any]]:
    return [
        inbound.model_dump(mode="json", by_alias=True)
        for inbound in sorted(inbounds, key=lambda item: item.id)
    ]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


async def _backup_server(
    api: AsyncApi,
    server: Server,
    inbounds: list[Inbound],
    backup_dir: Path,
    run_id: str,
) -> tuple[Path | None, Path]:
    server_slug = f"server_{server.id}_{_safe_slug(server.name)}"
    server_backup_dir = backup_dir / run_id
    server_backup_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = server_backup_dir / f"{server_slug}_inbounds_before.json"
    _write_json(snapshot_path, _inbound_snapshot(inbounds))

    db_path = server_backup_dir / f"{server_slug}_x-ui.db"
    await _download_xui_db(api, db_path)
    return db_path, snapshot_path


async def _download_xui_db(api: AsyncApi, db_path: Path) -> None:
    """Download x-ui.db using the current 3x-ui endpoint, with py3xui fallback."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # py3xui 0.3.x still points at server/getDb. Current 3x-ui exposes the
    # backup under panel/api/server/getDb.
    try:
        endpoint = "panel/api/server/getDb"
        headers = {"Accept": "application/octet-stream"}
        url = api.server._url(endpoint)  # noqa: SLF001 - py3xui has no public raw request API.
        response = await api.server._get(url, headers, skip_check=True)  # noqa: SLF001
        if response.status_code == 200:
            db_path.write_bytes(response.content)
            return
        response.raise_for_status()
    except Exception as exception:
        logger.warning(
            "Current 3x-ui DB endpoint failed, trying py3xui fallback: %s",
            exception,
        )

    await api.server.get_db(str(db_path))


def _build_plan_for_inbound(
    inbound: Inbound,
    *,
    from_network: str,
    target_network: str,
    target_flow: str,
    fix_existing_tcp: bool,
    inbound_ids: set[int] | None,
) -> InboundPlan | None:
    if inbound_ids is not None and inbound.id not in inbound_ids:
        return None
    if not _is_vless(inbound):
        return None

    old_network = _network(inbound)
    change_network = old_network == from_network
    fix_flow_only = bool(
        fix_existing_tcp
        and not change_network
        and old_network in {"tcp", "raw"}
        and _is_target_flow_applicable(inbound, old_network)
    )
    flow_target_network = target_network if change_network else old_network
    clients_to_update = (
        _count_clients_to_update(inbound, target_flow)
        if _is_target_flow_applicable(inbound, flow_target_network)
        else 0
    )
    if not change_network and (not fix_flow_only or clients_to_update == 0):
        return None

    return InboundPlan(
        inbound=inbound,
        old_network=old_network,
        new_network=flow_target_network,
        clients_total=_client_count(inbound),
        clients_to_update=clients_to_update,
        change_network=change_network,
        fix_flow_only=fix_flow_only,
    )


async def _verify_inbound(
    api: AsyncApi,
    inbound_id: int,
    *,
    target_network: str,
    target_flow: str,
) -> tuple[bool, str]:
    inbounds = await api.inbound.get_list()
    inbound = next((item for item in inbounds if item.id == inbound_id), None)
    if inbound is None:
        return False, "inbound disappeared after update"

    network = _network(inbound)
    if network != target_network:
        return False, f"network is {network!r}, expected {target_network!r}"

    if _is_target_flow_applicable(inbound, target_network):
        wrong_flow = _count_clients_to_update(inbound, target_flow)
        if wrong_flow:
            return False, f"{wrong_flow} client(s) still have non-target flow"

    return True, "ok"


async def _apply_client_flow_updates(
    api: AsyncApi,
    inbound: Inbound,
    target_flow: str,
) -> int:
    updated = 0
    for client in getattr(inbound.settings, "clients", None) or []:
        if getattr(client, "flow", "") == target_flow:
            continue
        if client.id is None:
            logger.warning(
                "Skipping client with empty UUID on inbound %s (email=%s).",
                inbound.id,
                getattr(client, "email", "?"),
            )
            continue

        client.inbound_id = inbound.id
        client.flow = target_flow
        await api.client.update(client_uuid=str(client.id), client=client)
        updated += 1
    return updated


async def _migrate_server(
    config: Config,
    server: Server,
    *,
    args: argparse.Namespace,
    run_id: str,
) -> tuple[int, int]:
    logger.info("Connecting to server %s (%s).", server.id, server.name)
    xui_logger = logging.getLogger(f"xui_migration_{server.id}")
    xui_logger.setLevel(logging.WARNING)
    api = AsyncApi(
        host=server.host,
        username=config.xui.USERNAME,
        password=config.xui.PASSWORD,
        token=config.xui.TOKEN,
        logger=xui_logger,
    )
    _configure_api(api)
    await api.login()

    inbounds = await api.inbound.get_list()
    inbound_ids = set(args.inbound_id) if args.inbound_id else None
    plans = [
        plan
        for inbound in inbounds
        if (
            plan := _build_plan_for_inbound(
                inbound,
                from_network=args.from_network,
                target_network=args.target_network,
                target_flow=args.target_flow,
                fix_existing_tcp=args.fix_existing_tcp,
                inbound_ids=inbound_ids,
            )
        )
    ]

    if not plans:
        logger.info("Server %s (%s): no matching inbounds.", server.id, server.name)
        return 0, 0

    for plan in plans:
        action = "migrate" if plan.change_network else "fix-flow"
        logger.info(
            "Server %s (%s): %s inbound=%s protocol=%s network=%s -> %s clients=%s flow_updates=%s",
            server.id,
            server.name,
            action,
            plan.inbound.id,
            plan.inbound.protocol,
            plan.old_network,
            plan.new_network,
            plan.clients_total,
            plan.clients_to_update,
        )

    if not args.apply:
        return len(plans), sum(plan.clients_to_update for plan in plans)

    if not args.skip_db_backup:
        try:
            db_path, snapshot_path = await _backup_server(
                api=api,
                server=server,
                inbounds=inbounds,
                backup_dir=args.backup_dir,
                run_id=run_id,
            )
            logger.info(
                "Server %s (%s): backup saved: db=%s snapshot=%s",
                server.id,
                server.name,
                db_path,
                snapshot_path,
            )
        except Exception as exception:
            if not args.continue_without_backup:
                raise RuntimeError(
                    f"Backup failed for server {server.id} ({server.name}); "
                    "rerun with --continue-without-backup to override."
                ) from exception
            logger.warning(
                "Server %s (%s): backup failed, continuing by request: %s",
                server.id,
                server.name,
                exception,
            )

    changed_inbounds = 0
    changed_clients = 0
    for plan in plans:
        inbound = plan.inbound
        if plan.change_network:
            _set_inbound_tcp_raw(inbound, args.target_network)
            if _is_target_flow_applicable(inbound, args.target_network):
                _set_clients_flow(inbound, args.target_flow)
            await api.inbound.update(inbound.id, inbound)
            changed_inbounds += 1

        # Refetch before individual client updates so we update the exact state
        # accepted by 3x-ui after the inbound update.
        refreshed = await api.inbound.get_by_id(inbound.id)
        if _is_target_flow_applicable(refreshed, _network(refreshed)):
            changed_clients += await _apply_client_flow_updates(
                api=api,
                inbound=refreshed,
                target_flow=args.target_flow,
            )

        ok, reason = await _verify_inbound(
            api,
            inbound.id,
            target_network=args.target_network if plan.change_network else plan.old_network,
            target_flow=args.target_flow,
        )
        if not ok:
            raise RuntimeError(
                f"Verification failed for server {server.id} inbound {inbound.id}: {reason}"
            )
        logger.info(
            "Server %s (%s): verified inbound=%s (%s).",
            server.id,
            server.name,
            inbound.id,
            reason,
        )

    return changed_inbounds, changed_clients


async def async_main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()
    server_ids = set(args.server_id) if args.server_id else None
    servers = await _load_servers(config, server_ids)
    if not servers:
        logger.error("No servers found for migration.")
        return 1

    run_id = _timestamp()
    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info(
        "%s: servers=%s from_network=%s target_network=%s target_flow=%s",
        mode,
        [server.id for server in servers],
        args.from_network,
        args.target_network,
        args.target_flow,
    )

    total_inbounds = 0
    total_clients = 0
    for server in servers:
        changed_inbounds, changed_clients = await _migrate_server(
            config=config,
            server=server,
            args=args,
            run_id=run_id,
        )
        total_inbounds += changed_inbounds
        total_clients += changed_clients

    logger.info(
        "%s finished: inbounds=%s clients=%s backup_run_id=%s",
        mode,
        total_inbounds,
        total_clients,
        run_id if args.apply and not args.skip_db_backup else "-",
    )
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exception:
        logger.exception("Migration failed: %s", exception)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
