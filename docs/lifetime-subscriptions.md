# Lifetime subscriptions

Administrators can use Admin tools -> Users -> user card to enable or disable a
lifetime subscription. The existing client must exist on every profile server.
The action sets 3x-ui expiry to zero and keeps the device and traffic limits.
Disable suspends VPN clients on every profile server without blocking the bot.
All servers are read before changing anything. Failed updates return an error
and attempt to restore previous client settings. Successful enable also clears
any scheduled tariff change. Ordinary extensions preserve unlimited expiry;
explicitly assigning a new finite subscription replaces it.

Per-panel credentials can be supplied in the secret `XUI_SERVER_CREDENTIALS`
environment variable, keyed by the database server name:

```json
{"Example":{"username":"USER","password":"SECRET","token":null,"csrf":true}}
```

`csrf: true` selects the 3x-ui v3 session and client API adapter. Other servers
continue to use the existing py3xui integration and shared credentials. HTTPS
certificate verification remains enabled.

A server with `max_clients=0` is reserved: it is excluded from automatic
assignment and public server selection. It remains available to its assigned
user and is appended to that user's normal multi-server subscription.

The deployed Israel server uses VLESS/XHTTP/REALITY on TCP 443. Its panel and
subscription endpoints use a Let's Encrypt IP certificate, automatically
renewed by acme.sh. Credentials and private keys are stored outside Git.
