"""Session authentication for 3x-ui v3 panels using the pinned py3xui client."""
import httpx
from urllib.parse import quote
from py3xui import AsyncApi


class CsrfAsyncApi(AsyncApi):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._csrf_token = ""
        for part in (self.client, self.inbound, self.database, self.server):
            original = part._request_with_retry

            async def request(method, url, headers, _original=original, **options):
                headers = {**headers, "X-CSRF-Token": self._csrf_token}
                url = url.replace("/inbounds/getClientTraffics/", "/clients/traffic/")
                return await _original(method, url, headers, **options)

            part._request_with_retry = request
        self.client.update = self._update_client
        self.client.add = self._add_clients

    async def _add_clients(self, inbound_id, clients):
        for client in clients:
            await self.client._post(self.client._url("panel/api/clients/add"), {}, {
                "client": client.model_dump(by_alias=True), "inboundIds": [inbound_id],
            })

    async def _update_client(self, client_uuid, client):
        # v3 uses email-addressed clients and a flat JSON update payload.
        # Include zero/false values: omitting them would lose disable/unlimited.
        url = self.client._url(f"panel/api/clients/update/{quote(client.email, safe='')}")
        await self.client._post(url, {}, client.model_dump(by_alias=True))

    async def login(self):
        base = self.client.host.rstrip("/")
        # TLS verification stays enabled. Tokens and cookies remain in memory.
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(f"{base}/csrf-token")
            response.raise_for_status()
            token = response.json()["obj"]
            response = await client.post(
                f"{base}/login",
                json={"username": self.client.username, "password": self.client.password},
                headers={"X-CSRF-Token": token},
            )
            response.raise_for_status()
            if not response.json().get("success"):
                raise ValueError("3x-ui login failed")
            response = await client.get(f"{base}/csrf-token")
            response.raise_for_status()
            self._csrf_token = response.json()["obj"]
            # py3xui knows the panel's supported session cookie names.
            response.cookies.update(client.cookies)
            cookie = await self.client._get_cookie(response)
            if not cookie:
                raise ValueError("3x-ui session cookie missing")
            self.cookie_name = self.client.cookie_name
            self.session = cookie
