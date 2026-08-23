import asyncio
import ipaddress
import logging
from urllib.parse import urljoin, urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n import I18n
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp.web import AppRunner, Application, TCPSite, _run_app
from redis.asyncio.client import Redis
from sqlalchemy import text

from app import logger
from app.bot import filters, middlewares, routers, services, tasks
from app.bot.middlewares import MaintenanceMiddleware
from app.bot.models import ServicesContainer
from app.bot.payment_gateways import GatewayFactory
from app.bot.utils import commands
from app.bot.utils.constants import (
    BOT_STARTED_TAG,
    BOT_STOPPED_TAG,
    DEFAULT_LANGUAGE,
    I18N_DOMAIN,
    TELEGRAM_WEBHOOK,
)
from app.config import DEFAULT_BOT_HOST, DEFAULT_LOCALES_DIR, Config, load_config
from app.db.database import Database
from app.web import (
    setup_additional_profile_route,
    setup_geo_file_routes,
    setup_primary_profile_route,
)

PROXY_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "socks4": 1080,
    "socks5": 1080,
}
TELEGRAM_RETRY_DELAY = 30
TELEGRAM_RETRY_MAX_DELAY = 300


def _redact_proxy_url(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return proxy_url

    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None

    port = f":{parsed_port}" if parsed_port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _proxy_endpoint(proxy_url: str) -> tuple[str, int] | None:
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return None

    try:
        port = parsed.port
    except ValueError:
        return None

    port = port or PROXY_DEFAULT_PORTS.get(parsed.scheme)
    if not port:
        return None

    return parsed.hostname, port


async def _is_proxy_reachable(proxy_url: str, timeout: float) -> bool:
    endpoint = _proxy_endpoint(proxy_url)
    if not endpoint:
        return False

    host, port = endpoint
    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port),
            timeout=timeout,
        )
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()


async def resolve_telegram_proxy_url(
    proxy_url: str | None,
    *,
    strict: bool,
    timeout: float,
) -> str | None:
    if not proxy_url:
        return None

    redacted_proxy_url = _redact_proxy_url(proxy_url)
    if await _is_proxy_reachable(proxy_url, timeout):
        logging.info("Telegram proxy is reachable: %s", redacted_proxy_url)
        return proxy_url

    if strict:
        raise RuntimeError(
            f"Telegram proxy is not reachable: {redacted_proxy_url}. "
            "BOT_PROXY_STRICT=True, startup aborted."
        )

    message = (
        f"Telegram proxy is not reachable: {redacted_proxy_url}. "
        "Starting with configured proxy anyway; Telegram calls will retry until "
        "the proxy is reachable."
    )
    logging.warning(message)
    return proxy_url


async def _close_runtime_resources(db: Database | None, bot: Bot | None) -> None:
    if bot:
        try:
            await bot.session.close()
        except Exception as exception:
            logging.warning("Failed to close bot session: %s", exception)

    if db:
        try:
            await db.close()
        except Exception as exception:
            logging.warning("Failed to close database connection: %s", exception)


async def _safe_shutdown_call(label: str, awaitable) -> None:
    try:
        await awaitable
    except Exception as exception:
        logging.warning("Shutdown step failed (%s): %s", label, exception)


async def retry_telegram_operation(label: str, operation):
    retry_delay = TELEGRAM_RETRY_DELAY
    while True:
        try:
            return await operation()
        except TelegramNetworkError as exception:
            logging.warning(
                "%s failed: %s. Retrying in %s seconds.",
                label,
                exception,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, TELEGRAM_RETRY_MAX_DELAY)


def schedule_retrying_telegram_operation(label: str, operation) -> asyncio.Task:
    async def runner() -> None:
        try:
            await retry_telegram_operation(label, operation)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("%s failed unexpectedly.", label)

    return asyncio.create_task(runner(), name=label)


async def on_shutdown(db: Database, bot: Bot, services: ServicesContainer) -> None:
    await services.notification.notify_developer(BOT_STOPPED_TAG)
    await _safe_shutdown_call("delete bot commands", commands.delete(bot))
    await _safe_shutdown_call("delete webhook", bot.delete_webhook())
    await _close_runtime_resources(db=db, bot=bot)
    logging.info("Bot stopped.")


