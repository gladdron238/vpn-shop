"""Telegram-бот: покупка, мои ключи, продление, оплата USDT и звёздами."""
import asyncio
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           BotCommand, KeyboardButton, LabeledPrice, Message,
                           PreCheckoutQuery, ReplyKeyboardMarkup, WebAppInfo)

from . import config, db, panel, service

r = Router()

COMMANDS = [BotCommand(command="start", description="Главное меню"),
            BotCommand(command="buy", description="Купить VPN"),
            BotCommand(command="keys", description="Мои ключи"),
            BotCommand(command="help", description="Как подключиться и поддержка")]

BUY, KEYS, HELP = "🛒 Купить VPN", "🔑 Мои ключи", "💬 Поддержка"


def menu():
    rows = [[KeyboardButton(text=BUY), KeyboardButton(text=KEYS)], [KeyboardButton(text=HELP)]]
    if config.SITE_URL.startswith("https://"):
        rows[1].append(KeyboardButton(text="🌐 Кабинет", web_app=WebAppInfo(url=config.SITE_URL)))
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def price(p, n=1):
    s = f"{p['usdt'] * n:g} USDT"
    return s + f" / {p['stars'] * n}⭐" if service.stars_enabled() else s


def user_of(event):
    return service.get_or_create_user(event.from_user.id, event.from_user.full_name)


@r.message(CommandStart())
async def start(m: Message, command: CommandObject):
    user_of(m)
    arg = command.args or ""
    if arg.startswith("login_"):
        # вход на сайт: спрашиваем явно, чтобы чужая ссылка не открыла кому-то ваш кабинет
        await m.answer("Войти на сайт под вашим аккаунтом?\n"
                       "Нажимайте, только если вы сами только что нажали «Войти» на сайте.",
                       reply_markup=kb([[("✅ Да, это я", f"login:{arg[6:]}")]]))
        return
    await m.answer("Привет! Здесь можно купить VPN для себя и близких.\n"
                   "Оплата: USDT через @CryptoBot или звёздами Telegram.", reply_markup=menu())


@r.callback_query(F.data.startswith("login:"))
async def confirm_login(c: CallbackQuery):
    # привязываем одноразовый код к этому человеку
    _, n = db.x("UPDATE login_codes SET tg_id=? WHERE code=? AND tg_id IS NULL AND created>?",
                (c.from_user.id, c.data[6:], int(time.time()) - 600))
    await c.message.edit_text("✅ Вход подтверждён, вернитесь на сайт." if n
                              else "Код устарел. Нажмите «Войти» на сайте ещё раз.")
    await c.answer()


@r.message(F.text == BUY)
@r.message(Command("buy"))
async def buy(m: Message):
    if service.sales_closed():
        await m.answer(service.sales_closed())
        return
    rows = [[(f"{p['title']} — {price(p)} за устройство", f"p:{p['id']}")]
            for p in service.plans()]
    await m.answer("Выберите срок:", reply_markup=kb(rows))


@r.callback_query(F.data.startswith("p:"))
async def pick_qty(c: CallbackQuery):
    pid = c.data.split(":")[1]
    await c.message.edit_text("Сколько устройств? На каждое будет свой ключ — "
                              "их можно раздать родным.",
                              reply_markup=kb([[(str(n), f"q:{pid}:{n}:0") for n in (1, 2, 3, 5)]]))
    await c.answer()


@r.callback_query(F.data.startswith("q:"))
async def pick_method(c: CallbackQuery):
    _, pid, qty, key = c.data.split(":")
    p = db.one("SELECT * FROM plans WHERE id=?", (pid,))
    if not p:
        await c.answer("Тариф больше не продаётся", show_alert=True)
        return
    n = int(qty)
    rows = [[("💵 USDT (CryptoBot)", f"m:{pid}:{qty}:{key}:c")]]
    if service.stars_enabled():
        rows.append([("⭐ Звёзды Telegram", f"m:{pid}:{qty}:{key}:s")])
    await c.message.edit_text(f"{p['title']}, устройств: {n}\nК оплате: {price(p, n)}",
                              reply_markup=kb(rows))
    await c.answer()


