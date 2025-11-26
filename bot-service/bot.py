# bot.py - main aiogram bot
import os
import asyncio
import httpx
import tempfile
import shutil
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import AsyncSessionLocal, Offense, Cache, Whitelist, create_tables
from utils import save_temp_file, md5_bytes
from admin_handlers import router as admin_router

BOT_TOKEN = os.getenv("BOT_TOKEN")
NSFW_SERVICE_URL = os.getenv("NSFW_SERVICE_URL")
MODEL_API_KEY = os.getenv("MODEL_API_KEY")
OFFENSE_LIMIT = int(os.getenv("OFFENSE_LIMIT", "2"))
MUTE_DURATION = int(os.getenv("MUTE_DURATION_SECONDS", "0"))
NSFW_DELETE_THRESHOLD = float(os.getenv("NSFW_DELETE_THRESHOLD", "0.8"))
NSFW_REVIEW_MIN = float(os.getenv("NSFW_REVIEW_MIN", "0.4"))
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID") or os.getenv("OWNER_CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_router(admin_router)

async def get_score(file_path: str):
    headers = {"Authorization": f"Bearer {MODEL_API_KEY}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        with open(file_path, "rb") as fh:
            files = {"image": ("file", fh, "image/jpeg")}
            r = await client.post(NSFW_SERVICE_URL, files=files, headers=headers)
            r.raise_for_status()
            data = r.json()
            return float(data.get("score", 0.0))

@dp.message(types.ContentTypeFilter(types.ContentType.PHOTO))
async def handle_photo(message: types.Message):
    # only groups/supergroups
    if message.chat.type not in ("group","supergroup"):
        return
    if message.from_user.is_bot:
        return

    # download best quality photo
    photo = message.photo[-1]
    b = await photo.download(destination=bytes)
    key = md5_bytes(b)

    # TODO: check whitelist here (DB)
    # save temp file
    path = await save_temp_file(b, suffix=".jpg")
    try:
        score = await get_score(path)
    except Exception as e:
        await message.reply("Error checking image: " + str(e))
        try:
            os.remove(path)
        except:
            pass
        return
    try:
        os.remove(path)
    except:
        pass

    if score >= NSFW_DELETE_THRESHOLD:
        try:
            await message.delete()
        except Exception:
            pass
        await bot.send_message(message.chat.id, f"⚠️ A message was removed for violating the rules. Offender: {message.from_user.mention}")
        # store offense
        async with AsyncSessionLocal() as session:
            off = Offense(chat_id=str(message.chat.id), user_id=str(message.from_user.id), msg_id=str(message.message_id), score=score, action='deleted')
            session.add(off)
            await session.commit()
        # notify admins
        if ADMIN_CHAT_ID:
            await bot.send_message(ADMIN_CHAT_ID, f"User {message.from_user.id} auto-removed for NSFW (score={score:.2f}) in {message.chat.title}")
    elif score >= NSFW_REVIEW_MIN:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Approve", callback_data=f"approve:{key}"), 
            InlineKeyboardButton(text="Delete & Ban", callback_data=f"delete:{key}:{message.from_user.id}")
        ]])
        # forward small info to admin channel
        if ADMIN_CHAT_ID:
            await bot.send_message(ADMIN_CHAT_ID, f"⚠️ Suspected NSFW (score={score:.2f}) from {message.from_user.mention} in {message.chat.title}", reply_markup=keyboard)
    else:
        return

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply("NSFW Moderator bot is running.")

if __name__ == "__main__":
    import asyncio
    async def main():
        await create_tables()
        from aiogram import executor
        executor.start_polling(dp, skip_updates=True)
    asyncio.run(main())
