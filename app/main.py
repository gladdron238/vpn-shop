"""Бэкенд: API для сайта, приём уведомлений об оплате, запуск бота и фоновых задач."""
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, payments, service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("shop")
state = {"bot_username": ""}


async def scheduler():
    while True:
        try:
            await service.tick()
        except Exception:
            log.exception("tick")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    db.init()
    tasks = [asyncio.create_task(scheduler())]
    bot = None
    if config.BOT_TOKEN:
        from aiogram import Bot, Dispatcher
        from .bot import r
        bot = Bot(config.BOT_TOKEN)
        state["bot_username"] = (await bot.get_me()).username
        service.bot = bot
        dp = Dispatcher()
        dp.include_router(r)
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
        log.info("Бот @%s запущен", state["bot_username"])
    else:
        log.warning("BOT_TOKEN не задан — бот выключен, работает только сайт")
    if config.DEMO:
        log.warning("ВКЛЮЧЁН DEMO-РЕЖИМ: оплата понарошку. Не для боевого сервера!")
    yield
    for t in tasks:
        t.cancel()
    if bot:
        await bot.session.close()


app = FastAPI(title="VPN shop", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS,
                   allow_methods=["*"], allow_headers=["*"])


# ---------- вход ----------
def new_session(user_id):
    token = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)",
         (token, user_id, int(time.time())))
    return token


def current_user(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    u = db.one("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
               (token,)) if token else None
    if not u:
        raise HTTPException(401, "Нужно войти")
    return u


@app.get("/api/info")
def info():
    return {"bot": state["bot_username"], "demo": config.DEMO}


@app.post("/api/auth/start")
def auth_start():
    """Сайт просит одноразовый код; человек подтверждает его в боте."""
    code = secrets.token_urlsafe(16)
    db.x("INSERT INTO login_codes(code,created) VALUES(?,?)", (code, int(time.time())))
    link = f"https://t.me/{state['bot_username']}?start=login_{code}" if state["bot_username"] else ""
    return {"code": code, "link": link}


@app.get("/api/auth/check")
def auth_check(code: str):
    row = db.one("SELECT * FROM login_codes WHERE code=?", (code,))
    if not row or not row["tg_id"]:
        return {"ok": False}
    db.x("DELETE FROM login_codes WHERE code=?", (code,))
    user = service.get_or_create_user(row["tg_id"])
    return {"ok": True, "token": new_session(user["id"])}


class WebAppAuth(BaseModel):
    init_data: str


@app.post("/api/auth/webapp")
def auth_webapp(body: WebAppAuth):
    """Вход внутри Telegram (Mini App): проверяем подпись данных от Telegram."""
    if not config.BOT_TOKEN:
        raise HTTPException(400, "Бот не настроен")
    data = dict(parse_qsl(body.init_data, keep_blank_values=True))
    their = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    good = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, their) or time.time() - int(data.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "Подпись не сошлась")
    tg = json.loads(data.get("user", "{}"))
    user = service.get_or_create_user(int(tg["id"]), tg.get("first_name", ""))
    return {"ok": True, "token": new_session(user["id"])}


@app.post("/api/auth/demo")
def auth_demo():
    """Только в DEMO: вход без Telegram, чтобы посмотреть сайт."""
    if not config.DEMO:
        raise HTTPException(404)
    return {"ok": True, "token": new_session(service.get_or_create_user(1, "Демо")["id"])}


# ---------- магазин ----------
@app.get("/api/plans")
def get_plans():
    return service.plans()


@app.get("/api/me/keys")
def my_keys(user=Depends(current_user)):
    return [{"id": k["id"], "name": k["name"], "url": k["url"], "expires": k["expires"],
             "status": k["status"]} for k in service.user_keys(user["id"])]


class Rename(BaseModel):
    name: str


@app.post("/api/me/keys/{key_id}/rename")
def rename_key(key_id: int, body: Rename, user=Depends(current_user)):
    name = body.name.strip()[:40]
    if not name:
        raise HTTPException(400, "Пустое имя")
    _, n = db.x("UPDATE keys SET name=? WHERE id=? AND user_id=?", (name, key_id, user["id"]))
    if not n:
        raise HTTPException(404, "Ключ не найден")
    return {"ok": True}


class NewOrder(BaseModel):
    plan_id: int
    qty: int = 1
    key_id: int | None = None


@app.post("/api/orders")
async def new_order(body: NewOrder, user=Depends(current_user)):
    try:
        o = await service.create_order(user, body.plan_id, body.qty, "cryptopay", body.key_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    usdt, _ = service.order_amounts(o)
    return {"id": o["id"], "pay_url": o["pay_url"], "amount": usdt, "status": o["status"]}


@app.get("/api/orders/{order_id}")
def get_order(order_id: int, user=Depends(current_user)):
    o = db.one("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user["id"]))
    if not o:
        raise HTTPException(404, "Заказ не найден")
    return {"id": o["id"], "status": o["status"]}


# ---------- уведомления об оплате ----------
@app.post("/webhooks/cryptopay")
async def cryptopay_webhook(request: Request):
    body = await request.body()
    if not payments.verify_signature(body, request.headers.get("crypto-pay-api-signature", "")):
        raise HTTPException(401, "Плохая подпись")
    upd = json.loads(body)
    if upd.get("update_type") == "invoice_paid":
        inv = upd["payload"]
        if inv.get("status") == "paid" and inv.get("asset") == "USDT":
            await service.confirm_cryptopay(inv["invoice_id"], inv["amount"])
    return {"ok": True}


@app.get("/demo/pay/{order_id}", response_class=HTMLResponse)
async def demo_pay(order_id: int):
    """Только в DEMO: «оплатить» заказ без денег."""
    if not config.DEMO:
        raise HTTPException(404)
    ok = await service.fulfill(order_id)
    return f"<h2>Демо-оплата заказа {order_id}: {'успешно' if ok else 'уже обработан'}</h2>" \
           "<p>Вернитесь на сайт или в бота.</p>"


# сайт раздаётся этим же сервером (на Cloudflare Pages можно выложить папку site отдельно)
app.mount("/", StaticFiles(directory=str(config.ROOT / "site"), html=True), name="site")
