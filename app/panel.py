"""Адаптер VPN-панели. Весь остальной код знает только три действия:
создать ключ, продлить ключ, отключить ключ. Панель можно заменить здесь."""
import secrets

import httpx

from . import config


class MockPanel:
    """Заглушка: VPN-сервера нет, ключи ненастоящие. Для проверки на своём компьютере."""

    async def create(self, username, expires):
        return f"https://example.invalid/sub/{username}-{secrets.token_hex(6)}"

    async def set_expire(self, username, expires):
        return True

    async def disable(self, username):
        return True


class MarzbanPanel:
    """Marzban: https://github.com/Gozargah/Marzban (API может отличаться в новых версиях)."""

    def __init__(self):
        self.token = None

    async def _req(self, method, path, **kw):
        async with httpx.AsyncClient(base_url=config.MARZBAN_URL, timeout=20) as c:
            for attempt in (1, 2):
                if not self.token:
                    r = await c.post("/api/admin/token", data={
                        "username": config.MARZBAN_USER, "password": config.MARZBAN_PASS})
                    r.raise_for_status()
                    self.token = r.json()["access_token"]
                r = await c.request(method, path,
                                    headers={"Authorization": f"Bearer {self.token}"}, **kw)
                if r.status_code == 401 and attempt == 1:
                    self.token = None      # токен устарел — получаем новый
                    continue
                r.raise_for_status()
                return r.json()

    async def create(self, username, expires):
        data = await self._req("POST", "/api/user", json={
            "username": username,
            "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
            "expire": int(expires),
            "data_limit": 0,
        })
        url = data.get("subscription_url", "")
        if url.startswith("/"):
            url = config.MARZBAN_URL + url
        return url

    async def set_expire(self, username, expires):
        await self._req("PUT", f"/api/user/{username}",
                        json={"expire": int(expires), "status": "active"})
        return True

    async def disable(self, username):
        await self._req("PUT", f"/api/user/{username}", json={"status": "disabled"})
        return True


def get_panel():
    return MarzbanPanel() if config.PANEL == "marzban" else MockPanel()


panel = get_panel()
