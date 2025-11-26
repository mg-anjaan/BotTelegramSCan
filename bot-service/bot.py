# bot-service/bot.py
import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatPermissions
from aiogram.filters import Command
from aiogram.enums import ContentType
from aiogram.utils import exceptions
from db import add_offense, mark_muted, get_offenses
from utils import get_image_score

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bridge-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.75"))
MUTE_DAYS = int(os.getenv("MUTE_DAYS", "0"))  # 0 -> permanent
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# Helper: mute until far future (year 2038 max for int32)
PERMANENT_UNTIL = 2147483647

async def handle_media(message: types.Message):
    # Accept photos and documents that are images
    user = message.from_user
    chat = message.chat

    # fetch image bytes depending on type
    file_bytes = None
    filename = "image.jpg"
    try:
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
            file_bytes = await bot.download_file(file.file_path)
            filename = "photo.jpg"
        elif message.document and (message.document.mime_type or "").startswith("image"):
            file = await bot.get_file(message.document.file_id)
            file_bytes = await bot.download_file(file.file_path)
            filename = message.document.file_name or "doc.jpg"
        else:
            return  # not an image
    except Exception:
        logger.exception("Failed to download file from Telegram")
        return

    # convert to bytes
    if hasattr(file_bytes, "read"):
        img_bytes = file_bytes.read()
    elif isinstance(file_bytes, (bytes, bytearray)):
        img_bytes = bytes(file_bytes)
    else:
        # fallback: attempt to call .content
        img_bytes = getattr(file_bytes, "content", b"")

    if not img_bytes:
        logger.warning("No bytes for image; skipping.")
        return

    # Call model-service
    try:
        score = await get_image_score(img_bytes, filename=filename)
    except Exception as e:
        logger.exception("Model API call failed")
        return

    logger.info("Image score for %s/%s: %s", chat.id, user.id, score)

    if score >= NSFW_THRESHOLD:
        # Delete message
        try:
            await bot.delete_message(chat.id, message.message_id)
        except exceptions.MessageCantBeDeleted:
            logger.warning("No permission to delete message in chat %s", chat.id)
        except Exception:
            logger.exception("Failed deleting message")

        # increment offense and mute user permanently
        offenses = add_offense(chat.id, user.id)
        try:
            # restrict permissions: no send messages, no media
            await bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False, can_send_media_messages=False, can_send_other_messages=False, can_add_web_page_previews=False),
                until_date=PERMANENT_UNTIL
            )
            mark_muted(chat.id, user.id)
        except exceptions.ChatAdminRequired:
            logger.warning("Bot needs admin privileges to mute in chat %s", chat.id)
            # notify owner
            if OWNER_CHAT_ID:
                await bot.send_message(OWNER_CHAT_ID, f"Need admin to mute user {user.id} in chat {chat.id}.")
        except Exception:
            logger.exception("Failed to restrict user")

        # notify owner optionally
        if OWNER_CHAT_ID:
            try:
                await bot.send_message(OWNER_CHAT_ID, f"User <a href='tg://user?id={user.id}'>{user.id}</a> was muted in chat {chat.id} for NSFW image (score={score:.2f}). Offenses={offenses}")
            except Exception:
                logger.exception("Failed to notify owner")

@dp.message(Command(commands=["start"]))
async def start_cmd(message: types.Message):
    await message.reply("NSFW moderation bot active. I delete vulgar images and mute offenders.")

# Register handlers for photos and documents
@dp.message(F.content_type == ContentType.PHOTO)
async def photo_handler(message: types.Message):
    await handle_media(message)

@dp.message(F.content_type == ContentType.DOCUMENT)
async def document_handler(message: types.Message):
    # only image documents
    if message.document and (message.document.mime_type or "").startswith("image"):
        await handle_media(message)

# Include admin router
from admin_handlers import router as admin_router
dp.include_router(admin_router)

async def main():
    try:
        logger.info("Starting bot polling...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())