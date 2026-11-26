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

BUY, KEYS, HELP, INVITE = "🛒 Купить VPN", "🔑 Мои ключи", "💬 Поддержка", "🎁 Пригласить"
MENU = {BUY, KEYS, HELP, INVITE, "🌐 Кабинет"}


def menu():
    rows = [[KeyboardButton(text=BUY), KeyboardButton(text=KEYS)], [KeyboardButton(text=HELP)]]
    if config.REF_DAYS:
        rows[1].append(KeyboardButton(text=INVITE))
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
    arg = command.args or ""
    ref = int(arg[4:]) if arg.startswith("ref_") and arg[4:].isdigit() else None
    user = service.get_or_create_user(m.from_user.id, m.from_user.full_name, ref_by=ref)
    if arg.startswith("gift_"):
        try:
            keys = await service.claim_gift(user, arg[5:])
        except Exception:
            await m.answer("Не получилось выдать подарок, попробуйте позже.", reply_markup=menu())
            return
        await m.answer(f"🎁 Вам подарили VPN!\n\n{service.keys_text(keys)}\n\n{service.HOWTO}"
                       if keys else "Эта подарочная ссылка уже использована.",
                       reply_markup=menu(), disable_web_page_preview=True)
        if keys:
            await service.notify(config.ADMIN_ID, f"🎁 Подарок активировал {m.from_user.full_name} "
                                                  f"(id {m.from_user.id})")
        return
    if arg.startswith("login_"):
        # вход на сайт: спрашиваем явно, чтобы чужая ссылка не открыла кому-то ваш кабинет
        await m.answer("Войти на сайт под вашим аккаунтом?\n"
                       "Нажимайте, только если вы сами только что нажали «Войти» на сайте.",
                       reply_markup=kb([[("✅ Да, это я", f"login:{arg[6:]}")]]))
        return
    pay = "USDT через @CryptoBot или звёздами Telegram" if service.stars_enabled() \
        else "USDT через @CryptoBot"
    await m.answer(f"Привет! Здесь можно купить VPN для себя и близких.\nОплата: {pay}.",
                   reply_markup=menu())
    if service.trial_available(user):
        await m.answer(f"Можно сначала попробовать бесплатно — {config.TRIAL_DAYS} дн.",
                       reply_markup=kb([[("🎁 Попробовать бесплатно", "trial")]]))


@r.callback_query(F.data == "trial")
async def trial(c: CallbackQuery):
    await c.answer()
    k = await service.start_trial(user_of(c))
    if not k:
        await c.message.edit_text("Пробный ключ уже был выдан или сейчас недоступен. "
                                  "Купить можно в меню «Купить VPN».")
        return
    await c.message.edit_text(f"🎁 Пробный ключ до {service.fmt_date(k['expires'])}:\n"
                              f"{service.link(k)}\n\n{service.HOWTO}",
                              disable_web_page_preview=True)


@r.message(F.text == INVITE)
async def invite(m: Message):
    me = await m.bot.me()
    n = db.one("SELECT COUNT(*) c FROM users WHERE ref_by=?", (user_of(m)["id"],))["c"]
    await m.answer(f"Перешлите другу эту ссылку. Когда он оплатит VPN, вам добавится "
                   f"{config.REF_DAYS} дн. бесплатно.\n\nhttps://t.me/{me.username}?start=ref_{m.from_user.id}"
                   f"\n\nПо вашей ссылке пришло: {n}", disable_web_page_preview=True)


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
    if config.ADMIN_ID:
        contact = "Если что-то не работает — просто напишите вопрос сюда, в этот чат. Мы ответим."
    elif config.SUPPORT:
        contact = f"Вопросы и проблемы: {config.SUPPORT}"
    else:
        contact = "Поддержка скоро появится."
    await m.answer(service.HOWTO + "\n\n" + contact)


# --- команды владельца ---
@r.message(Command("stats"), F.from_user.id == config.ADMIN_ID)
async def stats(m: Message):
    u = db.one("SELECT COUNT(*) c FROM users")["c"]
    k = db.one("SELECT COUNT(*) c FROM keys WHERE status='active'")["c"]
    o = db.one("SELECT COUNT(*) c FROM orders WHERE status='done' "
               "AND method IN ('cryptopay','stars')")["c"]
    t = db.one("SELECT COUNT(*) c FROM orders WHERE method='trial'")["c"]
    g = db.one("SELECT COUNT(*) c FROM orders WHERE method='gift' AND status='done'")["c"]
    e = db.one("SELECT COUNT(*) c FROM orders WHERE status='paid_error'")["c"]
    rev = {"cryptopay": 0.0, "stars": 0}
    month = int(time.time()) - 30 * service.DAY
    for row in db.q("SELECT * FROM orders WHERE status='done' AND created>? "
                    "AND method IN ('cryptopay','stars')", (month,)):
        usdt, stars = service.order_amounts(row)
        rev[row["method"]] += usdt if row["method"] == "cryptopay" else stars
    await m.answer(f"Клиентов: {u}\nАктивных ключей: {k}\nОплаченных заказов: {o}\nС ошибкой: {e}\n"
                   f"Пробных ключей: {t}, подарков: {g}\n"
                   f"Выручка за 30 дней: {rev['cryptopay']:g} USDT, {rev['stars']}⭐\n"
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
                   "/backup — прислать копию базы (и так приходит раз в сутки)\n\n"
                   "Бесплатно:\n"
                   "/give 123456789 30 — ключ на 30 дней клиенту с этим ID\n"
                   "/give 123456789 30 2 — то же, 2 устройства\n"
                   "/give 30 — ответом на сообщение клиента в поддержку, ID не нужен\n"
                   "/addtime 123456789 7 — добавить 7 дней к ключу клиента\n"
                   "/gift 30 — одноразовая ссылка-подарок для любого человека\n\n"
                   "Вопросы клиентов приходят сюда. Чтобы ответить — ответьте на сообщение.")