def _is_internal_request(request: web.Request | None) -> bool:
    if request is None:
        return True

    remote = request.remote
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


async def healthcheck(request: web.Request) -> web.Response:
    if not _is_internal_request(request):
        raise web.HTTPNotFound()
    return web.Response(text="ok")


async def _check_database(db: Database) -> dict[str, object]:
    try:
        async with db.session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exception:
        logging.warning("Readiness database check failed: %s", exception)
        return {"ok": False, "error": exception.__class__.__name__}
    return {"ok": True}


async def _check_redis(redis: Redis) -> dict[str, object]:
    try:
        await redis.ping()
    except Exception as exception:
        logging.warning("Readiness Redis check failed: %s", exception)
        return {"ok": False, "error": exception.__class__.__name__}
    return {"ok": True}


def make_readiness_handler(
    *,
    db: Database,
    redis: Redis,
    services: ServicesContainer,
):
    async def readiness(request: web.Request) -> web.Response:
        if not _is_internal_request(request):
            raise web.HTTPNotFound()

        checks = {
            "database": await _check_database(db),
            "redis": await _check_redis(redis),
        }
        ready = all(check["ok"] for check in checks.values())

        xui_gateway = getattr(getattr(services, "vpn", None), "xui_gateway", None)
        xui_health = (
            xui_gateway.get_health_snapshot()
            if xui_gateway and hasattr(xui_gateway, "get_health_snapshot")
            else []
        )
        open_xui_circuits = sum(
            1 for server in xui_health if server.get("circuit_open")
        )
        server_pool = getattr(getattr(services, "server_pool", None), "_servers", {})

        payload = {
            "status": "ok" if ready else "degraded",
            "checks": checks,
            "runtime": {
                "servers_in_pool": len(server_pool),
                "xui_circuits_open": open_xui_circuits,
            },
        }
        return web.json_response(payload, status=200 if ready else 503)

    return readiness


async def start_runtime(
    config: Config,
    bot: Bot,
    services: ServicesContainer,
    db: Database,
    redis: Redis,
    i18n: I18n,
    gateway_factory: GatewayFactory,
) -> None:
    if config.bot.USE_WEBHOOK:
        webhook_url = urljoin(config.bot.DOMAIN, TELEGRAM_WEBHOOK)

        current_webhook = await retry_telegram_operation(
            "Get Telegram webhook info",
            bot.get_webhook_info,
        )
        if current_webhook.url != webhook_url:
            await retry_telegram_operation(
                "Set Telegram webhook",
                lambda: bot.set_webhook(webhook_url),
            )

        current_webhook = await retry_telegram_operation(
            "Get Telegram webhook info",
            bot.get_webhook_info,
        )
        logging.info(f"Current webhook URL: {current_webhook.url}")
    else:
        await retry_telegram_operation(
            "Delete Telegram webhook",
            lambda: bot.delete_webhook(drop_pending_updates=False),
        )
        logging.info("Telegram webhook disabled. Bot is running in polling mode.")

    await services.notification.notify_developer(BOT_STARTED_TAG)
    logging.info("Bot started.")

    schedule_retrying_telegram_operation(
        "Configure Telegram bot commands",
        lambda: commands.setup(bot),
    )

    tasks.transactions.start_scheduler(db.session, gateway_factory, redis=redis)
    if config.shop.REFERRER_REWARD_ENABLED:
        tasks.referral.start_scheduler(
            session_factory=db.session,
            referral_service=services.referral,
            redis=redis,
        )
    tasks.subscription_expiry.start_scheduler(
        session_factory=db.session,
        redis=redis,
        i18n=i18n,
        vpn_service=services.vpn,
        notification_service=services.notification,
    )


