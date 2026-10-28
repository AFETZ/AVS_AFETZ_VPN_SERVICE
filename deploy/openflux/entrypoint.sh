#!/bin/sh
set -eu
: "${OPENFLUX_DOC_URL:?Set OPENFLUX_DOC_URL to the shared legacy Yandex document URL}"
case "$OPENFLUX_DOC_URL" in
    https://docs.yandex.ru/*|https://docs.yandex.com/*|https://disk.yandex.ru/*|https://disk.yandex.com/*|https://disk.360.yandex.ru/*) ;;
    *) echo 'Expected an HTTPS Yandex document URL' >&2; exit 1 ;;
esac

# Required by the upstream raw TCP stack; never install this in the host namespace.
# Keep this before established traffic: the kernel's RST can belong to a tracked flow.
iptables -A OUTPUT -p tcp --tcp-flags RST RST -j DROP
# Docker DNS also sends replies to loopback after its internal DNAT.
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Docker's DNS resolver remains reachable; new tunnel traffic may not reach host/LAN services.
# Docker DNATs port 53 to a per-container high port before the filter chain.
iptables -A OUTPUT -d 127.0.0.11 -j ACCEPT
for subnet in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
    iptables -A OUTPUT -d "$subnet" -j REJECT
done
exec /usr/local/bin/openflux --exit-node --transport yandex --url "$OPENFLUX_DOC_URL"
