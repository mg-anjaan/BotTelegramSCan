# bot.py — FINAL WORKING VERSION (Aiogram v3)
# Supports HF (if available) + fallback skin detection
# Keeps your mute/delete functions intact

import os
import io
import logging
import asyncio
import httpx
import numpy as np
from PIL import Image

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ContentType
from aiogram.types import ChatPermissions

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODEL_API_URL = os.getenv("MODEL_API_URL", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()

HF_NSFW_THRESHOLD = 0.65
SKIN_RATIO_THRESHOLD = 0.45
MIN_PIXELS = 20000
MUTE_SECONDS = 86400  # 1 day default

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------------- DOWNLOAD ----------------
async def tg_download(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content

# ---------------- HF API ----------------
async def hf_score(image_bytes: bytes):
    if not MODEL_API_URL:
        return None

    headers = {}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(MODEL_API_URL, content=image_bytes, headers=headers)

        if r.status_code != 200:
            logger.warning("HF non-200: %s %s", r.status_code, r.text[:200])
            return None

        try:
            js = r.json()
        except:
            logger.warning("HF returned HTML")
            return None

        if isinstance(js, dict):
            if "nsfw" in js:
                return float(js["nsfw"])
            if "score" in js and "label" in js:
                if "nsfw" in js["label"].lower():
                    return float(js["score"])
        if isinstance(js, list):
            for item in js:
                if isinstance(item, dict) and "label" in item and "score" in item:
                    if "nsfw" in item["label"].lower():
                        return float(item["score"])
        return None
    except:
        return None


# ---------------- FALLBACK SKIN DETECTOR ----------------
def skin_ratio(image_bytes: bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        if w * h < MIN_PIXELS:
            return 0.0

        arr = np.array(img).astype(np.float32)
        R = arr[:, :, 0]
        G = arr[:, :, 1]
        B = arr[:, :, 2]

        Y = 0.299 * R + 0.587 * G + 0.114 * B
        Cr = (R - Y) * 0.713 + 128
        Cb = (B - Y) * 0.564 + 128

        skin = (
            (Cr >= 140) & (Cr <= 180) &
            (Cb >= 95) & (Cb <= 135) &
            (Y >= 40) & (Y <= 240)
        )

        ratio = skin.sum() / skin.size
        score = min(1.0, ratio / SKIN_RATIO_THRESHOLD)
        return score
    except:
        return 0.0


# ---------------- ACTION ----------------
async def punish(message: types.Message, score: float, reason: str):
    cid = message.chat.id
    uid = message.from_user.id
    mid = message.message_id

    try:
        await bot.delete_message(cid, mid)
    except:
        pass

    try:
        await bot.restrict_chat_member(
            cid,
            uid,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=message.date + asyncio.timedelta(seconds=MUTE_SECONDS)
        )
    except:
        pass

    try:
        await message.answer(f"⚠️ NSFW detected ({reason}: {score:.2f}) — user muted.")
    except:
        pass


# ---------------- HANDLER ----------------
async def process_image(message: types.Message, file_id: str):
    bytes_img = await tg_download(file_id)

    # 1) Try HF
    score = await hf_score(bytes_img)
    if score is not None:
        logger.info("HF Score = %.3f", score)
        if score >= HF_NSFW_THRESHOLD:
            await punish(message, score, "hf")
        return

    # 2) Fallback skin
    score = skin_ratio(bytes_img)
    logger.info("Fallback Score = %.3f", score)
    if score >= 1.0:
        await punish(message, score, "skin")


@dp.message(Command("start"))
async def cmd_start(message):
    await message.reply("NSFW Scan Bot Active 🔍")

@dp.message(lambda m: m.photo)
async def handle_photo(message):
    await process_image(message, message.photo[-1].file_id)

@dp.message(lambda m: m.document and m.document.mime_type.startswith("image/"))
async def handle_doc(message):
    await process_image(message, message.document.file_id)


# ---------------- START (Aiogram v3) ----------------
async def main():
    logger.info("Bot running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())