async def serve_app(app: Application, host: str, port: int) -> None:
    runner = AppRunner(app)
    await runner.setup()
    site = TCPSite(runner, host=host, port=port)
    await site.start()
    logging.info("Web app started on %s:%s.", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    # Create web application
    app = Application()
    app.router.add_get("/healthz", healthcheck)

    # Load configuration
    config = load_config()

    # Set up logging
    logger.setup_logging(config.logging)

    db: Database | None = None
    bot: Bot | None = None
    try:
        # Initialize database
        db = Database(config.database)
        await db.initialize()

        # Set up storage for FSM (Finite State Machine)
        storage = RedisStorage.from_url(url=config.redis.url())
        # storage = MemoryStorage()

        # Initialize the bot with the token and default properties
        session_kwargs = {}
        proxy_url = await resolve_telegram_proxy_url(
            config.bot.PROXY_URL,
            strict=config.bot.PROXY_STRICT,
            timeout=config.bot.PROXY_CHECK_TIMEOUT,
        )
        if proxy_url:
            session_kwargs["proxy"] = proxy_url
        if config.bot.API_SERVER:
            session_kwargs["api"] = TelegramAPIServer.from_base(config.bot.API_SERVER)
        bot_session = AiohttpSession(**session_kwargs) if session_kwargs else None
        bot = Bot(
            token=config.bot.TOKEN,
            session=bot_session,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML, link_preview_is_disabled=True
            ),
        )

        # Set up internationalization (i18n)
        i18n = I18n(
            path=DEFAULT_LOCALES_DIR,
            default_locale=DEFAULT_LANGUAGE,
            domain=I18N_DOMAIN,
        )
        I18n.set_current(i18n)

        # Initialize services
        services_container = await services.initialize(
            config=config,
            session=db.session,
            bot=bot,
        )

        # Sync servers
        await services_container.server_pool.sync_servers()

        # Register payment gateways
        gateway_factory = GatewayFactory()
        gateway_factory.register_gateways(
            app=app,
            config=config,
            session=db.session,
            storage=storage,
            bot=bot,
            i18n=i18n,
            services=services_container,
        )
        app.router.add_get(
            "/readyz",
            make_readiness_handler(
                db=db,
                redis=storage.redis,
                services=services_container,
            ),
        )

        # Create the dispatcher
        dispatcher = Dispatcher(
            db=db,
            storage=storage,
            config=config,
            bot=bot,
            services=services_container,
            gateway_factory=gateway_factory,
            redis=storage.redis,
            i18n=i18n,
        )

        # Register event handlers
        dispatcher.startup.register(start_runtime)
        dispatcher.shutdown.register(on_shutdown)

        # Enable Maintenance mode for developing # WARNING: remove before production
        MaintenanceMiddleware.set_mode(False)

        # Register middlewares
        middlewares.register(dispatcher=dispatcher, i18n=i18n, session=db.session)

        # Register filters
        filters.register(
            dispatcher=dispatcher,
            developer_id=config.bot.DEV_ID,
            admins_ids=config.bot.ADMINS,
        )

        # Include bot routers
        routers.include(app=app, dispatcher=dispatcher)
        setup_additional_profile_route(
            app=app,
            subscription_service=services_container.subscription,
        )
        setup_primary_profile_route(
            app=app,
            subscription_service=services_container.subscription,
        )
        setup_geo_file_routes(app)

        # Set up webhook request handler
        webhook_requests_handler = SimpleRequestHandler(dispatcher=dispatcher, bot=bot)
        webhook_requests_handler.register(app, path=TELEGRAM_WEBHOOK)

        if config.bot.USE_WEBHOOK:
            # Set up application and run
            setup_application(app, dispatcher, bot=bot)
            await _run_app(app, host=DEFAULT_BOT_HOST, port=config.bot.PORT)
        else:
            web_task = asyncio.create_task(
                serve_app(app, DEFAULT_BOT_HOST, config.bot.PORT)
            )
            try:
                await retry_telegram_operation(
                    "Start Telegram polling",
                    lambda: dispatcher.start_polling(bot),
                )
            finally:
                web_task.cancel()
                await asyncio.gather(web_task, return_exceptions=True)
    except Exception:
        logging.exception("Bot runtime failed.")
        await _close_runtime_resources(db=db, bot=bot)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
