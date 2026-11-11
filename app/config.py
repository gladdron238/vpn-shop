"""Настройки. Все секреты читаются из файла .env (в код их не вписываем)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Простое чтение .env без лишних библиотек
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _get(name, default=""):
    return os.environ.get(name, default).strip()


BOT_TOKEN = _get("BOT_TOKEN")                  # токен бота от @BotFather
ADMIN_ID = int(_get("ADMIN_ID", "0") or 0)     # ваш Telegram ID (для /stats и /retry)
SUPPORT = _get("SUPPORT", "")                  # контакт поддержки, например @username

CRYPTOPAY_TOKEN = _get("CRYPTOPAY_TOKEN")      # токен приложения из @CryptoBot
CRYPTOPAY_TESTNET = _get("CRYPTOPAY_TESTNET", "0") == "1"   # 1 = тестовая сеть (@CryptoTestnetBot)

PANEL = _get("PANEL", "mock")                  # mock (без VPN-сервера) или marzban
MARZBAN_URL = _get("MARZBAN_URL").rstrip("/")
MARZBAN_USER = _get("MARZBAN_USER")
MARZBAN_PASS = _get("MARZBAN_PASS")

# DEMO=1 — оплата «понарошку» для проверки. На боевом сервере обязательно 0!
DEMO = _get("DEMO", "0") == "1"

SITE_URL = _get("SITE_URL", "http://localhost:8000").rstrip("/")
CORS = [o for o in _get("CORS", SITE_URL).split(",") if o]
DB_PATH = _get("DB_PATH", str(ROOT / "data" / "shop.db"))
