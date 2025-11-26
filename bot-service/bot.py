#!/usr/bin/env python3
"""
Telegram NSFW Scanner Bot (HuggingFace-powered)
- Detects porn, hentai, explicit, sexy, NSFW
- Deletes image, permanently mutes offender
- Logs score
- Stable & lightweight (aiogram v3)
"""

import os
import io
import logging
import asyncio
import httpx
import sqlite3

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ContentType
from aiogram.filters import Command
from aiogram.types import ChatPermissions

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

# -------------------- ENV ------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.65"))
HF_TOKEN = os.getenv("HF_TOKEN")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing")
if not HF_TOKEN:
    raise SystemExit("HF_TOKEN missing (HuggingFace Access Token)")

# -------------------- Database -------------------
DB_PATH = "/data/nsfw.sqlite3"
os.makedirs("/data", exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS offenders (
    chat_id INTEGER,
    user_id INTEGER,
    offenses INTEGER DEFAULT 1,
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
        cur.execute("INSERT INTO offenders(chat_id, user_id, offenses) VALUES (?,?,1)", (chat_id, user_id))

    conn.commit()
    return offenses


def reset_user(chat_id, user_id):
    conn.execute("UPDATE offenders SET offenses=0, muted=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()


# -------------------- Bot Setup ------------------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

PERMANENT_UNTIL = 2147483647  # almost permanent Unix timestamp


# -------------------- Download Telegram File --------------------
async def download_file(file_id: str) -> bytes:
    get_file_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    async with httpx.AsyncClient() as client:
        r = await client.get(get_file_url, params={"file_id": file_id})
        r.raise_for_status()
        path = r.json()["result"]["file_path"]

        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}"
        r2 = await client.get(file_url)
        r2.raise_for_status()
        return r2.content


# -------------------- HuggingFace Detection --------------------
async def get_hf_score(image_bytes: bytes) -> float:
    url = "https://api-inference.huggingface.co/models/Falconsai/nsfw-detector"

    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Accept": "application/json"
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, files={
            "file": ("image.jpg", image_bytes, "image/jpeg")
        })

    # If HF returned HTML => ERROR
    if resp.headers.get("content-type", "").startswith("text/html"):
        logger.error("HF returned HTML → wrong token or model busy")
        return 0.0

    try:
        data = resp.json()
    except:
        logger.error("HF returned non-JSON")
        return 0.0

    # Extract highest NSFW score
    best = 0.0
    for item in data:
        label = item.get("label", "").lower()
        score = float(item.get("score", 0.0))

        if label in ["porn", "sexy", "nsfw", "hentai", "explicit"]:
            if score > best:
                best = score

    return best


# -------------------- Main Image Handler --------------------
async def handle_image(msg: types.Message):
    user = msg.from_user
    chat = msg.chat

    # Download image
    try:
        if msg.photo:
            file_id = msg.photo[-1].file_id
        elif msg.document and (msg.document.mime_type or "").startswith("image"):
            file_id = msg.document.file_id
        else:
            return

        img = await download_file(file_id)

    except:
        logger.exception("Failed to download Telegram image")
        return

    # HF Score
    score = await get_hf_score(img)
    logger.info(f"Score={score:.3f} user={user.id} chat={chat.id}")

    if score >= NSFW_THRESHOLD:
        # Delete image
        try:
            await bot.delete_message(chat.id, msg.message_id)
        except:
            logger.error("Delete failed")

        offenses = add_offense(chat.id, user.id)

        # Mute permanently
        try:
            await bot.restrict_chat_member(
                chat.id,
                user.id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_other_messages=False
                ),
                until_date=PERMANENT_UNTIL,
            )
        except:
            logger.error("Mute failed")

        # Notify owner
        if OWNER_CHAT_ID:
            await bot.send_message(
                OWNER_CHAT_ID,
                f"⚠️ NSFW detected\nUser: <a href='tg://user?id={user.id}'>{user.id}</a>\nScore: {score:.3f}\nOffenses: {offenses}"
            )


# -------------------- Handlers --------------------
@dp.message(F.content_type == ContentType.PHOTO)
async def photo(msg: types.Message):
    await handle_image(msg)

@dp.message(F.content_type == ContentType.DOCUMENT)
async def doc(msg: types.Message):
    if msg.document.mime_type.startswith("image"):
        await handle_image(msg)

@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.reply("NSFW Scanner active ✔️")

@dp.message(Command("unmute"))
async def unmute(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return await msg.reply("Not authorized")

    # reply-based unmute
    if msg.reply_to_message:
        user = msg.reply_to_message.from_user
        chat = msg.chat

        try:
            await bot.restrict_chat_member(
                chat.id,
                user.id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True
                ),
                until_date=None
            )
            reset_user(chat.id, user.id)
            return await msg.reply(f"Unmuted {user.id}")

        except:
            return await msg.reply("Failed to unmute")

    await msg.reply("Reply to the user's message with /unmute")


# -------------------- Start --------------------
async def main():
    logger.info("Bot running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())