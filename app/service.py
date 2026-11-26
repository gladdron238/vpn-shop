"""Главная логика: заказы, выдача и продление ключей, фоновые задачи."""
import logging
import secrets
import tempfile
import time
from pathlib import Path

from . import config, db, payments
from .panel import panel

log = logging.getLogger("shop")
DAY = 86400
MAX_QTY = 10

bot = None  # сюда main.py кладёт бота, чтобы слать уведомления


async def notify(tg_id, text):
    if bot and tg_id:
        try:
            await bot.send_message(tg_id, text, disable_web_page_preview=True)
        except Exception as e:  # клиент мог заблокировать бота
            log.warning("notify %s: %s", tg_id, e)


def get_or_create_user(tg_id, name=""):
    u = db.one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    if not u:
        db.x("INSERT INTO users(tg_id,name,created) VALUES(?,?,?)", (tg_id, name, int(time.time())))
        u = db.one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    return u


def sales_closed():
    """Причина, по которой сейчас нельзя продавать, или пустая строка."""
    if config.PANEL == "mock" and not config.DEMO:
        # без настоящей VPN-панели ключи ненастоящие — за деньги их выдавать нельзя
        return "Продажи временно закрыты. Загляните чуть позже."
    return ""


def stars_enabled():
    # с панелью-заглушкой звёзды списались бы по-настоящему за ненастоящий ключ
    return config.PANEL != "mock"


def plans():
    return db.q("SELECT * FROM plans ORDER BY days")


def user_keys(user_id):
    return db.q("SELECT * FROM keys WHERE user_id=? ORDER BY id", (user_id,))


def link(key):
    """Ссылка ключа для клиента: наш адрес, а не адрес VPN-сервера."""
    return f"{config.SUB_URL}/sub/{key['sub_token']}"


def fmt_date(ts):
    return time.strftime("%d.%m.%Y", time.localtime(ts))


async def create_order(user, plan_id, qty, method, key_id=None):
    """Создаёт заказ. key_id задан — это продление одного ключа, иначе покупка qty новых."""
    plan = db.one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not plan:
        raise ValueError("Нет такого тарифа")
    if key_id:
        key = db.one("SELECT * FROM keys WHERE id=? AND user_id=?", (key_id, user["id"]))
        if not key:
            raise ValueError("Ключ не найден")
        qty = 1
    qty = int(qty)
    if not 1 <= qty <= MAX_QTY:
        raise ValueError("Неверное число устройств")
    if method not in ("cryptopay", "stars"):
        raise ValueError("Неизвестный способ оплаты")
    if sales_closed():
        raise ValueError(sales_closed())
    if method == "stars" and not stars_enabled():
        raise ValueError("Оплата звёздами включится, когда подключим VPN-сервер")

    oid, _ = db.x(
        "INSERT INTO orders(user_id,plan_id,qty,key_id,method,status,created) VALUES(?,?,?,?,?,?,?)",
        (user["id"], plan_id, qty, key_id, method, "created", int(time.time())))

    if method == "cryptopay":
        amount = round(plan["usdt"] * qty, 2)
        if config.CRYPTOPAY_TOKEN:
            ext_id, url = await payments.create_invoice(amount, oid, f"VPN: {plan['title']} x{qty}")
        elif config.DEMO:
            ext_id, url = f"demo{oid}", f"{config.SITE_URL}/demo/pay/{oid}"
        else:
            db.x("UPDATE orders SET status='expired' WHERE id=?", (oid,))
            raise ValueError("Оплата USDT пока не подключена")
        db.x("UPDATE orders SET ext_id=?, pay_url=? WHERE id=?", (ext_id, url, oid))
    return db.one("SELECT * FROM orders WHERE id=?", (oid,))


def order_amounts(order):
    plan = db.one("SELECT * FROM plans WHERE id=?", (order["plan_id"],))
    return round(plan["usdt"] * order["qty"], 2), plan["stars"] * order["qty"]


HOWTO = ("Как подключиться:\n"
         "1. Установите приложение: Hiddify (Android, iPhone, Windows, Mac), "
         "v2rayNG (Android) или Streisand (iPhone).\n"
         "2. Скопируйте ссылку ключа и в приложении нажмите «+» → «Добавить из буфера».\n"
         "3. Нажмите «Подключиться».\n"
         "Один ключ — одно устройство. Лишние ключи можно переслать близким.")


