# bot-service/bot.py
import os
import logging
import asyncio
import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatPermissions
from aiogram.filters import MessageType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot-service")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MODEL_API_URL = os.environ.get("MODEL_API_URL")  # full: https://<model-host>/score
MODEL_SECRET = os.environ.get("MODEL_SECRET", "mgPROTECT12345")
NSFW_THRESHOLD = float(os.environ.get("NSFW_THRESHOLD", "0.65"))
MUTE_DAYS = int(os.environ.get("MUTE_DAYS", "999"))
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0"))

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set in environment. Exiting.")
    raise SystemExit(1)
if not MODEL_API_URL:
    logger.error("MODEL_API_URL not set in environment. Exiting.")
    raise SystemExit(1)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

async def send_image_to_model_and_get_score(image_bytes: bytes) -> float:
    headers = {"Authorization": f"Bearer {MODEL_SECRET}"}
    files = {"image": ("image.jpg", image_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(MODEL_API_URL, headers=headers, files=files)
    if resp.status_code != 200:
        logger.error("Model API returned %s: %s", resp.status_code, resp.text)
        raise RuntimeError(f"Model API returned {resp.status_code}")
    data = resp.json()
    return float(data.get("score", 0.0))

@dp.message(MessageType.PHOTO)
async def handle_photo(message: types.Message):
    try:
        # get highest quality photo
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        b = await bot.download_file(file.file_path)
        image_bytes = b.read()
    except Exception as e:
        logger.exception("Failed to download photo")
        return

    try:
        score = await send_image_to_model_and_get_score(image_bytes)
    except Exception as e:
        logger.exception("Error calling model API: %s", e)
        try:
            await bot.send_message(OWNER_CHAT_ID, f"Model API error: {e}")
        except Exception:
            pass
        return

    logger.info("NSFW score for message %s by %s = %.3f", message.message_id, message.from_user.id, score)
    if score >= NSFW_THRESHOLD:
        # delete message
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            logger.exception("Failed to delete message")

        # restrict user (mute) — permanent if platform allows None, else set far future
        try:
            until_date = None  # aiogram / Bot API may accept None for permanent
            await bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                permissions=ChatPermissions(can_send_messages=False,
                                            can_send_media_messages=False,
                                            can_send_other_messages=False,
                                            can_add_web_page_previews=False),
                until_date=until_date
            )
        except Exception:
            logger.exception("Failed to restrict (mute) user")

        # notify owner
        try:
            await bot.send_message(OWNER_CHAT_ID,
                                   f"Deleted NSFW image from {message.from_user.id} in {message.chat.title or message.chat.id}. Score: {score:.3f}")
        except Exception:
            pass

async def main():
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())