# bot-service/bot.py
"""
Robust NSFW moderation Telegram bot (aiogram v3)
- Reliable file download via Telegram getFile endpoint
- Calls model at MODEL_API_URL (/score) with Authorization Bearer MODEL_SECRET
- If score >= NSFW_THRESHOLD -> delete message, mute user permanently, log offense
- Defensive error handling so single handler exceptions don't stop processing

Added:
- per-chat whitelist stored in sqlite
- /whitelist (toggle by reply or numeric id) — admin-only
- /whitelisted (list for this chat)
- /unmute supports reply (admin/owner) or /unmute <chat_id> <user_id> (owner-only)
"""
import os
import io
import logging
import asyncio
import sqlite3
import html
from typing import Optional

import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatPermissions
from aiogram import F
from aiogram.enums import ContentType
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

# ---------- config from env ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODEL_API_URL = os.getenv("MODEL_API_URL")  # e.g. https://surprising-communication-production.up.railway.app/score
MODEL_SECRET = os.getenv("MODEL_SECRET", "")
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.65"))
MUTE_DAYS = int(os.getenv("MUTE_DAYS", "9999"))
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0") or 0)

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
# Whitelist table: per-chat whitelist entries
_conn.execute(
    """
CREATE TABLE IF NOT EXISTS whitelist (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
)
"""
)
_conn.commit()


def add_offense(chat_id: int, user_id: int) -> int:
    try:
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
    except Exception:
        logger.exception("DB error in add_offense")
        return 0


