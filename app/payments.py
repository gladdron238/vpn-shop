"""Crypto Pay (@CryptoBot): счета в USDT. Документация: https://help.crypt.bot/crypto-pay-api"""
import hashlib
import hmac

import httpx

from . import config

BASE = "https://testnet-pay.crypt.bot/api" if config.CRYPTOPAY_TESTNET else "https://pay.crypt.bot/api"


async def _call(method, **params):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{BASE}/{method}", json=params,
                         headers={"Crypto-Pay-API-Token": config.CRYPTOPAY_TOKEN})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Crypto Pay: {data}")
        return data["result"]


async def create_invoice(amount, order_id, description):
    """Создаёт счёт. Возвращает (id счёта, ссылка на оплату)."""
    inv = await _call("createInvoice", asset="USDT", amount=f"{amount:.2f}",
                      payload=str(order_id), description=description, expires_in=3600)
    url = inv.get("bot_invoice_url") or inv.get("pay_url")
    return str(inv["invoice_id"]), url


async def paid_invoices(invoice_ids):
    """Какие из счетов уже оплачены: {id счёта: сумма}."""
    if not invoice_ids:
        return {}
    res = await _call("getInvoices", invoice_ids=",".join(invoice_ids), status="paid")
    return {str(i["invoice_id"]): float(i["amount"]) for i in res.get("items", [])}


def verify_signature(body: bytes, signature: str) -> bool:
    """Проверка, что уведомление действительно пришло от Crypto Pay."""
    if not config.CRYPTOPAY_TOKEN or not signature:
        return False
    secret = hashlib.sha256(config.CRYPTOPAY_TOKEN.encode()).digest()
    good = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, signature)
