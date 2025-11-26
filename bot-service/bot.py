# bot-service/bot.py
"""
NSFW moderation Telegram bot (aiogram v3)
- Ensemble detection: external model score + local skin-tone heuristic
- Fusion: treat image as NSFW if model_score >= NSFW_THRESHOLD OR skin_fraction >= SKIN_THRESHOLD
- Multi-crop skin checks (center + various scales) to reduce false positives
"""
import os
import io
import logging
import asyncio
import sqlite3
from typing import Optional, Tuple, List

import httpx
from PIL import Image, ImageOps
from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatPermissions
from aiogram import F
from aiogram.enums import ContentType
from aiogram.filters import Command

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

# ---------- config from env (tune these) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODEL_API_URL = os.getenv("MODEL_API_URL")  # e.g. https://.../score
MODEL_SECRET = os.getenv("MODEL_SECRET", "")
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.65"))  # model threshold
# Skin heuristic params
USE_SKIN_HEURISTIC = os.getenv("USE_SKIN_HEURISTIC", "1") not in ("0", "false", "False")
SKIN_THRESHOLD = float(os.getenv("SKIN_THRESHOLD", "0.28"))  # fraction (0-1), tune down/up
SKIN_CENTER_ONLY = os.getenv("SKIN_CENTER_ONLY", "0") not in ("0", "false", "False")
SKIN_CHECK_CROPS = int(os.getenv("SKIN_CHECK_CROPS", "3"))  # number of crops to test (center + scaled)
SKIN_MIN_AREA = float(os.getenv("SKIN_MIN_AREA", "0.05"))  # ignore tiny images (fraction of total)
# Fusion style: "or" (default) or "weighted"
FUSION_MODE = os.getenv("FUSION_MODE", "or")  # "or" or "weighted"
SKIN_WEIGHT = float(os.getenv("SKIN_WEIGHT", "0.6"))  # used only if weighted: final = model*0.7 + skin*skin_weight

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

# ---------- sqlite setup (unchanged) ----------
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
_conn.execute(
    """
CREATE TABLE IF NOT EXISTS whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_ts INTEGER DEFAULT (strftime('%s','now')),
    UNIQUE(chat_id, user_id)
)
"""
)
_conn.commit()


# ---------- DB helpers (unchanged) ----------
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