def mark_muted(chat_id: int, user_id: int):
    try:
        cur = _conn.cursor()
        cur.execute("UPDATE offenders SET muted=1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        _conn.commit()
    except Exception:
        logger.exception("DB error in mark_muted")


def get_offenses(chat_id: int, user_id: int) -> int:
    try:
        cur = _conn.cursor()
        cur.execute("SELECT offenses FROM offenders WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = cur.fetchone()
        return row[0] if row else 0
    except Exception:
        logger.exception("DB error in get_offenses")
        return 0


# ---------- whitelist helpers ----------
def add_whitelist(chat_id: int, user_id: int):
    try:
        cur = _conn.cursor()
        cur.execute("INSERT OR IGNORE INTO whitelist (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
        _conn.commit()
    except Exception:
        logger.exception("DB error in add_whitelist")


def remove_whitelist(chat_id: int, user_id: int):
    try:
        cur = _conn.cursor()
        cur.execute("DELETE FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        _conn.commit()
    except Exception:
        logger.exception("DB error in remove_whitelist")


def is_whitelisted(chat_id: int, user_id: int) -> bool:
    try:
        cur = _conn.cursor()
        cur.execute("SELECT 1 FROM whitelist WHERE chat_id=? AND user_id=? LIMIT 1", (chat_id, user_id))
        return cur.fetchone() is not None
    except Exception:
        logger.exception("DB error in is_whitelisted")
        return False


def list_whitelisted(chat_id: int):
    try:
        cur = _conn.cursor()
        cur.execute("SELECT user_id FROM whitelist WHERE chat_id=? ORDER BY user_id", (chat_id,))
        return [row[0] for row in cur.fetchall()]
    except Exception:
        logger.exception("DB error in list_whitelisted")
        return []


# ---------- bot setup ----------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# large until_date to approximate permanent mute
PERMANENT_UNTIL = 2147483647


# ---------- helper: download file bytes via Telegram API ----------
async def download_file_bytes(file_id: str, timeout: float = 30.0) -> bytes:
    """
    Uses Telegram Bot API getFile -> download file via file path.
    Returns raw bytes of the file.
    """
    getfile_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(getfile_url, params={"file_id": file_id})
        resp.raise_for_status()
        j = resp.json()
    if not j.get("ok") or "result" not in j:
        raise RuntimeError(f"getFile failed: {j}")
    file_path = j["result"].get("file_path")
    if not file_path:
        raise RuntimeError("getFile returned empty file_path")

    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        file_resp = await client.get(file_url)
        file_resp.raise_for_status()
        return file_resp.content


# ---------- model call helper ----------
async def get_image_score(image_bytes: bytes, filename: str = "image.jpg", timeout: float = 30.0) -> float:
    headers = {"Authorization": f"Bearer {MODEL_SECRET}"} if MODEL_SECRET else {}
    # model expects field name "image" in current repo; adjust if needed
    files = {"image": (filename, image_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(MODEL_API_URL, headers=headers, files=files)
    resp.raise_for_status()
    data = resp.json()
    # support multiple keys
    if isinstance(data, dict):
        if "score" in data:
            return float(data.get("score", 0.0))
        if "prediction" in data:
            return float(data.get("prediction", 0.0))
    # fallback: try to parse first numeric value
    try:
        return float(data)
    except Exception:
        return 0.0


# ---------- helper: check admin ----------
async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER, ChatMemberStatus.CREATOR)
    except Exception:
        logger.exception("is_chat_admin check failed")
        return False


# ---------- image handler logic ----------
async def handle_media(message: types.Message):
    """Main logic for processing an incoming image/document (image)."""
    try:
        user = message.from_user
        chat = message.chat

        # If sender is whitelisted for this chat, skip
        if user and is_whitelisted(chat.id, user.id):
            logger.info("Skipping scan for whitelisted user %s in chat %s", user.id, chat.id)
            return

        # download image bytes
        image_bytes = None
        filename = "image.jpg"
        try:
            if message.photo:
                file_id = message.photo[-1].file_id
                image_bytes = await download_file_bytes(file_id)
                filename = "photo.jpg"
            elif message.document and (message.document.mime_type or "").startswith("image"):
                file_id = message.document.file_id
                image_bytes = await download_file_bytes(file_id)
                filename = message.document.file_name or "document.jpg"
            else:
                # not an image - nothing to do
                return
        except Exception:
            logger.exception("Failed to download file from Telegram")
            if OWNER_CHAT_ID:
                try:
                    await bot.send_message(OWNER_CHAT_ID, f"Failed to download file for chat {chat.id}; see logs.")
                except Exception:
                    pass
            return

        if not image_bytes:
            logger.warning("No image bytes found, skipping")
            return

        # Call model
        try:
            score = await get_image_score(image_bytes, filename=filename)
        except httpx.HTTPStatusError as e:
            logger.error("Model API returned status %s: %s", e.response.status_code, e.response.text)
            if OWNER_CHAT_ID:
                try:
                    await bot.send_message(OWNER_CHAT_ID, f"Model API error: {e.response.status_code} {e.response.text}")
                except Exception:
                    pass
            return
        except Exception:
            logger.exception("Failed to call model API")
            if OWNER_CHAT_ID:
                try:
                    await bot.send_message(OWNER_CHAT_ID, "Model API call failed; check logs.")
                except Exception:
                    pass
            return

        logger.info("Score for chat=%s user=%s msg=%s -> %.3f", chat.id, user.id, message.message_id, score)

        if score >= NSFW_THRESHOLD:
            # delete message
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
                        f"Muted user <a href='tg://user?id={user.id}'>{html.escape(str(user.id))}</a> in {html.escape(chat_title)}\nscore={score:.3f}\noffenses={offenses}",
                    )
                except Exception:
                    logger.exception("Failed to notify owner about mute")
        else:
            logger.debug("Image OK (score %.3f) for user %s in chat %s", score, user.id, chat.id)
    except Exception:
        # Catch-all to prevent one update from killing the worker
        logger.exception("Unexpected error in handle_media")


# ---------- handlers (defensive) ----------
@dp.message(F.content_type == ContentType.PHOTO)
async def photo_handler(message: types.Message):
    try:
        await handle_media(message)
    except Exception:
        logger.exception("photo_handler error")


@dp.message(F.content_type == ContentType.DOCUMENT)
async def document_handler(message: types.Message):
    try:
        if message.document and (message.document.mime_type or "").startswith("image"):
            await handle_media(message)
    except Exception:
        logger.exception("document_handler error")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    try:
        await message.reply("NSFW moderation bot active. I delete vulgar images and mute offenders.")
    except Exception:
        logger.exception("cmd_start reply failed")


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    try:
        if message.from_user and message.from_user.id == OWNER_CHAT_ID:
            await message.reply("Bot is running.")
        else:
            await message.reply("You are not authorized.")
    except Exception:
        logger.exception("cmd_status reply failed")


# ---------- whitelist commands ----------
@dp.message(Command("whitelist"))
async def cmd_whitelist(message: types.Message):
    """
    Toggle whitelist for a user.
    Usage:
      - Reply to user's message with /whitelist  -> toggles that user's whitelist state in this chat (admin-only)
      - /whitelist <user_id>                    -> toggles given user in this chat (admin-only)
    """
    try:
        chat_id = message.chat.id
        requester = message.from_user.id

        # Only admins can change whitelist
        if not await is_chat_admin(chat_id, requester):
            await message.reply("Only chat admins can change the whitelist.")
            return

        target_user_id: Optional[int] = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user_id = message.reply_to_message.from_user.id
        else:
            args = (message.get_command_arguments() or "").strip()
            if args:
                # accept numeric id (not @username resolution here to keep simple)
                try:
                    target_user_id = int(args.split()[0])
                except ValueError:
                    await message.reply("Please reply to a user or provide numeric user id to whitelist.", parse_mode="HTML")
                    return
            else:
                await message.reply("Usage: reply to a user with /whitelist or /whitelist <user_id>", parse_mode="HTML")
                return

        if is_whitelisted(chat_id, target_user_id):
            remove_whitelist(chat_id, target_user_id)
            await message.reply(f"User <a href='tg://user?id={target_user_id}'>{html.escape(str(target_user_id))}</a> removed from whitelist for this chat.", parse_mode="HTML")
        else:
            add_whitelist(chat_id, target_user_id)
            await message.reply(f"User <a href='tg://user?id={target_user_id}'>{html.escape(str(target_user_id))}</a> added to whitelist for this chat. Their images will not be scanned.", parse_mode="HTML")
    except Exception:
        logger.exception("cmd_whitelist failed")


@dp.message(Command("whitelisted"))
async def cmd_whitelisted(message: types.Message):
    """
    List whitelisted users for this chat.
    """
    try:
        chat_id = message.chat.id
        entries = list_whitelisted(chat_id)
        if not entries:
            await message.reply("No whitelisted users for this chat.")
            return
        lines = []
        for uid in entries:
            lines.append(f"<a href='tg://user?id={uid}'>{html.escape(str(uid))}</a>")
        await message.reply("Whitelisted users:\n" + "\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("cmd_whitelisted failed")


# ---------- unmute command (reply-friendly) ----------
@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    """
    Unmute a user.
    Usage:
      - Reply to a muted user's message and send /unmute  -> works for chat admins or owner
      - /unmute <chat_id> <user_id>                     -> owner-only (cross-chat)
    """
    try:
        requester = message.from_user
        # If reply -> allow chat admin or owner to unmute in that chat
        if message.reply_to_message and message.reply_to_message.from_user:
            chat_id = message.chat.id
            user_id = message.reply_to_message.from_user.id

            # allow owner or chat admin
            if requester.id != OWNER_CHAT_ID and not await is_chat_admin(chat_id, requester.id):
                await message.reply("Only chat admins (or owner) can unmute by reply.")
                return

            try:
                await bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True),
                    until_date=None,
                )
                cur = _conn.cursor()
                cur.execute("UPDATE offenders SET muted=0, offenses=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
                _conn.commit()
                await message.reply(f"User <a href='tg://user?id={user_id}'>{html.escape(str(user_id))}</a> unmuted in this chat.", parse_mode="HTML")
            except Exception:
                logger.exception("Failed to unmute user by reply")
                await message.reply("Failed to unmute (bot needs admin or user not found).")
            return

        # else check owner-only usage: /unmute <chat_id> <user_id>
        parts = (message.get_command_arguments() or "").split()
        if requester.id != OWNER_CHAT_ID:
            await message.reply("To unmute by IDs you must be the owner.")
            return
        if len(parts) < 2:
            await message.reply("Usage (owner): /unmute <chat_id> <user_id>")
            return
        try:
            chat_id = int(parts[0])
            user_id = int(parts[1])
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
            cur = _conn.cursor()
            cur.execute("UPDATE offenders SET muted=0, offenses=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            _conn.commit()
            await message.reply(f"User <a href='tg://user?id={user_id}'>{html.escape(str(user_id))}</a> unmuted in chat {html.escape(str(chat_id))}.", parse_mode="HTML")
        except Exception:
            logger.exception("Failed to unmute by id")
            await message.reply("Failed to unmute (bot needs admin or invalid IDs).")
    except Exception:
        logger.exception("cmd_unmute unexpected error")


# ---------- start polling ----------
async def main():
    try:
        logger.info("Starting bot polling...")
        await dp.start_polling(bot)
    except Exception:
        logger.exception("Dispatcher failed")
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())