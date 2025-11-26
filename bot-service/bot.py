# bot-service/bot.py
"""
Telegram moderation bot (aiogram v3)
- Downloads incoming images (photo/document)
- Sends image bytes to MODEL_API_URL (Authorization: Bearer <MODEL_SECRET>)
- If score >= NSFW_THRESHOLD -> delete message, mute user permanently, log offense
- Notifies OWNER_CHAT_ID about actions and model errors
"""
import os
import io
import logging
import asyncio
import sqlite3
from typing import Optional

import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatPermissions
from aiogram import F
from aiogram.enums import ContentType
from aiogram.filters import Command  # correct filter for commands in aiogram v3

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

# ---------- config from env ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODEL_API_URL = os.getenv("MODEL_API_URL")  # e.g. https://your-model.up.railway.app/score
MODEL_SECRET = os.getenv("MODEL_SECRET", "")
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.65"))
MUTE_DAYS = int(os.getenv("MUTE_DAYS", "9999"))
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0") or 0)
PORT = int(os.getenv("PORT", "8000"))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set. Exiting.")
    raise SystemExit("BOT_TOKEN env var required")
if not MODEL_API_URL:
    logger.error("MODEL_API_URL is not set. Exiting.")
    raise SystemExit("MODEL_API_URL env var required")
if not MODEL_SECRET:
    logger.warning("MODEL_SECRET is empty. Model requests may be unauthorized.")

# ---------- sqlite (simple file inside /data or /app) ----------
DB_PATH = os.getenv("BOT_DB_PATH", "/data/bot_state.sqlite3")
# ensure directory exists
try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except Exception:
    pass

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute(
    """
CREATE TABLE IF NOT EXISTS offenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    offenses INTEGER NOT NULL DEFAULT 1,
    muted INTEGER NOT NULL DEFAULT 0,
    last_offense_ts INTEGER DEFAULT (strftime('%s','now'))
)
"""
)
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


# ---------- bot setup ----------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# permanent until (year 2038 safe int)
PERMANENT_UNTIL = 2147483647


# ---------- model call helper ----------
async def get_image_score(image_bytes: bytes, filename: str = "image.jpg", timeout: float = 30.0) -> float:
    headers = {"Authorization": f"Bearer {MODEL_SECRET}"} if MODEL_SECRET else {}
    files = {"image": (filename, image_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(MODEL_API_URL, headers=headers, files=files)
    resp.raise_for_status()
    data = resp.json()
    return float(data.get("score", 0.0))


# ---------- image handler logic ----------
async def handle_media(message: types.Message):
    user = message.from_user
    chat = message.chat

    # download image bytes from photo or document
    image_bytes = None
    filename = "image.jpg"
    try:
        if message.photo:
            buf = io.BytesIO()
            await message.photo[-1].download(destination=buf)
            buf.seek(0)
            image_bytes = buf.read()
            filename = "photo.jpg"
        elif message.document and (message.document.mime_type or "").startswith("image"):
            buf = io.BytesIO()
            await message.document.download(destination=buf)
            buf.seek(0)
            image_bytes = buf.read()
            filename = message.document.file_name or "document.jpg"
        else:
            # not an image, ignore
            return
    except Exception:
        logger.exception("Failed to download file from Telegram")
        return

    if not image_bytes:
        logger.warning("No image bytes found, skipping")
        return

    # call model API
    try:
        score = await get_image_score(image_bytes, filename=filename)
    except httpx.HTTPStatusError as e:
        logger.error("Model API returned status %s: %s", e.response.status_code, e.response.text)
        # notify owner about model error
        if OWNER_CHAT_ID:
            try:
                await bot.send_message(OWNER_CHAT_ID, f"Model API error: {e.response.status_code} {e.response.text}")
            except Exception:
                pass
        return
    except Exception as e:
        logger.exception("Failed to call model API")
        if OWNER_CHAT_ID:
            try:
                await bot.send_message(OWNER_CHAT_ID, f"Model API call failed: {e}")
            except Exception:
                pass
        return

    logger.info("Score for chat=%s user=%s msg=%s -> %.3f", chat.id, user.id, message.message_id, score)

    if score >= NSFW_THRESHOLD:
        # delete the message
        try:
            await bot.delete_message(chat.id, message.message_id)
        except Exception:
            logger.exception("Failed to delete message (permission?)")

        # increment offenses
        offenses = add_offense(chat.id, user.id)

        # attempt to mute permanently
        try:
            await bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                ),
                until_date=PERMANENT_UNTIL,
            )
            mark_muted(chat.id, user.id)
        except Exception:
            logger.exception("Failed to restrict/mute user (bot may need admin)")
            if OWNER_CHAT_ID:
                try:
                    await bot.send_message(OWNER_CHAT_ID, f"Need admin to mute user {user.id} in chat {chat.id}.")
                except Exception:
                    pass

        # notify owner
        if OWNER_CHAT_ID:
            try:
                chat_title = chat.title or str(chat.id)
                await bot.send_message(
                    OWNER_CHAT_ID,
                    f"Muted user <a href='tg://user?id={user.id}'>{user.id}</a> in {chat_title}\nscore={score:.3f}\noffenses={offenses}",
                )
            except Exception:
                pass
    else:
        # optional: you may log or do nothing for safe images
        logger.debug("Image OK (score %.3f) for user %s in chat %s", score, user.id, chat.id)


# ---------- handlers ----------
@dp.message(F.content_type == ContentType.PHOTO)
async def photo_handler(message: types.Message):
    await handle_media(message)


@dp.message(F.content_type == ContentType.DOCUMENT)
async def document_handler(message: types.Message):
    if message.document and (message.document.mime_type or "").startswith("image"):
        await handle_media(message)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply("NSFW moderation bot active. I delete vulgar images and mute offenders.")


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    # owner-only
    if message.from_user and message.from_user.id == OWNER_CHAT_ID:
        await message.reply("Bot is running.")
    else:
        await message.reply("You are not authorized.")


@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    # owner-only: /unmute <chat_id> <user_id>
    if message.from_user and message.from_user.id != OWNER_CHAT_ID:
        await message.reply("Only owner can use this command.")
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("Usage: /unmute <chat_id> <user_id>")
        return
    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        await message.reply("Chat ID and User ID must be integers.")
        return
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True),
            until_date=None,
        )
        # reset DB mute flag
        cur = _conn.cursor()
        cur.execute("UPDATE offenders SET muted=0, offenses=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        _conn.commit()
        await message.reply(f"User {user_id} unmuted in chat {chat_id}.")
    except Exception:
        logger.exception("Failed to unmute user")
        await message.reply("Failed to unmute (bot needs admin or invalid IDs).")


# ---------- start polling ----------
async def main():
    try:
        logger.info("Starting bot polling...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())