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
