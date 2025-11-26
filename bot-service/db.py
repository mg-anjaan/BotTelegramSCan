# bot-service/db.py
import sqlite3
import os
from typing import Optional

DB_PATH = os.getenv("BOT_DB_PATH", "/data/bot_state.sqlite3")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("""
CREATE TABLE IF NOT EXISTS offenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    offenses INTEGER NOT NULL DEFAULT 1,
    muted INTEGER NOT NULL DEFAULT 0,
    last_offense_ts INTEGER DEFAULT (strftime('%s','now'))
)
""")
_conn.commit()

def add_offense(chat_id: int, user_id: int):
    cur = _conn.cursor()
    cur.execute("SELECT id, offenses FROM offenders WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()
    if row:
        _id, offenses = row
        offenses += 1
        cur.execute("UPDATE offenders SET offenses=?, last_offense_ts=strftime('%s','now') WHERE id=?", (offenses, _id))
    else:
        offenses = 1
        cur.execute("INSERT INTO offenders (chat_id,user_id,offenses) VALUES (?,?,?)", (chat_id, user_id, offenses))
    _conn.commit()
    return offenses

def mark_muted(chat_id: int, user_id: int):
    cur = _conn.cursor()
    cur.execute("UPDATE offenders SET muted=1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    _conn.commit()

def get_offenses(chat_id: int, user_id: int) -> int:
    cur = _conn.cursor()
    cur.execute("SELECT offenses FROM offenders WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()
    return row[0] if row else 0

def unmute_user_record(chat_id: int, user_id: int):
    cur = _conn.cursor()
    cur.execute("UPDATE offenders SET muted=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    _conn.commit()