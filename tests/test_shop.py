import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from app import config, db, service


def run(coro):
    return asyncio.run(coro)


def test_health_and_info(client):
    assert client.get("/api/health").json() == {"ok": True}
    info = client.get("/api/info").json()
    assert info["demo"] is True and info["closed"] == ""


def test_demo_purchase_flow(client, auth):
    plans = client.get("/api/plans").json()
    assert len(plans) == 3
    before = len(client.get("/api/me/keys", headers=auth).json())
    o = client.post("/api/orders", headers=auth, json={"plan_id": plans[0]["id"], "qty": 2}).json()
    assert o["amount"] == plans[0]["usdt"] * 2
    assert client.get(f"/api/orders/{o['id']}", headers=auth).json()["status"] == "created"

    assert "успешно" in client.get(f"/demo/pay/{o['id']}").text
    assert "уже обработан" in client.get(f"/demo/pay/{o['id']}").text   # второй раз ничего не даёт
    assert client.get(f"/api/orders/{o['id']}", headers=auth).json()["status"] == "done"
    keys = client.get("/api/me/keys", headers=auth).json()
    assert len(keys) == before + 2

    k = keys[-1]
    assert client.post(f"/api/me/keys/{k['id']}/rename", headers=auth, json={"name": "Мама"}).status_code == 200
    assert client.get("/api/me/keys", headers=auth).json()[-1]["name"] == "Мама"

    # продление добавляет дни к текущему сроку
    r = client.post("/api/orders", headers=auth,
                    json={"plan_id": plans[0]["id"], "key_id": k["id"]}).json()
    client.get(f"/demo/pay/{r['id']}")
    after = client.get("/api/me/keys", headers=auth).json()[-1]
    assert after["expires"] == k["expires"] + plans[0]["days"] * service.DAY


def test_needs_login(client):
    assert client.get("/api/me/keys").status_code == 401
    assert client.post("/api/orders", json={"plan_id": 1}).status_code == 401


def test_bad_orders(client, auth):
    assert client.post("/api/orders", headers=auth, json={"plan_id": 999}).status_code == 400
    assert client.post("/api/orders", headers=auth, json={"plan_id": 1, "qty": 50}).status_code == 400
    assert client.post("/api/orders", headers=auth,
                       json={"plan_id": 1, "key_id": 99999}).status_code == 400


def test_no_real_sales_without_panel(client, auth, monkeypatch):
    monkeypatch.setattr(config, "DEMO", False)
    assert client.get("/api/info").json()["closed"]
    assert client.post("/api/orders", headers=auth, json={"plan_id": 1}).status_code == 400
    assert client.get("/demo/pay/1").status_code == 404
    assert client.post("/api/auth/demo").status_code == 404


def test_stars_off_with_mock_panel(client):
    user = service.get_or_create_user(1)
    try:
        run(service.create_order(user, 1, 1, "stars"))
        assert False, "звёзды не должны приниматься с панелью-заглушкой"
    except ValueError:
        pass


def test_site_login_via_bot(client):
    s = client.post("/api/auth/start").json()
    assert client.get("/api/auth/check", params={"code": s["code"]}).json() == {"ok": False}
    db.x("UPDATE login_codes SET tg_id=? WHERE code=?", (555, s["code"]))   # то, что делает бот
    r = client.get("/api/auth/check", params={"code": s["code"]}).json()
    assert r["ok"] and r["token"]
    assert client.get("/api/auth/check", params={"code": s["code"]}).json() == {"ok": False}


def test_webapp_signature(client, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "123:abc")
    data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 777, "first_name": "Ann"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", b"123:abc", hashlib.sha256).digest()
    good = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    ok = client.post("/api/auth/webapp", json={"init_data": urlencode({**data, "hash": good})})
    assert ok.status_code == 200 and ok.json()["token"]
    bad = client.post("/api/auth/webapp", json={"init_data": urlencode({**data, "hash": "0" * 64})})
    assert bad.status_code == 401


def test_cryptopay_webhook(client, auth, monkeypatch):
    monkeypatch.setattr(config, "CRYPTOPAY_TOKEN", "tok")
    user = service.get_or_create_user(1)
    oid, _ = db.x("INSERT INTO orders(user_id,plan_id,qty,method,ext_id,status,created) "
                  "VALUES(?,?,?,?,?,?,?)", (user["id"], 1, 1, "cryptopay", "inv42", "created",
                                            int(time.time())))
    body = json.dumps({"update_type": "invoice_paid", "payload": {
        "invoice_id": "inv42", "status": "paid", "asset": "USDT", "amount": "100"}}).encode()
    assert client.post("/webhooks/cryptopay", content=body,
                       headers={"crypto-pay-api-signature": "bad"}).status_code == 401
    sig = hmac.new(hashlib.sha256(b"tok").digest(), body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/cryptopay", content=body,
                       headers={"crypto-pay-api-signature": sig}).status_code == 200
    assert db.one("SELECT status FROM orders WHERE id=?", (oid,))["status"] == "done"


