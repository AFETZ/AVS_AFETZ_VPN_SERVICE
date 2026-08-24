# Миграция хостинга: Finland -> Kazakhstan

Дата аудита: 2026-05-27.

## Что важно не потерять

У этого проекта два разных источника правды:

- бот: `.env`, `plans.json`, `app/data/bot_database.sqlite3`, опционально `.local/redis-data`;
- 3X-UI: база панели `/etc/x-ui/x-ui.db` и настройки inbound/server address/certs на VPN-хосте.

Публичные ссылки пользователей вида `https://<BOT_DOMAIN>/sub/<vpn_id>` можно сохранить без замены у клиентов, если оставить тот же `BOT_DOMAIN` и просто перевести DNS на новый хост.

Критично: бот хранит `vpn_id` и привязку пользователя к серверу, но фактические клиенты, expiry и inbound живут в 3X-UI. Если база 3X-UI старого сервера недоступна, одного `bot_database.sqlite3` недостаточно для полного восстановления активных подписок.

## Текущее состояние проекта

- Рабочая БД бота: `app/data/bot_database.sqlite3`.
- Alembic revision: `e4b6f0c2d9a1`.
- В БД: 53 users, 90 transactions, 20 promocodes.
- Сервер `id=1` сейчас держит 40 пользователей; `id=2` пользователей не держит.
- `BOT_USE_WEBHOOK=False`, значит бот работает через polling. В момент cutover нельзя держать старый и новый bot с одним Telegram token одновременно.
- Веб-приложение бота слушает `0.0.0.0:8080`; внешний HTTPS должен проксировать на этот порт.

## Подготовка нового KZ-хоста

1. Установить базу:

```bash
sudo apt update
sudo apt install -y git curl ca-certificates docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

2. Открыть firewall только под нужные порты:

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 8443/tcp
sudo ufw enable
```

3. Поднять 3X-UI на KZ-хосте и восстановить `/etc/x-ui/x-ui.db` со старого сервера. Делать восстановление только при остановленной панели:

```bash
sudo systemctl stop x-ui
sudo cp /tmp/x-ui.db /etc/x-ui/x-ui.db
sudo chown root:root /etc/x-ui/x-ui.db
sudo chmod 600 /etc/x-ui/x-ui.db
sudo systemctl start x-ui
sudo systemctl status x-ui --no-pager
```

После старта проверить в панели, что inbound и клиенты на месте. Если используется TLS/certs вместо Reality, перенести и сертификаты.

## Сбор миграционного бандла на старом хосте

Обычный бандл бота:

```bash
cd /home/andrey/apps/3xui-shop
./scripts/create_migration_bundle.sh
```

Если на этом же хосте доступна база 3X-UI:

```bash
sudo cp /etc/x-ui/x-ui.db /tmp/x-ui.db
sudo chown "$USER":"$USER" /tmp/x-ui.db
XUI_DB_PATH=/tmp/x-ui.db ./scripts/create_migration_bundle.sh
```

Архив появится в `backups/migration/`, рядом будет `.sha256`.

Для финального cutover лучше остановить bot перед последним бандлом, чтобы исключить новые покупки в момент копирования:

```bash
docker compose stop bot
./scripts/create_migration_bundle.sh
```

## Восстановление бота на KZ-хосте

```bash
mkdir -p /home/andrey/apps/3xui-shop
cd /home/andrey/apps/3xui-shop
tar -xzf /tmp/3xui-shop_migration_YYYYMMDD_HHMMSS.tar.gz
cd 3xui-shop_migration_YYYYMMDD_HHMMSS
cp -a source/. /home/andrey/apps/3xui-shop/
cp -a runtime/.env /home/andrey/apps/3xui-shop/.env
cp -a runtime/plans.json /home/andrey/apps/3xui-shop/plans.json
mkdir -p /home/andrey/apps/3xui-shop/app/data
cp -a runtime/app/data/bot_database.sqlite3 /home/andrey/apps/3xui-shop/app/data/bot_database.sqlite3
mkdir -p /home/andrey/apps/3xui-shop/.local
cp -a runtime/.local/redis-data /home/andrey/apps/3xui-shop/.local/ 2>/dev/null || true
```

Если домен панели 3X-UI изменился, обновить host у существующего server id, чтобы старые пользователи остались привязаны к тому же server id:

```bash
cd /home/andrey/apps/3xui-shop
python3 - <<'PY'
import sqlite3

db = "app/data/bot_database.sqlite3"
server_id = 1
new_host = "https://NEW_3XUI_PANEL_HOST/PANEL_PATH/"

con = sqlite3.connect(db)
con.execute(
    "update servers set host = ?, location = ?, online = 0 where id = ?",
    (new_host, "KZ", server_id),
)
con.commit()
print(con.execute("select id, name, location, online from servers order by id").fetchall())
con.close()
PY
```

Не меняйте `users.server_id`, если переносите старый 3X-UI сервер как замену старого server `id=1`.

## `.env` после переезда

Проверить вручную:

- `BOT_DOMAIN` лучше оставить прежним доменом без `https://`;
- `BOT_HOST` должен совпадать с публичным host reverse proxy;
- `BOT_PROXY_URL` оставить пустым, если на KZ-хосте нет локального SOCKS5 на `127.0.0.1:10808`;
- `XUI_USERNAME`/`XUI_PASSWORD` должны подходить к новой 3X-UI панели;
- `XUI_SUBSCRIPTION_SCHEME`, `XUI_SUBSCRIPTION_PORT`, `XUI_SUBSCRIPTION_PATH` должны совпадать с настройками 3X-UI subscription;
- если домен бота меняется, обновить callback/webhook URL в YooKassa/YooMoney/Cryptomus/Heleket.

## Reverse proxy и DNS

Предпочтительный вариант: оставить старый `BOT_DOMAIN` для `/sub/<vpn_id>`, чтобы старые ссылки пользователей продолжили работать. Отдельный `BOT_SUBSCRIPTION_DOMAIN` можно использовать для новых ссылок.

На новом хосте внешний HTTPS должен проксировать на `http://127.0.0.1:8080` или на docker host gateway. В репозитории есть пример `deploy/traefik-3xui-shop.yml`.

Важно: встроенный legacy-traefik в `docker-compose.yml` мапит `8443:443`. Если пользователи ходят на обычный `https://domain/sub/...`, нужен реальный listener на `443`, а не только `8443`.

## Запуск и проверка

```bash
cd /home/andrey/apps/3xui-shop
docker compose up -d --build redis bot
docker compose ps
curl -i http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/readyz
docker compose logs --tail=120 bot
```

Проверить:

- в логах нет `Traceback`/`CRITICAL`;
- `/readyz` возвращает `status: ok`;
- server pool успешно логинится в новый 3X-UI;
- Telegram-бот отвечает;
- админка видит пользователей и сервер;
- ссылка активного пользователя `/sub/<vpn_id>` возвращает профиль;
- платежный webhook возвращает ожидаемый статус;
- тестовая покупка или ручная выдача подписки создает клиента в KZ 3X-UI.

После DNS-переключения:

```bash
./scripts/smoke_test_prod.sh
```

## Rollback

Если новый хост не взлетел:

1. Остановить новый bot: `docker compose stop bot`.
2. Вернуть DNS на старый IP.
3. Запустить старый bot: `docker compose up -d bot`.
4. Если были изменения БД на новом хосте во время cutover, не смешивать их со старой БД без ручной сверки transactions/users.

Самый безопасный rollback получается, если во время cutover включить короткое окно обслуживания и не принимать оплаты до успешного smoke-test.
