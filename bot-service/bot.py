# bot.py - NSFW Moderation Bot using HuggingFace API
import os
import io
import logging
import asyncio
import sqlite3
from typing import Optional

import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ContentType
from aiogram.types import ChatPermissions
from aiogram.filters import Command
from aiogram import F

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

# -------------------- Environment Variables --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")  # HuggingFace token
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0") or 0)
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.70"))  # default 0.7
MUTE_DAYS = int(os.getenv("MUTE_DAYS", "9999"))

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN not set!")

if not HF_TOKEN:
    raise SystemExit("❌ HF_TOKEN (HuggingFace API Key) not set!")

# HuggingFace model
HF_MODEL_URL = "https://api-inference.huggingface.co/models/Falconsai/nsfw_image_detection"
HF_HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}

# -------------------- SQLite DB --------------------
DB_PATH = os.getenv("BOT_DB_PATH", "/data/bot_state.sqlite3")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

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


def add_offense(chat_id: int, user_id: int) -> int:
    cur = _conn.cursor()
    cur.execute("SELECT id, offenses FROM offenders WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()
    if row:
        _id, offenses = row
        offenses += 1
        cur.execute("UPDATE offenders SET offenses=?, last_offense_ts=strftime('%s','now') WHERE id=?", (offenses, _id))
    else:
        offenses = 1
        cur.execute("INSERT INTO offenders (chat_id,user_id,offenses) VALUES (?,?,?)",
                    (chat_id, user_id, offenses))
    _conn.commit()
    return offenses


def mark_muted(chat_id: int, user_id: int):
    cur = _conn.cursor()
    cur.execute("UPDATE offenders SET muted=1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    _conn.commit()


# -------------------- Telegram Bot Setup --------------------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

PERMANENT_UNTIL = 2147483647  # max timestamp


# -------------------- Image Score Using HuggingFace --------------------
async def get_hf_score(image_bytes: bytes) -> float:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(HF_MODEL_URL, headers=HF_HEADERS, files={"file": image_bytes})

    if resp.status_code != 200:
        logger.error(f"HuggingFace error: {resp.status_code} {resp.text}")
        return 0.0

    data = resp.json()
    # Expected output: list of dicts → pick NSFW score
    try:
        for item in data:
            if item["label"].lower() in ["porn", "nsfw", "sexy", "hentai"]:
                return float(item["score"])
        return 0.0
    except:
        logger.error("Unexpected HuggingFace response format")
        return 0.0


# -------------------- Telegram File Download --------------------
async def tg_download(file_id: str) -> bytes:
    getfile = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(getfile, params={"file_id": file_id})
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]

    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=20) as c:
        f = await c.get(file_url)
        f.raise_for_status()
        return f.content


# -------------------- Main Image Handler --------------------
async def process_image(message: types.Message):
    chat = message.chat
    user = message.from_user

    try:
        if message.photo:
            file_id = message.photo[-1].file_id
        elif message.document and (message.document.mime_type or "").startswith("image"):
            file_id = message.document.file_id
        else:
            return
    except:
        return

    try:
        img_bytes = await tg_download(file_id)
    except Exception:
        logger.exception("Failed to download telegram image")
        return

    score = await get_hf_score(img_bytes)
    logger.info(f"Score={score:.3f} user={user.id} chat={chat.id}")

    if score < NSFW_THRESHOLD:
        return  # safe image

    # Delete message
    try:
        await bot.delete_message(chat.id, message.message_id)
    except:
        logger.exception("Failed to delete NSFW image")

    # Offense tracking
    offenses = add_offense(chat.id, user.id)

    # Mute permanently
    try:
        await bot.restrict_chat_member(
            chat.id,
            user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=PERMANENT_UNTIL
        )
        mark_muted(chat.id, user.id)
    except:
        logger.exception("Mute failed")

    # Notify owner
    if OWNER_CHAT_ID:
        try:
            await bot.send_message(
                OWNER_CHAT_ID,
                f"🚫 NSFW Detected\n"
                f"👤 User: <a href='tg://user?id={user.id}'>{user.full_name}</a>\n"
                f"💬 Chat: {chat.title or chat.id}\n"
                f"🔥 Score: {score:.3f}\n"
                f"⚠️ Offenses: {offenses}"
            )
        except:
            pass


# -------------------- Handlers --------------------
@dp.message(F.content_type == ContentType.PHOTO)
async def on_photo(message: types.Message):
    await process_image(message)


@dp.message(F.content_type == ContentType.DOCUMENT)
async def on_doc(message: types.Message):
    if message.document.mime_type.startswith("image"):
        await process_image(message)


@dp.message(Command("start"))
async def on_start(message: types.Message):
    await message.reply("🤖 NSFW Scanner active.\nSend an image to test.")


@dp.message(Command("status"))
async def status(message: types.Message):
    if message.from_user.id != OWNER_CHAT_ID:
        return await message.reply("Unauthorized.")
    await message.reply("Bot running.")


@dp.message(Command("unmute"))
async def unmute(message: types.Message):
    if message.from_user.id != OWNER_CHAT_ID:
        return await message.reply("Only owner can unmute.")

    if not message.reply_to_message:
        return await message.reply("Reply to a user's message with /unmute")

    user_id = message.reply_to_message.from_user.id
    chat_id = message.chat.id

    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(can_send_messages=True),
            until_date=None
        )
        cur = _conn.cursor()
        cur.execute("UPDATE offenders SET muted=0, offenses=0 WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id))
        _conn.commit()

        await message.reply(f"User {user_id} is now unmuted.")
    except:
        await message.reply("Failed to unmute.")


# -------------------- Start Bot --------------------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())