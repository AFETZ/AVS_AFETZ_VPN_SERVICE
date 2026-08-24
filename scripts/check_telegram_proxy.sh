#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

CHECK_URL="${TELEGRAM_PROXY_CHECK_URL:-https://api.telegram.org}"
TIMEOUT="${TELEGRAM_PROXY_CHECK_TIMEOUT:-10}"
RECHECK_DELAY="${TELEGRAM_PROXY_RECHECK_DELAY:-3}"
RESTART_UNIT="${TELEGRAM_PROXY_RESTART_UNIT:-telegram-proxy.user.service}"
RESTART_ENABLED="${TELEGRAM_PROXY_RESTART:-1}"

read_env_value() {
    local key="$1"
    local file="$2"

    [[ -f "$file" ]] || return 0

    grep -E "^[[:space:]]*${key}=" "$file" \
        | tail -n 1 \
        | cut -d= -f2- \
        | sed -e 's/^[[:space:]]*//' \
              -e 's/[[:space:]]*$//' \
              -e 's/^"//' \
              -e 's/"$//' \
              -e "s/^'//" \
              -e "s/'$//"
}

proxy_url="${BOT_PROXY_URL:-$(read_env_value BOT_PROXY_URL "$ENV_FILE")}"

curl_proxy_url() {
    local url="$1"

    case "$url" in
        socks5://*)
            printf 'socks5h://%s' "${url#socks5://}"
            ;;
        *)
            printf '%s' "$url"
            ;;
    esac
}

check_proxy() {
    local curl_args=(-fsSIL --max-time "$TIMEOUT")

    if [[ -n "$proxy_url" ]]; then
        curl_args+=(--proxy "$(curl_proxy_url "$proxy_url")")
    fi

    curl "${curl_args[@]}" "$CHECK_URL" >/dev/null
}

redacted_proxy_url() {
    local url="$1"

    if [[ "$url" =~ ^([^/]+//)([^/@]+@)?([^/]+)$ ]]; then
        printf '%s%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[3]}"
    else
        printf '%s' "$url"
    fi
}

if check_proxy; then
    echo "Telegram proxy check OK: ${CHECK_URL}"
    exit 0
fi

echo "Telegram proxy check failed: ${CHECK_URL} via $(redacted_proxy_url "${proxy_url:-direct}")" >&2

if [[ "$RESTART_ENABLED" != "1" ]]; then
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is not available; cannot restart ${RESTART_UNIT}" >&2
    exit 1
fi

echo "Restarting ${RESTART_UNIT}..." >&2
systemctl --user restart "$RESTART_UNIT"
sleep "$RECHECK_DELAY"

if check_proxy; then
    echo "Telegram proxy check OK after ${RESTART_UNIT} restart: ${CHECK_URL}"
    exit 0
fi

echo "Telegram proxy still fails after ${RESTART_UNIT} restart" >&2
exit 1
