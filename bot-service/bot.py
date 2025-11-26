import os
import io
import logging
import asyncio
import sqlite3
import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ContentType
from aiogram.types import ChatPermissions
from aiogram.filters import Command
from aiogram import F

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

# ---------------- ENV VARS ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.70"))
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0") or 0)

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN not set")
if not HF_TOKEN:
    raise SystemExit("❌ HF_TOKEN not set (required for HuggingFace API)")

# HuggingFace NSFW model URL (no variable needed)
HF_MODEL_URL = "https://api-inference.huggingface.co/models/Falconsai/nsfw-detector"

# ---------------- SQLITE DB ----------------
DB_PATH = "/data/bot_db.sqlite3"
os.makedirs("/data", exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS offenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        cur.execute("INSERT INTO offenders (chat_id,user_id,offenses) VALUES (?,?,?)", (chat_id, user_id, offenses))
    conn.commit()
    return offenses

def reset_offense(chat_id, user_id):
    cur = conn.cursor()
    cur.execute("UPDATE offenders SET offenses=0, muted=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()

# ---------------- BOT SETUP ----------------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
PERMA_MUTE = 2147483647

# ---------------- DOWNLOAD IMAGE ----------------
async def tg_download(file_id):
    try:
        get_file = await bot.get_file(file_id)
        file_path = get_file.file_path

        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.error("Failed to download image: %s", e)
        return None

# ---------------- HUGGINGFACE SCORE ----------------
async def hf_score(image_bytes):
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}

    files = {"inputs": image_bytes}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(HF_MODEL_URL, headers=headers, files=files)
    except Exception:
        return 0.0

    try:
        data = r.json()
    except:
        logger.error("HF returned HTML or invalid JSON")
        return 0.0

    if isinstance(data, list) and len(data) > 0 and "score" in data[0]:
        return float(data[0]["score"])

    return 0.0

# ---------------- MEDIA HANDLER ----------------
async def handle_media(message: types.Message):
    chat = message.chat
    user = message.from_user

    # download bytes
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and str(message.document.mime_type).startswith("image"):
        file_id = message.document.file_id
    else:
        return

    img = await tg_download(file_id)
    if not img:
        return

    score = await hf_score(img)
    logger.info(f"Score={score:.3f} user={user.id} chat={chat.id}")

    if score >= NSFW_THRESHOLD:
        # delete
        try:
            await bot.delete_message(chat.id, message.message_id)
        except:
            pass

        offenses = add_offense(chat.id, user.id)

        # mute permanently
        try:
            await bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=PERMA_MUTE
            )
        except:
            pass

        if OWNER_CHAT_ID:
            try:
                await bot.send_message(
                    OWNER_CHAT_ID,
                    f"🚫 <b>NSFW Detected</b>\nUser: <a href='tg://user?id={user.id}'>{user.id}</a>\nChat: {chat.id}\nScore: {score:.3f}\nOffenses: {offenses}"
                )
            except:
                pass

# ---------------- HANDLERS ----------------
@dp.message(F.content_type == ContentType.PHOTO)
async def photo_handler(message: types.Message):
    await handle_media(message)

@dp.message(F.content_type == ContentType.DOCUMENT)
async def doc_handler(message: types.Message):
    await handle_media(message)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply("🤖 NSFW protection bot active.")

@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    if message.from_user.id != OWNER_CHAT_ID:
        return await message.reply("Not allowed")

    # If reply → unmute replied user
    if message.reply_to_message:
        user = message.reply_to_message.from_user
        try:
            await bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=True),
                until_date=None
            )
            reset_offense(message.chat.id, user.id)
            return await message.reply(f"Unmuted {user.id}")
        except:
            return await message.reply("Failed")

    await message.reply("Reply to user to unmute")

# ---------------- MAIN ----------------
async def main():
    logger.info("Bot running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())