def test_underpaid_invoice_ignored(client):
    user = service.get_or_create_user(1)
    oid, _ = db.x("INSERT INTO orders(user_id,plan_id,qty,method,ext_id,status,created) "
                  "VALUES(?,?,?,?,?,?,?)", (user["id"], 1, 3, "cryptopay", "inv43", "created",
                                            int(time.time())))
    assert run(service.confirm_cryptopay("inv43", "0.5")) is False
    assert db.one("SELECT status FROM orders WHERE id=?", (oid,))["status"] == "created"


def test_tick_expires_keys_and_orders(client):
    user = service.get_or_create_user(1)
    kid, _ = db.x("INSERT INTO keys(user_id,name,panel_user,url,expires,status) VALUES(?,?,?,?,?,?)",
                  (user["id"], "старый", "u1old", "x", int(time.time()) - 5, "active"))
    oid, _ = db.x("INSERT INTO orders(user_id,plan_id,qty,method,status,created) VALUES(?,?,?,?,?,?)",
                  (user["id"], 1, 1, "cryptopay", "created", int(time.time()) - 5000))
    run(service.tick())
    assert db.one("SELECT status FROM keys WHERE id=?", (kid,))["status"] == "expired"
    assert db.one("SELECT status FROM orders WHERE id=?", (oid,))["status"] == "expired"


def test_backup(tmp_path, client):
    dst = tmp_path / "copy.db"
    db.backup(str(dst))
    import sqlite3
    assert sqlite3.connect(dst).execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 3


def test_subscription_link(client, auth):
    o = client.post("/api/orders", headers=auth, json={"plan_id": 1}).json()
    client.get(f"/demo/pay/{o['id']}")
    url = client.get("/api/me/keys", headers=auth).json()[-1]["url"]
    assert url.startswith(config.SUB_URL + "/sub/") and "example.invalid" not in url
    r = client.get(url.removeprefix(config.SUB_URL))
    assert r.status_code == 200 and b"demo" in r.content
    assert client.get("/sub/nope").status_code == 404


def test_price_change_keeps_invoice_amount(client, auth):
    o = client.post("/api/orders", headers=auth, json={"plan_id": 2}).json()
    order = db.one("SELECT * FROM orders WHERE id=?", (o["id"],))
    db.x("UPDATE plans SET usdt=usdt*10 WHERE id=2")        # владелец поднял цену
    try:
        assert service.order_amounts(order)[0] == o["amount"]
        db.x("UPDATE orders SET ext_id='inv-price' WHERE id=?", (o["id"],))
        assert run(service.confirm_cryptopay("inv-price", o["amount"])) is True
    finally:
        db.x("UPDATE plans SET usdt=usdt/10 WHERE id=2")


def test_trial_once(client):
    u = service.get_or_create_user(9001, "Новый")
    assert service.trial_available(u)
    k = run(service.start_trial(u))
    assert k and k["name"] == "Пробный"
    assert k["expires"] - time.time() <= config.TRIAL_DAYS * service.DAY
    u = service.get_or_create_user(9001)
    assert not service.trial_available(u)
    assert run(service.start_trial(u)) is None


def test_referral_bonus(client):
    inviter = service.get_or_create_user(9100, "Друг")
    keys = run(service.issue_keys(inviter, 0, 1, int(time.time()) + service.DAY, "Мой"))
    before = keys[0]["expires"]
    friend = service.get_or_create_user(9101, "Новичок", ref_by=9100)
    assert friend["ref_by"] == inviter["id"]
    assert service.get_or_create_user(9100, ref_by=9101)["ref_by"] is None   # старых не переписываем
    assert service.get_or_create_user(9102, ref_by=9102)["ref_by"] is None   # сам себя нельзя

    for _ in range(2):   # бонус только за первую оплату друга
        o = run(service.create_order(friend, 1, 1, "cryptopay"))
        run(service.fulfill(o["id"]))
    after = db.one("SELECT expires FROM keys WHERE id=?", (keys[0]["id"],))["expires"]
    assert after == before + config.REF_DAYS * service.DAY


def test_referral_bonus_without_keys_goes_to_next_purchase(client):
    inviter = service.get_or_create_user(9200)
    friend = service.get_or_create_user(9201, ref_by=9200)
    run(service.fulfill(run(service.create_order(friend, 1, 1, "cryptopay"))["id"]))
    assert service.get_or_create_user(9200)["bonus_days"] == config.REF_DAYS
    o = run(service.create_order(inviter, 1, 1, "cryptopay"))
    run(service.fulfill(o["id"]))
    k = db.one("SELECT * FROM keys WHERE order_id=?", (o["id"],))
    assert k["expires"] - time.time() > (30 + config.REF_DAYS) * service.DAY - 60
    assert service.get_or_create_user(9200)["bonus_days"] == 0


def test_bot_handlers_import():
    from app import bot
    assert bot.r.message.handlers and bot.COMMANDS