async def fulfill(order_id, from_status="created"):
    """Оплата подтверждена -> выдать или продлить ключи. Срабатывает ровно один раз."""
    _, changed = db.x("UPDATE orders SET status='issuing' WHERE id=? AND status=?",
                      (order_id, from_status))
    if not changed:
        return False  # уже обработан — повторное уведомление игнорируем
    order = db.one("SELECT * FROM orders WHERE id=?", (order_id,))
    plan = db.one("SELECT * FROM plans WHERE id=?", (order["plan_id"],))
    user = db.one("SELECT * FROM users WHERE id=?", (order["user_id"],))
    now = int(time.time())
    try:
        if order["key_id"]:
            key = db.one("SELECT * FROM keys WHERE id=?", (order["key_id"],))
            expires = max(now, key["expires"]) + plan["days"] * DAY
            await panel.set_expire(key["panel_user"], expires)
            db.x("UPDATE keys SET expires=?, status='active', reminded=0 WHERE id=?",
                 (expires, key["id"]))
            text = f"✅ Ключ «{key['name']}» продлён до {fmt_date(expires)}."
        else:
            expires = now + plan["days"] * DAY
            have = db.one("SELECT COUNT(*) c FROM keys WHERE user_id=?", (user["id"],))["c"]
            # если прошлая попытка оборвалась — не выдаём уже выданное повторно
            done = db.one("SELECT COUNT(*) c FROM keys WHERE order_id=?", (order_id,))["c"]
            for i in range(done, order["qty"]):
                name = f"Устройство {have + i - done + 1}"
                kid, _ = db.x(
                    "INSERT INTO keys(user_id,order_id,name,expires,status,sub_token) "
                    "VALUES(?,?,?,?,?,?)",
                    (user["id"], order_id, name, expires, "pending", secrets.token_urlsafe(18)))
                pu = f"u{user['tg_id']}k{kid}"
                try:
                    url = await panel.create(pu, expires)
                except Exception:
                    db.x("DELETE FROM keys WHERE id=?", (kid,))
                    raise
                db.x("UPDATE keys SET panel_user=?, url=?, status='active' WHERE id=?",
                     (pu, url, kid))
            lines = [f"🔑 {k['name']} (до {fmt_date(k['expires'])}):\n{link(k)}"
                     for k in db.q("SELECT * FROM keys WHERE order_id=?", (order_id,))]
            text = "✅ Оплата получена. Ваши ключи:\n\n" + "\n\n".join(lines) + \
                   "\n\n" + HOWTO
        db.x("UPDATE orders SET status='done' WHERE id=?", (order_id,))
        await notify(user["tg_id"], text)
        usdt, stars = order_amounts(order)
        paid = f"{usdt:g} USDT" if order["method"] == "cryptopay" else f"{stars}⭐"
        await notify(config.ADMIN_ID, f"💰 Заказ {order_id}: {plan['title']} × {order['qty']}"
                                      f"{' (продление)' if order['key_id'] else ''}, {paid}")
        return True
    except Exception as e:
        log.exception("fulfill %s", order_id)
        db.x("UPDATE orders SET status='paid_error' WHERE id=?", (order_id,))
        await notify(user["tg_id"], "Оплата получена, но ключ пока не выдан. "
                                    "Мы уже разбираемся, ничего повторно платить не нужно.")
        await notify(config.ADMIN_ID, f"⚠️ Заказ {order_id} оплачен, ключ не выдан: {e}\n"
                                      f"После починки: /retry {order_id}")
        return False


async def confirm_cryptopay(invoice_id, amount):
    """Счёт оплачен (пришло уведомление или нашли при опросе)."""
    order = db.one("SELECT * FROM orders WHERE ext_id=? AND method='cryptopay'", (str(invoice_id),))
    if not order:
        return False
    need, _ = order_amounts(order)
    if float(amount) + 1e-9 < need:
        await notify(config.ADMIN_ID, f"⚠️ Заказ {order['id']}: сумма {amount} меньше {need}")
        return False
    return await fulfill(order["id"])


async def tick():
    """Раз в минуту: добираем оплаты, закрываем старые счета, напоминаем о продлении."""
    now = int(time.time())
    pending = db.q("SELECT * FROM orders WHERE status='created' AND method='cryptopay'")
    if config.CRYPTOPAY_TOKEN and pending:
        try:
            paid = await payments.paid_invoices([o["ext_id"] for o in pending if o["ext_id"]])
            for inv_id, amount in paid.items():
                await confirm_cryptopay(inv_id, amount)
        except Exception as e:
            log.warning("poll cryptopay: %s", e)
    db.x("UPDATE orders SET status='expired' WHERE status='created' AND created<?", (now - 3900,))

    for k in db.q("SELECT k.*, u.tg_id FROM keys k JOIN users u ON u.id=k.user_id "
                  "WHERE k.status='active'"):
        left = k["expires"] - now
        if left <= 0:
            db.x("UPDATE keys SET status='expired' WHERE id=?", (k["id"],))
            try:
                await panel.disable(k["panel_user"])
            except Exception as e:
                log.warning("disable %s: %s", k["panel_user"], e)
            await notify(k["tg_id"], f"⏰ Срок ключа «{k['name']}» закончился. "
                                     "Продлить можно в разделе «Мои ключи».")
        elif left <= DAY and k["reminded"] < 2:
            db.x("UPDATE keys SET reminded=2 WHERE id=?", (k["id"],))
            await notify(k["tg_id"], f"⏳ Ключ «{k['name']}» закончится завтра. "
                                     "Продлите его в разделе «Мои ключи».")
        elif left <= 3 * DAY and k["reminded"] < 1:
            db.x("UPDATE keys SET reminded=1 WHERE id=?", (k["id"],))
            await notify(k["tg_id"], f"⏳ Ключ «{k['name']}» закончится через 3 дня.")
    db.x("DELETE FROM login_codes WHERE created<?", (now - 600,))
    db.x("DELETE FROM sessions WHERE created<?", (now - 30 * DAY,))

    if now - int(db.get_meta("last_backup", 0)) >= DAY:
        if await send_backup():
            db.set_meta("last_backup", now)


async def send_backup(chat_id=None):
    """Отправляет копию базы владельцу в Telegram. Возвращает True, если получилось."""
    chat_id = chat_id or config.ADMIN_ID
    if not (bot and chat_id):
        return False
    from aiogram.types import FSInputFile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / time.strftime("shop-%Y-%m-%d.db")
        db.backup(str(path))
        try:
            await bot.send_document(chat_id, FSInputFile(path),
                                    caption="🗄 Резервная копия базы. Храните её в секрете.")
            return True
        except Exception as e:
            log.warning("backup: %s", e)
            return False
