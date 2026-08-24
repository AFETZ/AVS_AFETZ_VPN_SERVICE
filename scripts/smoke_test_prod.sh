#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

status_line="$(docker compose ps bot | tail -n 1)"
if [[ "$status_line" != *"Up"* ]]; then
  echo "[smoke] bot container is not running" >&2
  docker compose ps bot
  exit 1
fi

domain_raw="$(grep -E '^BOT_DOMAIN=' .env | head -n 1 | cut -d'=' -f2- || true)"
if [[ -z "$domain_raw" ]]; then
  echo "[smoke] BOT_DOMAIN is not set in .env" >&2
  exit 1
fi

if [[ "$domain_raw" == http://* || "$domain_raw" == https://* ]]; then
  base_url="$domain_raw"
else
  base_url="https://$domain_raw"
fi

webhook_code="$(curl -k -s -o /dev/null -w "%{http_code}" "$base_url/webhook" || true)"
yookassa_code="$(curl -k -s -o /dev/null -w "%{http_code}" "$base_url/yookassa" || true)"
public_health_code="$(curl -k -s -o /dev/null -w "%{http_code}" "$base_url/healthz" || true)"
public_ready_code="$(curl -k -s -o /dev/null -w "%{http_code}" "$base_url/readyz" || true)"
internal_health_code="$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8080/healthz" || true)"
internal_ready_code="$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8080/readyz" || true)"
root_code="$(curl -k -s -o /dev/null -w "%{http_code}" "$base_url/" || true)"
cabinet_code="$(curl -k -s -o /dev/null -w "%{http_code}" "$base_url/cabinet" || true)"
connection_code="$(curl -k -s -o /dev/null -w "%{http_code}" "$base_url/connection" || true)"
geoip_code="$(curl -sS -I -o /dev/null -w "%{http_code}" "$base_url/geoip.dat" || true)"
geosite_code="$(curl -sS -I -o /dev/null -w "%{http_code}" "$base_url/geosite.dat" || true)"

echo "[smoke] public /healthz HTTP $public_health_code"
echo "[smoke] public /readyz HTTP $public_ready_code"
echo "[smoke] internal /healthz HTTP $internal_health_code"
echo "[smoke] internal /readyz HTTP $internal_ready_code"
echo "[smoke] / HTTP $root_code"
echo "[smoke] /cabinet HTTP $cabinet_code"
echo "[smoke] /connection HTTP $connection_code"
echo "[smoke] /geoip.dat HTTP $geoip_code"
echo "[smoke] /geosite.dat HTTP $geosite_code"
echo "[smoke] /webhook HTTP $webhook_code"
echo "[smoke] /yookassa HTTP $yookassa_code"

if [[ "$internal_health_code" != "200" || "$internal_ready_code" != "200" ]]; then
  echo "[smoke] internal health endpoints are unavailable" >&2
  exit 1
fi

if [[ "$public_health_code" != "404" || "$public_ready_code" != "404" ]]; then
  echo "[smoke] internal health endpoints are publicly visible" >&2
  exit 1
fi

if [[ "$root_code" != "404" || "$cabinet_code" != "404" || "$connection_code" != "400" ]]; then
  echo "[smoke] public website is visible or a bot endpoint is unavailable" >&2
  exit 1
fi

if [[ "$geoip_code" != "200" || "$geosite_code" != "200" ]]; then
  echo "[smoke] geo file proxy is unavailable" >&2
  exit 1
fi

if [[ "$webhook_code" != "405" ]]; then
  echo "[smoke] unexpected /webhook status: $webhook_code" >&2
  exit 1
fi

if [[ "$yookassa_code" != "405" ]]; then
  echo "[smoke] unexpected /yookassa status: $yookassa_code" >&2
  exit 1
fi

if docker compose logs --tail=200 bot | rg -q "Traceback|CRITICAL"; then
  echo "[smoke] warning: suspicious log lines found in last 200 lines" >&2
  docker compose logs --tail=200 bot | rg "Traceback|CRITICAL" -n || true
fi

if [[ -x scripts/check_telegram_proxy.sh ]]; then
  TELEGRAM_PROXY_RESTART=0 scripts/check_telegram_proxy.sh
fi

echo "[smoke] checks passed"