@r.callback_query(F.data.startswith("m:"))
async def pay(c: CallbackQuery):
    _, pid, qty, key, how = c.data.split(":")
    user = user_of(c)
    method = "cryptopay" if how == "c" else "stars"
    try:
        order = await service.create_order(user, int(pid), int(qty), method, int(key) or None)
    except Exception as e:
        await c.answer(str(e), show_alert=True)
        return
    if method == "cryptopay":
        await c.message.edit_text(
            "Счёт создан и действует 1 час. После оплаты ключ придёт сюда автоматически.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Оплатить", url=order["pay_url"])]]))
    else:
        _, stars = service.order_amounts(order)
        p = db.one("SELECT * FROM plans WHERE id=?", (pid,))
        await c.bot.send_invoice(
            c.from_user.id, title="VPN-доступ",
            description=f"{p['title']}, устройств: {order['qty']}",
            payload=str(order["id"]), currency="XTR",
            prices=[LabeledPrice(label="VPN", amount=stars)])
        await c.message.delete()
    await c.answer()


@r.pre_checkout_query()
async def pre_checkout(pq: PreCheckoutQuery):
    """Telegram спрашивает перед списанием звёзд: всё ли в порядке с заказом."""
    order = db.one("SELECT * FROM orders WHERE id=?", (pq.invoice_payload,))
    ok = bool(order and order["status"] == "created" and order["method"] == "stars"
              and pq.total_amount == service.order_amounts(order)[1])
    await pq.answer(ok=ok, error_message=None if ok else "Заказ устарел, создайте новый.")


@r.message(F.successful_payment)
async def paid_stars(m: Message):
    sp = m.successful_payment
    db.x("UPDATE orders SET ext_id=? WHERE id=?", (sp.telegram_payment_charge_id, sp.invoice_payload))
    oid = int(sp.invoice_payload)
    # звёзды уже списаны — выдаём, даже если заказ успел «устареть» за секунды до оплаты
    await service.fulfill(oid) or await service.fulfill(oid, from_status="expired")


@r.message(F.text == KEYS)
@r.message(Command("keys"))
async def my_keys(m: Message):
    keys = service.user_keys(user_of(m)["id"])
    if not keys:
        await m.answer("Ключей пока нет. Нажмите «Купить VPN».")
        return
    for k in keys:
        state = "активен до" if k["status"] == "active" else "истёк"
        await m.answer(f"🔑 {k['name']} — {state} {service.fmt_date(k['expires'])}\n{service.link(k)}",
                       reply_markup=kb([[("Продлить", f"r:{k['id']}")]]),
                       disable_web_page_preview=True)


@r.callback_query(F.data.startswith("r:"))
async def renew(c: CallbackQuery):
    kid = c.data.split(":")[1]
    rows = [[(f"{p['title']} — {price(p)}", f"q:{p['id']}:1:{kid}")]
            for p in service.plans()]
    await c.message.answer("На какой срок продлить?", reply_markup=kb(rows))
    await c.answer()


@r.message(F.text == HELP)
@r.message(Command("help"))
async def support(m: Message):
    contact = f"Вопросы и проблемы: {config.SUPPORT}" if config.SUPPORT \
        else "Поддержка скоро появится."
    await m.answer(service.HOWTO + "\n\n" + contact)


# --- команды владельца ---
@r.message(Command("stats"), F.from_user.id == config.ADMIN_ID)
async def stats(m: Message):
    u = db.one("SELECT COUNT(*) c FROM users")["c"]
    k = db.one("SELECT COUNT(*) c FROM keys WHERE status='active'")["c"]
    o = db.one("SELECT COUNT(*) c FROM orders WHERE status='done'")["c"]
    e = db.one("SELECT COUNT(*) c FROM orders WHERE status='paid_error'")["c"]
    rev = {"cryptopay": 0.0, "stars": 0}
    month = int(time.time()) - 30 * service.DAY
    for row in db.q("SELECT o.method, o.qty, p.usdt, p.stars FROM orders o "
                    "JOIN plans p ON p.id=o.plan_id WHERE o.status='done' AND o.created>?", (month,)):
        rev[row["method"]] += row["usdt"] * row["qty"] if row["method"] == "cryptopay" \
            else row["stars"] * row["qty"]
    await m.answer(f"Клиентов: {u}\nАктивных ключей: {k}\nОплаченных заказов: {o}\nС ошибкой: {e}\n"
                   f"Выручка за 30 дней (по текущим ценам): {rev['cryptopay']:g} USDT, {rev['stars']}⭐\n"
                   "Все команды: /admin")


@r.message(Command("retry"), F.from_user.id == config.ADMIN_ID)
async def retry(m: Message, command: CommandObject):
    if not (command.args or "").isdigit():
        await m.answer("Пример: /retry 12")
        return
    ok = await service.fulfill(int(command.args), from_status="paid_error")
    await m.answer("Готово, ключ выдан." if ok else "Не получилось (или заказ не в статусе ошибки).")


