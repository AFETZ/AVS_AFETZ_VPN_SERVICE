# VPN TCP settings and rollback

The Israel node uses VLESS/XHTTP/REALITY over TCP 443. XHTTP already runs over
TCP; switching a client to a raw TCP transport requires a matching server inbound.

## Current baseline

`deploy/sysctl/90-vpn-tcp.conf` records the restored settings: Cubic, fq_codel,
and `tcp_mtu_probing=0`. Keep these settings on the Israel node unless a future
change has demonstrated both reliable reconnections and sustained transfers.

On 2026-10-28 a BBR/fq trial with `tcp_mtu_probing=1` passed initial speed tests,
but later a Windows VPN probe timed out while SSH, 3x-ui, and the subscription
remained available. Reverting all three settings restored that probe. Xray was
then restarted to clear old connections; both installed Windows Xray versions
connected, and 10 MB and 50 MB HTTPS downloads completed. This does not isolate
which individual setting caused the regression. Short speed tests alone did
not establish stability, so the BBR trial is no longer the deployment default.

## Applying the baseline

Record the current sysctl values and `tc qdisc show`, and preserve existing
configuration files. Install the template under `/etc/sysctl.d/` and run
`sysctl -p` for that file. After checking the actual egress interface and its
existing queue policy, apply the queue with
`tc qdisc replace dev <interface> root fq_codel`.

Restore or remove the dedicated BBR modules-load file if one was created for
the trial. Existing TCP sessions can retain the previous congestion algorithm;
reconnect clients, or restart Xray during recovery to clear those sessions.
Verify the new connections with `ss -tin`, not just the default sysctl value.

## Validation

Check the actual VPN egress IP, repeated fresh connections, and full HTTPS
downloads from the affected client network. Record HTTP status, received bytes,
total time, and time to first byte. ICMP responses from a desktop TUN can be
synthetic, so they are not sufficient proof that the remote VPN is available.
Keep transport, payload size, destination, and client network constant when
comparing throughput. Server-specific backups, panel credentials, subscription
identifiers, and private keys stay outside Git.

Reference: [Linux IP sysctls](https://www.kernel.org/doc/html/latest/networking/ip-sysctl.html).
