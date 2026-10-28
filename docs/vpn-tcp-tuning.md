# VPN TCP tuning

The Israel node uses VLESS/XHTTP/REALITY over TCP 443. XHTTP already runs over
TCP; switching a client to a raw TCP transport requires a matching server inbound.

On 2026-10-28 the node was changed from Cubic/fq_codel to BBR/fq, with path-MTU
probing enabled only after detecting a black hole (`tcp_mtu_probing=1`). The
kernel already included `tcp_bbr`; no replacement kernel was installed.

The settings template is `deploy/sysctl/90-vpn-tcp.conf`. Before applying it,
record the current sysctl values and `tc qdisc show`, and preserve any existing
configuration files. Load `tcp_bbr` and `sch_fq`, add them to a file in
`/etc/modules-load.d/`, then install the template under `/etc/sysctl.d/` and run
`sysctl -p` for that file. The default queue setting applies when a device queue
is created. To apply it immediately, use `tc qdisc replace dev <interface> root fq`
after checking the actual egress interface and its existing queue policy.

Verify `net.ipv4.tcp_congestion_control`, `tc qdisc show`, and the congestion
algorithm of new VPN connections with `ss -tin`. Existing connections may retain
their old algorithm; reconnect the VPN client before comparing speeds.

Measure the same HTTPS download from the server and through a local SOCKS test
client, recording HTTP status, bytes, total time, and time to first byte. Keep
transport, payload size, destination, and client network constant. Short tests
include TLS startup overhead and fluctuate; they are not a guaranteed link rate.

To roll back, restore the saved sysctl values and queue policy and remove or
restore the two dedicated configuration files. Server-specific backups, panel
credentials, client subscription identifiers, and private keys stay outside Git.

References: [Linux IP sysctls](https://www.kernel.org/doc/html/latest/networking/ip-sysctl.html)
and [Google BBR](https://github.com/google/bbr).