@r.message(Command("admin"), F.from_user.id == config.ADMIN_ID)
async def admin_help(m: Message):
    await m.answer("Команды владельца:\n"
                   "/stats — клиенты, ключи, выручка\n"
                   "/plans — тарифы с номерами\n"
                   "/price 1 2.5 200 — тариф №1: 2.5 USDT и 200⭐\n"
                   "/retry 12 — выдать ключ по заказу с ошибкой\n"
                   "/refund 12 — вернуть звёзды по заказу и отключить его ключи\n"
                   "/send текст — рассылка всем клиентам\n"
                   "/backup — прислать копию базы (и так приходит раз в сутки)")


@r.message(Command("plans"), F.from_user.id == config.ADMIN_ID)
async def admin_plans(m: Message):
    await m.answer("\n".join(f"№{p['id']}: {p['title']} ({p['days']} дн.) — "
                             f"{p['usdt']:g} USDT / {p['stars']}⭐" for p in service.plans()))


@r.message(Command("price"), F.from_user.id == config.ADMIN_ID)
async def admin_price(m: Message, command: CommandObject):
    try:
        pid, usdt, stars = (command.args or "").split()
        pid, usdt, stars = int(pid), round(float(usdt.replace(",", ".")), 2), int(stars)
        assert usdt > 0 and stars > 0
    except Exception:
        await m.answer("Пример: /price 1 2.5 200 (номер тарифа из /plans, USDT, звёзды)")
        return
    _, n = db.x("UPDATE plans SET usdt=?, stars=? WHERE id=?", (usdt, stars, pid))
    await m.answer("Цена изменена. Неоплаченные счета остаются по старой цене."
                   if n else "Нет такого тарифа, см. /plans")


@r.message(Command("send"), F.from_user.id == config.ADMIN_ID)
async def admin_send(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        await m.answer("Пример: /send Работы на сервере завтра с 3:00 до 3:30")
        return
    users = db.q("SELECT tg_id FROM users WHERE tg_id>1")
    await m.answer(f"Отправляю {len(users)} клиентам…")
    ok = 0
    for u in users:
        try:
            await m.bot.send_message(u["tg_id"], text)
            ok += 1
        except Exception:
            pass  # заблокировал бота
        await asyncio.sleep(0.05)  # Telegram ограничивает частоту сообщений
    await m.answer(f"Доставлено: {ok} из {len(users)}")


@r.message(Command("backup"), F.from_user.id == config.ADMIN_ID)
async def admin_backup(m: Message):
    if not await service.send_backup(m.chat.id):
        await m.answer("Не получилось отправить копию, подробности в логах сервера.")


@r.message(Command("refund"), F.from_user.id == config.ADMIN_ID)
async def admin_refund(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    o = db.one("SELECT * FROM orders WHERE id=?", (arg,)) if arg.isdigit() else None
    if not o or o["method"] != "stars" or o["status"] not in ("done", "paid_error"):
        await m.answer("Пример: /refund 12 — только оплаченные звёздами заказы.\n"
                       "USDT возвращается вручную через @CryptoBot.")
        return
    user = db.one("SELECT * FROM users WHERE id=?", (o["user_id"],))
    try:
        await m.bot.refund_star_payment(user["tg_id"], o["ext_id"])
    except Exception as e:
        await m.answer(f"Telegram не принял возврат: {e}")
        return
    db.x("UPDATE orders SET status='refunded' WHERE id=?", (o["id"],))
    if o["key_id"]:  # продление — отматываем срок назад
        plan = db.one("SELECT * FROM plans WHERE id=?", (o["plan_id"],))
        k = db.one("SELECT * FROM keys WHERE id=?", (o["key_id"],))
        expires = k["expires"] - plan["days"] * service.DAY
        db.x("UPDATE keys SET expires=? WHERE id=?", (expires, k["id"]))
        try:
            await panel.panel.set_expire(k["panel_user"], max(expires, int(time.time()) + 60))
        except Exception as e:
            await m.answer(f"Срок в панели поправьте вручную: {e}")
    else:
        for k in db.q("SELECT * FROM keys WHERE order_id=? AND status='active'", (o["id"],)):
            db.x("UPDATE keys SET status='expired' WHERE id=?", (k["id"],))
            try:
                await panel.panel.disable(k["panel_user"])
            except Exception as e:
                await m.answer(f"Ключ {k['id']} отключите в панели вручную: {e}")
    await service.notify(user["tg_id"], f"Звёзды по заказу {o['id']} возвращены.")
    await m.answer("Возврат сделан.")
