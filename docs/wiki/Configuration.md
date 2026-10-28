# Конфигурация

## `.env`

Основной пример: `.env.example`.

Ключевые группы:

- Telegram: `BOT_TOKEN`, `BOT_ADMINS`, `BOT_DEV_ID`;
- публичный домен: `BOT_DOMAIN`, `BOT_HOST`;
- отдельный домен выдачи: `BOT_SUBSCRIPTION_DOMAIN` - используется для `/connection`, `/sub`, `/wl` и `/wl-filtered`, по умолчанию равен `BOT_DOMAIN`;
- запасной домен выдачи: `BOT_SUBSCRIPTION_BACKUP_DOMAIN` - если задан и отличается от основного, бот показывает отдельную запасную кнопку;
- режим Telegram updates: `BOT_USE_WEBHOOK`;
- proxy: `BOT_PROXY_URL`, `BOT_PROXY_STRICT`, `BOT_PROXY_CHECK_TIMEOUT`;
- 3X-UI: `XUI_USERNAME`, `XUI_PASSWORD`, `XUI_SUBSCRIPTION_*`;
- payments: `SHOP_PAYMENT_*_ENABLED` и gateway secrets;
- logs: `LOG_LEVEL`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`.

## `plans.json`

Тарифы задаются в `plans.json`.
Текущая витрина использует сроки `30`, `60`, `90` и `180` дней; годовой срок намеренно не продается.

Важные поля:

- `code`;
- `title`;
- `devices`;
- `prices`;
- `is_public`;
- `is_popular`;
- `includes_additional_profile`;
- `upgrade_from`.

`includes_additional_profile: true` открывает подписку обхода БС.

## Runtime State

Не коммитить:

- `.env`;
- `.env.staging`;
- `app/data`;
- `app/logs`;
- `.local`;
- `.venv`;
- `backups`;
- compiled `.mo`.

## Docker

Production service:

```bash
docker compose up -d --build bot
```

После изменения доменов обязательно проверить локальные health endpoints, deep-link redirect и geo-файлы:

```bash
scripts/smoke_test_prod.sh
```

Staging service использует `docker-compose.staging.yml` и отдельный `.env.staging`.
