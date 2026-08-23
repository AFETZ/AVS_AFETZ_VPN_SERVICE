# Операции

## Ежедневные Проверки

```bash
docker ps
docker logs --tail=120 3xui-shop-bot
curl -I http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/readyz
docker inspect --format='{{.State.Health.Status}}' 3xui-shop-bot
TELEGRAM_PROXY_RESTART=0 scripts/check_telegram_proxy.sh
```

Проверить вручную:

- бот отвечает в Telegram;
- активная ссылка `/sub/{vpn_id}` открывается;
- подписка обхода БС открывается на тарифе с `includes_additional_profile`;
- `/geoip.dat` и `/geosite.dat` отвечают `200` с валидным публичным TLS;
- платежные callbacks не сыпят ошибки;
- Redis container жив;
- `/readyz` возвращает `status: ok`;
- Docker health status для `3xui-shop-bot` равен `healthy`;
- `telegram-proxy-watchdog.user.timer` активен, если используется `BOT_PROXY_URL`;
- в логах нет циклического restart loop.

## Деплой

```bash
docker compose up -d --build bot
docker logs --tail=120 3xui-shop-bot
```

Успешный старт обычно содержит:

- `Bot started`;
- `Start polling` или текущий webhook URL;
- `Web app started on 0.0.0.0:8080`;
- успешный Alembic upgrade.

## Polling И Webhook

Текущий режим задается `BOT_USE_WEBHOOK`.

Polling проще в эксплуатации, но держит постоянный long polling к Telegram API. Webhook лучше для production, если домен, TLS, reverse proxy и secret-path подготовлены. Перед переключением на webhook проверить:

- `BOT_DOMAIN` доступен по HTTPS;
- `/webhook` принимает `POST`;
- reverse proxy корректно прокидывает `X-Forwarded-*`;
- Telegram API доступен напрямую или через стабильный `BOT_PROXY_URL`.

Для production предпочтительный режим - webhook. Polling оставлять как fallback на время миграции или если публичный HTTPS endpoint недоступен.

## Telegram Proxy Watchdog

Если `BOT_PROXY_URL` указывает на локальный SOCKS, держать включенным user timer:

```bash
loginctl enable-linger "$(id -un)"
systemctl --user enable --now telegram-proxy.user.service
systemctl --user enable --now telegram-proxy-watchdog.user.timer
systemctl --user status telegram-proxy.user.service
systemctl --user status telegram-proxy-watchdog.user.timer
journalctl --user -u telegram-proxy-watchdog.user.service -n 50 --no-pager
```

`linger` обязателен: без него user services могут не подняться после reboot до первого входа пользователя.

Ручная проверка без рестарта:

```bash
TELEGRAM_PROXY_RESTART=0 scripts/check_telegram_proxy.sh
```

Если проверка падает, watchdog перезапускает `telegram-proxy.user.service` и повторяет запрос к `https://api.telegram.org`.

## Бэкапы

Перед крупными изменениями сохранить:

- SQLite database из `app/data`;
- `.env`;
- `plans.json`;
- актуальные compose/deploy файлы;
- список docker images/containers.

Runtime state не коммитится в Git.

## Логи

Основной лог: `app/logs/app.log`.

Ротация управляется:

- `LOG_MAX_BYTES`;
- `LOG_BACKUP_COUNT`.

Логи могут содержать production-контекст, поэтому не отправлять их в публичные issues без редактирования.
