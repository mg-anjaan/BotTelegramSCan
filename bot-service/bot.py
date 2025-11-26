#!/usr/bin/env python3
"""
bot.py
Telegram bot (python-telegram-bot v20+) that:
- watches group messages for photos / image files / videos
- forwards the file to external model-service (/score) with Bearer MODEL_SECRET
- if returned score >= threshold: deletes the message, mutes the sender (long mute), notifies admins/owner
Environment variables:
- BOT_TOKEN
- MODEL_API_URL   e.g. https://ramscan-production.up.railway.app
- MODEL_SECRET    e.g. mgPROTECT12345
- NSFW_THRESHOLD  (optional) default=0.7
- OWNER_CHAT_ID   (optional)
- MUTE_DAYS       (optional) default=36500 (~100 yrs)
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nsfw-bot")

# -------- env / defaults ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODEL_API_URL = os.getenv("MODEL_API_URL")
MODEL_SECRET = os.getenv("MODEL_SECRET")
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.7"))
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
MUTE_DAYS = int(os.getenv("MUTE_DAYS", "36500"))

if not BOT_TOKEN or not MODEL_API_URL or not MODEL_SECRET:
    logger.critical("Missing required env vars (BOT_TOKEN, MODEL_API_URL, MODEL_SECRET).")
    raise SystemExit


# ---------- helper: model scoring ----------
async def score_image(image_bytes: bytes) -> Optional[float]:
    url = MODEL_API_URL.rstrip("/") + "/score"
    headers = {"Authorization": f"Bearer {MODEL_SECRET}"}
    files = {"image": ("image.jpg", image_bytes, "image/jpeg")}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, files=files)
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("score"))
    except Exception as e:
        logger.exception("Model service error: %s", e)
        return None


# ---------- mute & notify ----------
async def mute_member_and_notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, reason: str):
    until = datetime.utcnow() + timedelta(days=MUTE_DAYS)
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )

    # mute user
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
            until_date=until,
        )
    except Exception:
        logger.exception("Failed to mute %s", user_id)

    # notify
    note = (
        f"🚫 Auto-Mute Executed\n"
        f"User: {user_id}\n"
        f"Chat: {chat_id}\n"
        f"Reason: {reason}"
    )

    if OWNER_CHAT_ID:
        try:
            await context.bot.send_message(int(OWNER_CHAT_ID), note)
        except:
            pass
    else:
        # notify group admins
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            for a in admins:
                try:
                    await context.bot.send_message(a.user.id, note)
                except:
                    pass
        except:
            pass


# ---------- main handler ----------
async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        return

    # get image bytes
    file_bytes = None

    try:
        if message.photo:
            file = await message.photo[-1].get_file()
            file_bytes = await file.download_as_bytearray()

        elif message.document and message.document.mime_type.startswith("image"):
            file = await message.document.get_file()
            file_bytes = await file.download_as_bytearray()

        elif message.video:
            # video thumbnail
            if message.video.thumb:
                file = await message.video.thumb.get_file()
                file_bytes = await file.download_as_bytearray()
    except:
        logger.exception("Failed to download media")
        return

    if not file_bytes:
        return

    # score
    score = await score_image(bytes(file_bytes))
    if score is None:
        return

    if score >= NSFW_THRESHOLD:
        # delete message
        try:
            await message.delete()
        except:
            logger.exception("Failed deleting message")

        # mute user
        await mute_member_and_notify(
            context,
            chat.id,
            user.id,
            f"NSFW score {score:.2f} >= {NSFW_THRESHOLD}",
        )

        # notify group
        try:
            await context.bot.send_message(
                chat.id,
                "⚠️ Explicit content removed. User has been muted permanently.",
            )
        except:
            pass


# ---------- start bot ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    media_filter = (filters.PHOTO | filters.Document.IMAGE | filters.VIDEO)
    app.add_handler(MessageHandler(media_filter, media_handler))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()