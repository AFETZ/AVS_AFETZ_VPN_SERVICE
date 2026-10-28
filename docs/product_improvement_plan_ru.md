# План улучшения продукта и кода

Дата: 2026-07-01

## Цель

Сделать AFZVPN устойчивее как продукт для продления доступа в ситуациях, когда Telegram, бот или активный VPN недоступны, и одновременно снизить риск регрессий в коде вокруг кабинета, подписок и уведомлений.

## Выполнить сейчас

1. Централизовать построение публичных URL и ссылок кабинета.
2. Централизовать текстовые подсказки про сохранение кабинета для продления.
3. Подключить эти helper-ы в сервис подписок, платежные уведомления и фоновые напоминания.
4. Вынести большие HTML-шаблоны web-кабинета из Python handlers в `app/web/templates`.
5. Добавить `/readyz` для проверки готовности БД, Redis и runtime-состояния.
6. Подключить Docker health checks к `/readyz` для production и staging контейнеров.
7. Заменить годовой срок подписки на 3 месяца: `30`, `60`, `90`, `180` дней.
8. Покрыть поведение тестами: отдельный домен кабинета, fallback на `BOT_DOMAIN`, русская и английская подсказки, загрузка шаблонов, readiness.
9. Прогнать полный тестовый набор.

## Внешний обзор

Что стоит держать в roadmap после просмотра похожих 3x-ui/VPN bot решений:

- `rs8kvn_bot`: `/readyz`, Docker health checks, QR-import, invite/trial landing, Sentry, E2E и fuzz tests.
- `MHSanaei/3x-ui`: traffic statistics, QR/share links, Telegram monitoring, backups, system status, custom subscription pages.
- исходный `3xui-shop`: admin tools, server manager, promocodes, notifications, referral/trial и backup flow.

Что добавлено в этот проход: `/readyz` и Docker health checks, потому что это минимальный и сразу полезный operational gap.
Что отложено: QR-import, invite flow и Sentry — они требуют отдельного UX/инфраструктурного решения.

## Следующий продуктовый слой

1. Добавить QR-код импорта подписки рядом с Happ deep-link в боте.
2. Улучшить invite/trial flow внутри Telegram.
3. Добавить e2e smoke-сценарий покупки и продления через Telegram-бота.

## Операционный эффект

- Меньше расхождений между Telegram-сценариями и фактическим состоянием 3X-UI.
- Покупка, продление и управление тарифом собраны в одном канале.
