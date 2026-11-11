"""База данных SQLite: один файл на диске."""
import sqlite3
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()
_con = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, tg_id INTEGER UNIQUE, name TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY, title TEXT, days INTEGER, usdt REAL, stars INTEGER);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, user_id INTEGER, plan_id INTEGER, qty INTEGER,
  key_id INTEGER, method TEXT, ext_id TEXT, pay_url TEXT,
  status TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS keys(
  id INTEGER PRIMARY KEY, user_id INTEGER, order_id INTEGER, name TEXT,
  panel_user TEXT, url TEXT, expires INTEGER, status TEXT, reminded INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS login_codes(code TEXT PRIMARY KEY, tg_id INTEGER, created INTEGER);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, created INTEGER);
"""

# Тарифы: цена за ОДНО устройство. Меняйте здесь до первого запуска
# (или потом прямо в базе, таблица plans).
DEFAULT_PLANS = [
    ("1 месяц", 30, 2.0, 150),
    ("3 месяца", 90, 5.0, 400),
    ("12 месяцев", 365, 18.0, 1400),
]


def init():
    global _con
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _con = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    _con.row_factory = sqlite3.Row
    _con.execute("PRAGMA journal_mode=WAL")
    _con.executescript(SCHEMA)
    if not q("SELECT 1 FROM plans LIMIT 1"):
        for p in DEFAULT_PLANS:
            x("INSERT INTO plans(title,days,usdt,stars) VALUES(?,?,?,?)", p)


def q(sql, args=()):
    """Запрос на чтение: список словарей."""
    with _lock:
        return [dict(r) for r in _con.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql, args=()):
    """Запрос на изменение: возвращает (id новой строки, число изменённых строк)."""
    with _lock:
        cur = _con.execute(sql, args)
        return cur.lastrowid, cur.rowcount