def target_and_numbers(m: Message, args):
    """Кому (Telegram ID из команды или из сообщения, на которое ответили) и числа после."""
    nums = [int(a) for a in args.split() if a.isdigit()]
    if len(nums) != len(args.split()):
        return None, []
    if m.reply_to_message:
        row = db.one("SELECT tg_id FROM support_msgs WHERE msg_id=?",
                     (m.reply_to_message.message_id,))
        if row:
            return row["tg_id"], nums
    if len(nums) >= 2:              # без ответа на сообщение первое число — Telegram ID
        return nums[0], nums[1:]
    return None, nums


@r.message(Command("give"), F.from_user.id == config.ADMIN_ID)
async def admin_give(m: Message, command: CommandObject):
    tg_id, nums = target_and_numbers(m, command.args or "")
    if not tg_id or not 1 <= len(nums) <= 2:
        await m.answer("Пример: /give 123456789 30 (ID клиента, дней) или /give 123456789 30 2 "
                       "(ещё и число устройств). ID клиента виден в его сообщениях в поддержку.\n"
                       "Если человек ещё не заходил в бота — используйте /gift 30.")
        return
    user = db.one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    if not user:
        await m.answer("Этот человек ещё не запускал бота, ему не дойдёт сообщение. "
                       "Сделайте ссылку-подарок: /gift " + " ".join(map(str, nums)))
        return
    try:
        keys = await service.give_free(user, nums[0], nums[1] if len(nums) > 1 else 1)
    except Exception as e:
        await m.answer(f"Не получилось: {e}")
        return
    await service.notify(tg_id, f"🎁 Вам подарили VPN!\n\n{service.keys_text(keys)}\n\n{service.HOWTO}")
    await m.answer(f"Готово: {len(keys)} ключ(а) на {nums[0]} дн. для {user['name'] or tg_id}.")


@r.message(Command("addtime"), F.from_user.id == config.ADMIN_ID)
async def admin_addtime(m: Message, command: CommandObject):
    tg_id, nums = target_and_numbers(m, command.args or "")
    user = db.one("SELECT * FROM users WHERE tg_id=?", (tg_id,)) if tg_id else None
    if not user or len(nums) != 1 or not 1 <= nums[0] <= 3650:
        await m.answer("Пример: /addtime 123456789 7 — добавить 7 дней к ключу клиента "
                       "(или ответом на его сообщение: /addtime 7)")
        return
    if not db.one("SELECT 1 FROM keys WHERE user_id=? AND panel_user IS NOT NULL", (user["id"],)):
        await m.answer("У клиента нет ключей. Выдайте новый: /give " + f"{tg_id} {nums[0]}")
        return
    try:
        where = await service.add_days(user, nums[0])
    except Exception as e:
        await m.answer(f"Не получилось: {e}")
        return
    await service.notify(tg_id, f"🎁 Вам добавили {nums[0]} дн. {where}.")
    await m.answer(f"Готово: +{nums[0]} дн. {where}.")


@r.message(Command("gift"), F.from_user.id == config.ADMIN_ID)
async def admin_gift(m: Message, command: CommandObject):
    nums = [int(a) for a in (command.args or "").split() if a.isdigit()]
    if not 1 <= len(nums) <= 2:
        await m.answer("Пример: /gift 30 — ссылка на ключ на 30 дней, /gift 30 3 — на 3 устройства")
        return
    try:
        code = service.new_gift_code(nums[0], nums[1] if len(nums) > 1 else 1)
    except ValueError as e:
        await m.answer(str(e))
        return
    me = await m.bot.me()
    await m.answer(f"🎁 Ссылка-подарок на {nums[0]} дн."
                   f"{f', устройств: {nums[1]}.' if len(nums) > 1 else ''} "
                   f"Сработает один раз — перешлите её человеку:\n\n"
                   f"https://t.me/{me.username}?start=gift_{code}", disable_web_page_preview=True)


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


# --- поддержка через бота: клиент пишет сюда, владелец отвечает ответом на сообщение ---
# Клиенты не видят личный аккаунт владельца.
@r.message(F.from_user.id == config.ADMIN_ID, F.reply_to_message)
async def admin_reply(m: Message):
    row = db.one("SELECT tg_id FROM support_msgs WHERE msg_id=?", (m.reply_to_message.message_id,))
    if not row:
        return
    try:
        await m.bot.send_message(row["tg_id"], "💬 Ответ поддержки:")
        await m.copy_to(row["tg_id"])
        await m.answer("Отправлено.")
    except Exception as e:
        await m.answer(f"Не доставлено: {e}")


@r.message(F.chat.type == "private", F.from_user.id != config.ADMIN_ID)
async def to_support(m: Message):
    if (m.text or "").startswith("/") or m.text in MENU or not config.ADMIN_ID:
        await m.answer("Выберите действие в меню ниже.", reply_markup=menu())
        return
    u = user_of(m)
    keys = db.one("SELECT COUNT(*) c FROM keys WHERE user_id=? AND status='active'", (u["id"],))["c"]
    head = await m.bot.send_message(
        config.ADMIN_ID, f"✉️ {m.from_user.full_name} (id {m.from_user.id}, активных ключей: {keys}). "
                         "Чтобы ответить — ответьте на сообщение ниже.")
    copy = await m.copy_to(config.ADMIN_ID)
    for mid in (head.message_id, copy.message_id):
        db.x("INSERT OR REPLACE INTO support_msgs(msg_id,tg_id) VALUES(?,?)", (mid, m.from_user.id))
    await m.answer("Передали в поддержку, ответ придёт сюда.")