def add_whitelist(chat_id: int, user_id: int) -> bool:
    try:
        cur = _conn.cursor()
        cur.execute("INSERT OR IGNORE INTO whitelist (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
        _conn.commit()
        return cur.rowcount > 0
    except Exception:
        logger.exception("DB error in add_whitelist")
        return False


def remove_whitelist(chat_id: int, user_id: int) -> bool:
    try:
        cur = _conn.cursor()
        cur.execute("DELETE FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        _conn.commit()
        return cur.rowcount > 0
    except Exception:
        logger.exception("DB error in remove_whitelist")
        return False


def is_whitelisted(chat_id: int, user_id: int) -> bool:
    try:
        cur = _conn.cursor()
        cur.execute("SELECT 1 FROM whitelist WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        return cur.fetchone() is not None
    except Exception:
        logger.exception("DB error in is_whitelisted")
        return False


# ---------- bot setup ----------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
PERMANENT_UNTIL = 2147483647


# ---------- helper: download file bytes via Telegram API ----------
async def download_file_bytes(file_id: str, timeout: float = 30.0) -> bytes:
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


# ---------- model call helper (unchanged) ----------
async def call_model_api(image_bytes: bytes, filename: str = "image.jpg", timeout: float = 30.0) -> float:
    headers = {"Authorization": f"Bearer {MODEL_SECRET}"} if MODEL_SECRET else {}
    files = {"image": (filename, image_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(MODEL_API_URL, headers=headers, files=files)
    resp.raise_for_status()
    data = resp.json()
    return float(data.get("score", 0.0))


# ---------- skin-tone heuristic ----------
def pil_open_image(image_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def center_crop(img: Image.Image, frac: float = 0.8) -> Image.Image:
    w, h = img.size
    cw, ch = int(w * frac), int(h * frac)
    left = (w - cw) // 2
    top = (h - ch) // 2
    return img.crop((left, top, left + cw, top + ch))


def compute_skin_fraction(img: Image.Image) -> float:
    """
    Simple skin-tone pixel fraction heuristic.
    Works in RGB -> convert to HSV-like test using RGB rules.
    Returns fraction of pixels detected as 'skin' relative to image area.
    Heuristic tuned to reduce false positives; adjust SKIN_THRESHOLD if needed.
    """
    # downscale for speed
    max_side = 400
    w, h = img.size
    if max(w, h) > max_side:
        img = ImageOps.fit(img, (max_side, int(max_side * (h / w) if w > h else max_side * (w / h))))
    pixels = img.getdata()
    total = 0
    skin = 0
    # iterate
    for r, g, b in pixels:
        total += 1
        # RGB-based skin color tests (combination from common heuristics)
        # Condition 1: r > 95, g > 40, b > 20 and r>g and r>b and (r-g)>15
        # Condition 2: r>220,g>210,b>170 (very bright skin)
        if (r > 95 and g > 40 and b > 20 and r > g and r > b and (r - g) > 15) or (r > 220 and g > 210 and b > 170):
            # additional narrow check to reduce false positives: exclude very red objects
            if not (r > 200 and g < 50 and b < 50):
                skin += 1
    return float(skin) / float(total) if total else 0.0


def multi_crop_skin_score(image_bytes: bytes, crops: int = 3) -> float:
    """
    Returns maximum skin fraction across crops.
    crops=1 -> full image only
    crops=3 -> full, center 0.8, center 0.5
    """
    try:
        img = pil_open_image(image_bytes)
    except Exception:
        logger.exception("Failed to open image for skin heuristic")
        return 0.0
    scores: List[float] = []
    # full image
    scores.append(compute_skin_fraction(img))
    if crops >= 2:
        scores.append(compute_skin_fraction(center_crop(img, 0.8)))
    if crops >= 3:
        scores.append(compute_skin_fraction(center_crop(img, 0.5)))
    # you can add asymmetric crops if needed
    return max(scores)


# ---------- decision fusion ----------
def decide_nsfw(model_score: float, skin_frac: float) -> Tuple[bool, float]:
    """
    Return (is_nsfw, final_score) where final_score is interpretive value.
    Logic:
      - If FUSION_MODE == "or": nsfw if model_score >= NSFW_THRESHOLD OR skin_frac >= SKIN_THRESHOLD
      - If FUSION_MODE == "weighted": final = model_score * (1 - SKIN_WEIGHT) + skin_frac * SKIN_WEIGHT ; compare to NSFW_THRESHOLD
    """
    if FUSION_MODE == "weighted":
        final = model_score * (1.0 - SKIN_WEIGHT) + skin_frac * SKIN_WEIGHT
        return (final >= NSFW_THRESHOLD, final)
    else:
        # "or" mode: build a combined interpretive score: max(model_score, skin_frac)
        final = max(model_score, skin_frac)
        return ((model_score >= NSFW_THRESHOLD) or (skin_frac >= SKIN_THRESHOLD), final)


# ---------- image handler logic (integrates heuristic) ----------
async def handle_media(message: types.Message):
    try:
        user = message.from_user
        chat = message.chat
        if not user:
            return
        if is_whitelisted(chat.id, user.id):
            logger.debug("User %s is whitelisted in chat %s — skipping scan", user.id, chat.id)
            return

        # download bytes
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

        # 1) quick local skin heuristic (optional)
        skin_frac = 0.0
        if USE_SKIN_HEURISTIC:
            try:
                crops = 1 if SKIN_CENTER_ONLY else max(1, SKIN_CHECK_CROPS)
                skin_frac = multi_crop_skin_score(image_bytes, crops=crops)
                logger.info("Skin fraction (max-crop)=%.3f for chat=%s user=%s", skin_frac, chat.id, user.id)
            except Exception:
                logger.exception("Skin heuristic failed, continuing with model only")

        # 2) call external model
        model_score = 0.0
        try:
            model_score = await call_model_api(image_bytes, filename=filename)
        except httpx.HTTPStatusError as e:
            logger.error("Model API returned status %s: %s", e.response.status_code, e.response.text)
            if OWNER_CHAT_ID:
                try:
                    await bot.send_message(OWNER_CHAT_ID, f"Model API error: {e.response.status_code} {e.response.text}")
                except Exception:
                    pass
            # if model unavailable, we can still use skin heuristic to decide (dangerous), but proceed to decision below
        except Exception:
            logger.exception("Model API call failed")

        is_nsfw, final_score = decide_nsfw(model_score, skin_frac)
        logger.info("Scores chat=%s user=%s msg=%s model=%.3f skin=%.3f final=%.3f nsfw=%s",
                    chat.id, user.id, message.message_id, model_score, skin_frac, final_score, is_nsfw)

        if is_nsfw:
            # delete message
            try:
                await bot.delete_message(chat.id, message.message_id)
            except Exception:
                logger.exception("Failed to delete message (permission?)")

            offenses = add_offense(chat.id, user.id)

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

            if OWNER_CHAT_ID:
                try:
                    chat_title = chat.title or str(chat.id)
                    await bot.send_message(
                        OWNER_CHAT_ID,
                        f"Muted user <a href='tg://user?id={user.id}'>{user.id}</a> in {chat_title}\nmodel={model_score:.3f}\nskin={skin_frac:.3f}\nfinal={final_score:.3f}\noffenses={offenses}",
                    )
                except Exception:
                    logger.exception("Failed to notify owner about mute")
        else:
            logger.debug("Image OK (final=%.3f) for user %s in chat %s", final_score, user.id, chat.id)
    except Exception:
        logger.exception("Unexpected error in handle_media")


# ---------- handlers (unchanged) ----------
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


# whitelist/unmute commands omitted here for brevity - keep your existing ones
# If you replaced whole file earlier, re-add your whitelist/unmute handlers below
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