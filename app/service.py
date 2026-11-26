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


def get_or_create_user(tg_id, name="", ref_by=None):
    """ref_by — Telegram ID пригласившего; учитывается только для нового клиента."""
    u = db.one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    if not u:
        ref = db.one("SELECT id FROM users WHERE tg_id=?", (ref_by,)) if ref_by and ref_by != tg_id else None
        db.x("INSERT INTO users(tg_id,name,created,ref_by) VALUES(?,?,?,?)",
             (tg_id, name, int(time.time()), ref["id"] if ref else None))
        u = db.one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    return u


def trial_available(user):
    return bool(config.TRIAL_DAYS and not user["trial_used"] and not sales_closed()
                and not db.one("SELECT 1 FROM keys WHERE user_id=?", (user["id"],)))


async def start_trial(user):
    """Бесплатный пробный ключ, один раз на человека. Возвращает ключ или None."""
    _, n = db.x("UPDATE users SET trial_used=1 WHERE id=? AND trial_used=0", (user["id"],))
    if not n or not trial_available({**user, "trial_used": 0}):
        return None
    oid, _ = db.x("INSERT INTO orders(user_id,plan_id,qty,method,usdt,stars,status,created) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (user["id"], 0, 1, "trial", 0, 0, "issuing", int(time.time())))
    try:
        keys = await issue_keys(user, oid, 1, int(time.time()) + config.TRIAL_DAYS * DAY,
                                "Пробный")
    except Exception:
        log.exception("trial %s", user["id"])
        db.x("UPDATE users SET trial_used=0 WHERE id=?", (user["id"],))
        db.x("UPDATE orders SET status='expired' WHERE id=?", (oid,))
        return None
    db.x("UPDATE orders SET status='done' WHERE id=?", (oid,))
    return keys[0]


async def give_free(user, days, qty=1, method="gift"):
    """Бесплатные ключи от владельца. Возвращает список выданных ключей."""
    if sales_closed():
        raise ValueError("VPN-панель не подключена — ключи были бы ненастоящими")
    if not (1 <= days <= 3650 and 1 <= qty <= MAX_QTY):
        raise ValueError(f"Дней от 1 до 3650, устройств от 1 до {MAX_QTY}")
    oid, _ = db.x("INSERT INTO orders(user_id,plan_id,qty,method,usdt,stars,status,created) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (user["id"], 0, qty, method, 0, 0, "issuing", int(time.time())))
    try:
        keys = await issue_keys(user, oid, qty, int(time.time()) + days * DAY)
    except Exception:
        # не paid_error: /retry рассчитан на платные заказы с тарифом
        db.x("UPDATE orders SET status='expired' WHERE id=?", (oid,))
        raise
    db.x("UPDATE orders SET status='done' WHERE id=?", (oid,))
    return keys


def new_gift_code(days, qty=1):
    if not (1 <= days <= 3650 and 1 <= qty <= MAX_QTY):
        raise ValueError(f"Дней от 1 до 3650, устройств от 1 до {MAX_QTY}")
    code = secrets.token_urlsafe(9)
    db.x("INSERT INTO gift_codes(code,days,qty,created) VALUES(?,?,?,?)",
         (code, days, qty, int(time.time())))
    return code


async def claim_gift(user, code):
    """Активирует подарочную ссылку. Каждая срабатывает один раз."""
    _, n = db.x("UPDATE gift_codes SET used_by=? WHERE code=? AND used_by IS NULL",
                (user["id"], code))
    if not n:
        return None
    g = db.one("SELECT * FROM gift_codes WHERE code=?", (code,))
    try:
        return await give_free(user, g["days"], g["qty"])
    except Exception:
        db.x("UPDATE gift_codes SET used_by=NULL WHERE code=?", (code,))   # пусть попробует позже
        raise


def keys_text(keys):
    return "\n\n".join(f"🔑 {k['name']} (до {fmt_date(k['expires'])}):\n{link(k)}" for k in keys)


async def issue_keys(user, order_id, qty, expires, base_name="Устройство"):
    """Создаёт недостающие ключи заказа в панели. Повторный вызов не дублирует ключи."""
    have = db.one("SELECT COUNT(*) c FROM keys WHERE user_id=?", (user["id"],))["c"]
    done = db.one("SELECT COUNT(*) c FROM keys WHERE order_id=?", (order_id,))["c"]
    for i in range(done, qty):
        name = base_name if qty == 1 and base_name != "Устройство" else f"{base_name} {have + i - done + 1}"
        kid, _ = db.x(
            "INSERT INTO keys(user_id,order_id,name,expires,status,sub_token) VALUES(?,?,?,?,?,?)",
            (user["id"], order_id, name, expires, "pending", secrets.token_urlsafe(18)))
        pu = f"u{user['tg_id']}k{kid}"
        try:
            url = await panel.create(pu, expires)
        except Exception:
            db.x("DELETE FROM keys WHERE id=?", (kid,))
            raise
        db.x("UPDATE keys SET panel_user=?, url=?, status='active' WHERE id=?", (pu, url, kid))
    return db.q("SELECT * FROM keys WHERE order_id=?", (order_id,))


async def add_days(user, days):
    """Добавляет дни к самому «долгому» ключу клиента, а если ключей нет — к следующей покупке."""
    k = db.one("SELECT * FROM keys WHERE user_id=? AND panel_user IS NOT NULL "
               "ORDER BY expires DESC LIMIT 1", (user["id"],))
    if not k:
        db.x("UPDATE users SET bonus_days=bonus_days+? WHERE id=?", (days, user["id"]))
        return "к следующей покупке"
    expires = max(int(time.time()), k["expires"]) + days * DAY
    await panel.set_expire(k["panel_user"], expires)
    db.x("UPDATE keys SET expires=?, status='active', reminded=0 WHERE id=?", (expires, k["id"]))
    return f"к ключу «{k['name']}», теперь он до {fmt_date(expires)}"


async def reward_referrer(user):
    """Первая оплата приглашённого — бонусные дни тому, кто пригласил."""
    if not (config.REF_DAYS and user["ref_by"]):
        return
    paid = db.one("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='done' "
                  "AND method IN ('cryptopay','stars')", (user["id"],))["c"]
    if paid != 1:
        return
    ref = db.one("SELECT * FROM users WHERE id=?", (user["ref_by"],))
    try:
        where = await add_days(ref, config.REF_DAYS)
    except Exception as e:
        log.warning("referral %s: %s", ref["id"], e)
        await notify(config.ADMIN_ID, f"⚠️ Не удалось начислить бонус клиенту {ref['tg_id']}: {e}")
        return
    await notify(ref["tg_id"], f"🎁 Ваш друг оплатил VPN. Дарим {config.REF_DAYS} дн. {where}.")


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

    amount, stars = round(plan["usdt"] * qty, 2), plan["stars"] * qty
    oid, _ = db.x(
        "INSERT INTO orders(user_id,plan_id,qty,key_id,method,usdt,stars,status,created) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (user["id"], plan_id, qty, key_id, method, amount, stars, "created", int(time.time())))

    if method == "cryptopay":
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
    """Сумма заказа: та, что была при его создании (цены могли измениться)."""
    if order.get("usdt") is not None:
        return order["usdt"], order["stars"]
    plan = db.one("SELECT * FROM plans WHERE id=?", (order["plan_id"],))   # заказы из старой базы
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
            # бонусные дни (за приглашённых друзей) добавляются к новой покупке
            expires = now + (plan["days"] + (user["bonus_days"] or 0)) * DAY
            keys = await issue_keys(user, order_id, order["qty"], expires)
            db.x("UPDATE users SET bonus_days=0 WHERE id=?", (user["id"],))
            lines = [f"🔑 {k['name']} (до {fmt_date(k['expires'])}):\n{link(k)}" for k in keys]
            text = "✅ Оплата получена. Ваши ключи:\n\n" + "\n\n".join(lines) + \
                   "\n\n" + HOWTO
        db.x("UPDATE orders SET status='done' WHERE id=?", (order_id,))
        await notify(user["tg_id"], text)
        usdt, stars = order_amounts(order)
        paid = f"{usdt:g} USDT" if order["method"] == "cryptopay" else f"{stars}⭐"
        await notify(config.ADMIN_ID, f"💰 Заказ {order_id}: {plan['title']} × {order['qty']}"
                                      f"{' (продление)' if order['key_id'] else ''}, {paid}")
        await reward_referrer(user)
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

    # VPN-сервер отвечает? Пишем владельцу, когда он упал и когда поднялся
    if now - int(db.get_meta("last_ping", 0)) >= 300:
        db.set_meta("last_ping", now)
        try:
            await panel.ping()
            alive, err = "1", ""
        except Exception as e:
            alive, err = "0", str(e)
        if db.get_meta("panel_alive", "1") != alive:
            db.set_meta("panel_alive", alive)
            await notify(config.ADMIN_ID, f"🔴 VPN-панель не отвечает: {err}" if alive == "0"
                         else "🟢 VPN-панель снова работает.")

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
