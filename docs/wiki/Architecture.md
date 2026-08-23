# Архитектура

AFZVPN Bot состоит из Telegram-бота, aiohttp web app, локальной базы, Redis и внешних 3X-UI панелей. 3X-UI остается источником правды для реальных VPN-клиентов, inbound, traffic и expiry.

## Runtime

```text
Telegram
  -> aiogram Router
  -> Services layer
  -> SQLite / Redis / 3X-UI
  -> aiohttp public endpoints
  -> Happ / payment gateways
```

## Компоненты

| Компонент | Ответственность |
| --- | --- |
| `app/__main__.py` | старт bot + web app, polling/webhook, scheduler, Redis, payment gateways |
| `app/bot/routers` | Telegram UI, callback navigation, user/admin flows |
| `app/bot/services` | подписки, VPN, server pool, notifications, job locks, runtime metrics |
| `app/bot/tasks` | фоновые reconciliation и expiry jobs |
| `app/web` | subscription proxy, additional profiles, connection redirect |
| `app/db` | SQLAlchemy models, Alembic migrations |
| `plans.json` | коммерческий каталог тарифов |

## Public Endpoints

| Route | Назначение |
| --- | --- |
| `/healthz` | локальный health check |
| `/readyz` | локальный readiness check: БД, Redis и runtime-сводка |
| `/webhook` | Telegram webhook endpoint |
| `/connection` | redirect для Happ deep-links |
| `/geoip.dat`, `/geosite.dat` | локально кэшируемые geo-файлы для Happ; клиент не обращается к GitHub напрямую |
| `/sub/{vpn_id}` | основная подписка |
| `/wl-filtered/{vpn_id}` | рекомендуемая подписка обхода БС |
| `/wl/{vpn_id}` | запасная подписка обхода БС |
| payment callbacks | YooKassa, YooMoney, Cryptomus, Heleket |

## Data Ownership

| Данные | Где источник правды |
| --- | --- |
| Telegram user, plan, local state | bot SQLite |
| VPN client, traffic, expiry | 3X-UI |
| FSM/background state | Redis |
| тарифы | `plans.json` |
| платежные транзакции | bot SQLite + payment gateway callback |

## Важные Границы

- Бот не должен отдавать подписку неактивному пользователю.
- Web endpoints валидируют `vpn_id` и состояние подписки.
- Ссылки Happ и geo-файлов строятся на `BOT_SUBSCRIPTION_DOMAIN` с fallback на `BOT_DOMAIN`.
- Корень домена, health endpoints и неизвестные маршруты не публикуются наружу.
- Runtime state не хранится в Git.
- Миграции Alembic должны быть совместимы с существующей production-базой.
