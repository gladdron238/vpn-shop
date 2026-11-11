"""Telegram-бот: покупка, мои ключи, продление, оплата USDT и звёздами."""
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, LabeledPrice, Message, PreCheckoutQuery,
                           ReplyKeyboardMarkup, WebAppInfo)

from . import config, db, service

r = Router()

BUY, KEYS, HELP = "🛒 Купить VPN", "🔑 Мои ключи", "💬 Поддержка"


def menu():
    rows = [[KeyboardButton(text=BUY), KeyboardButton(text=KEYS)], [KeyboardButton(text=HELP)]]
    if config.SITE_URL.startswith("https://"):
        rows[1].append(KeyboardButton(text="🌐 Кабинет", web_app=WebAppInfo(url=config.SITE_URL)))
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def user_of(event):
    return service.get_or_create_user(event.from_user.id, event.from_user.full_name)


@r.message(CommandStart())
async def start(m: Message, command: CommandObject):
    user_of(m)
    arg = command.args or ""
    if arg.startswith("login_"):
        # вход на сайт: привязываем одноразовый код к этому человеку
        _, n = db.x("UPDATE login_codes SET tg_id=? WHERE code=? AND tg_id IS NULL",
                    (m.from_user.id, arg[6:]))
        await m.answer("✅ Вход подтверждён, вернитесь на сайт." if n
                       else "Код устарел. Нажмите «Войти» на сайте ещё раз.", reply_markup=menu())
        return
    await m.answer("Привет! Здесь можно купить VPN для себя и близких.\n"
                   "Оплата: USDT через @CryptoBot или звёздами Telegram.", reply_markup=menu())


@r.message(F.text == BUY)
async def buy(m: Message):
    rows = [[(f"{p['title']} — {p['usdt']:g} USDT / {p['stars']}⭐ за устройство", f"p:{p['id']}")]
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
    n = int(qty)
    await c.message.edit_text(
        f"{p['title']}, устройств: {n}\nК оплате: {p['usdt'] * n:g} USDT или {p['stars'] * n}⭐",
        reply_markup=kb([[("💵 USDT (CryptoBot)", f"m:{pid}:{qty}:{key}:c")],
                         [("⭐ Звёзды Telegram", f"m:{pid}:{qty}:{key}:s")]]))
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
    await service.fulfill(int(sp.invoice_payload))


@r.message(F.text == KEYS)
async def my_keys(m: Message):
    keys = service.user_keys(user_of(m)["id"])
    if not keys:
        await m.answer("Ключей пока нет. Нажмите «Купить VPN».")
        return
    for k in keys:
        state = "активен до" if k["status"] == "active" else "истёк"
        await m.answer(f"🔑 {k['name']} — {state} {service.fmt_date(k['expires'])}\n{k['url']}",
                       reply_markup=kb([[("Продлить", f"r:{k['id']}")]]),
                       disable_web_page_preview=True)


@r.callback_query(F.data.startswith("r:"))
async def renew(c: CallbackQuery):
    kid = c.data.split(":")[1]
    rows = [[(f"{p['title']} — {p['usdt']:g} USDT / {p['stars']}⭐", f"q:{p['id']}:1:{kid}")]
            for p in service.plans()]
    await c.message.answer("На какой срок продлить?", reply_markup=kb(rows))
    await c.answer()


@r.message(F.text == HELP)
async def support(m: Message):
    await m.answer(f"Напишите нам: {config.SUPPORT}" if config.SUPPORT
                   else "Поддержка скоро появится.")


# --- команды владельца ---
@r.message(Command("stats"), F.from_user.id == config.ADMIN_ID)
async def stats(m: Message):
    u = db.one("SELECT COUNT(*) c FROM users")["c"]
    k = db.one("SELECT COUNT(*) c FROM keys WHERE status='active'")["c"]
    o = db.one("SELECT COUNT(*) c FROM orders WHERE status='done'")["c"]
    e = db.one("SELECT COUNT(*) c FROM orders WHERE status='paid_error'")["c"]
    await m.answer(f"Клиентов: {u}\nАктивных ключей: {k}\nОплаченных заказов: {o}\nС ошибкой: {e}")


@r.message(Command("retry"), F.from_user.id == config.ADMIN_ID)
async def retry(m: Message, command: CommandObject):
    if not (command.args or "").isdigit():
        await m.answer("Пример: /retry 12")
        return
    ok = await service.fulfill(int(command.args), from_status="paid_error")
    await m.answer("Готово, ключ выдан." if ok else "Не получилось (или заказ не в статусе ошибки).")
