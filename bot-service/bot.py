# bot.py — FINAL FIXED VERSION (ONLY handler fix done)
import os
import io
import logging
import asyncio
import sqlite3
from typing import Optional

import httpx
import numpy as np
from PIL import Image

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ContentType
from aiogram.types import ChatPermissions
from aiogram.filters import Command
from aiogram import F

# ---------------------------------------------------
# Logging
# ---------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

# ---------------------------------------------------
# ENV config
# ---------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0") or 0)

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.65"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing")

# ---------------------------------------------------
# Database (offenders)
# ---------------------------------------------------
DB_PATH = os.getenv("BOT_DB_PATH", "/data/bot.sqlite3")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS offenders (
    chat_id INTEGER,
    user_id INTEGER,
    offenses INTEGER DEFAULT 0,
    muted INTEGER DEFAULT 0
)
""")
conn.commit()

def add_offense(chat_id, user_id):
    cur = conn.cursor()
    cur.execute("SELECT offenses FROM offenders WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()
    if row:
        offenses = row[0] + 1
        cur.execute("UPDATE offenders SET offenses=? WHERE chat_id=? AND user_id=?", (offenses, chat_id, user_id))
    else:
        offenses = 1
        cur.execute("INSERT INTO offenders (chat_id, user_id, offenses) VALUES (?,?,?)",
                    (chat_id, user_id, offenses))
    conn.commit()
    return offenses

def reset_user(chat_id, user_id):
    conn.execute("UPDATE offenders SET offenses=0, muted=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()

# ---------------------------------------------------
# Bot setup
# ---------------------------------------------------
bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# ---------------------------------------------------
# Simple fallback NSFW detection (pixel based)
# ---------------------------------------------------
def fallback_nsfw_score(image_bytes: bytes) -> float:
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)

        # lower sensitivity (tighter NSFW)
        red_ratio = (arr[:,:,0] / (arr.sum(axis=2)+1e-6)).mean()

        score = float(min(max((red_ratio - 0.3)*2.5, 0), 1))
        return score
    except Exception as e:
        logger.error(f"Fallback error: {e}")
        return 0.0

# ---------------------------------------------------
# Telegram file download
# ---------------------------------------------------
async def download_file(file_id: str) -> bytes:
    get_file = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"

    async with httpx.AsyncClient() as client:
        meta = await client.get(get_file)
        meta.raise_for_status()
        path = meta.json()["result"]["file_path"]

        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
        file = await client.get(file_url)
        file.raise_for_status()
        return file.content

# ---------------------------------------------------
# Process image
# ---------------------------------------------------
async def process_image_message(message: types.Message):
    try:
        # detect images
        image_bytes = None
        if message.photo:
            image_bytes = await download_file(message.photo[-1].file_id)
        elif message.document and (message.document.mime_type or "").startswith("image"):
            image_bytes = await download_file(message.document.file_id)
        elif message.animation:
            image_bytes = await download_file(message.animation.file_id)

        if not image_bytes:
            return

        # fallback score
        score = fallback_nsfw_score(image_bytes)
        logger.info(f"Fallback Score = {score:.3f}")

        if score >= NSFW_THRESHOLD:
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except:
                pass

            offenses = add_offense(message.chat.id, message.from_user.id)

            # mute
            try:
                await bot.restrict_chat_member(
                    message.chat.id,
                    message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=2147483647
                )
            except:
                pass

            if OWNER_CHAT_ID:
                await bot.send_message(
                    OWNER_CHAT_ID,
                    f"🚫 NSFW Detected\nUser: <a href='tg://user?id={message.from_user.id}'>{message.from_user.id}</a>\n"
                    f"Chat: {message.chat.id}\nScore: {score:.3f}\nOffenses: {offenses}"
                )

    except Exception as e:
        logger.exception(e)

# ---------------------------------------------------
# Commands
# ---------------------------------------------------
@dp.message(Command("start"))
async def command_start(message: types.Message):
    await message.reply("👮 NSFW Scanner Active.")

@dp.message(Command("unmute"))
async def unmute_user(message: types.Message):
    if message.from_user.id != OWNER_CHAT_ID:
        return await message.reply("Not allowed.")

    if not message.reply_to_message:
        return await message.reply("Reply to a muted user's message.")

    user_id = message.reply_to_message.from_user.id
    chat_id = message.chat.id

    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(can_send_messages=True)
        )
        reset_user(chat_id, user_id)
        await message.reply(f"Unmuted {user_id}")
    except:
        await message.reply("Failed.")

# ---------------------------------------------------
# REGISTER HANDLERS — FINAL FIX (NO ERRORS)
# ---------------------------------------------------
dp.message.register(process_image_message, lambda m: bool(m.photo))
dp.message.register(process_image_message, lambda m: bool(m.document and (m.document.mime_type or "").startswith("image")))
dp.message.register(process_image_message, lambda m: bool(m.animation))

# ---------------------------------------------------
# Main
# ---------------------------------------------------
async def main():
    logger.info("Bot running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())