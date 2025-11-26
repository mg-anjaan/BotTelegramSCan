# bot.py
# Drop-in replacement for your Telegram scanner bot.
# Requirements: aiogram, httpx, Pillow, numpy
# Provide BOT_TOKEN env var. Optional: MODEL_API_URL (HuggingFace inference url) and HF_TOKEN.

import os
import io
import logging
import asyncio
from datetime import timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatPermissions
from aiogram.utils import executor

import httpx
from PIL import Image
import numpy as np

# ----------------- Config (no need to edit) -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN env var")

# Optional HF model endpoint (example: https://api-inference.huggingface.co/models/xxx/model)
MODEL_API_URL = os.getenv("MODEL_API_URL", "").strip()
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()  # optional

# Thresholds
HF_NSFW_SCORE_THRESHOLD = 0.65   # if HF says nsfw probability >= this -> take action
SKIN_RATIO_THRESHOLD = 0.45      # fallback heuristic: ratio of skin pixels in image
MIN_PIXELS_TO_CHECK = 20000      # don't attempt to score tiny images
MUTE_SECONDS = int(os.getenv("MUTE_SECONDS", "86400"))  # default 24 hours

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-moderator")

# AIogram setup
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ----------------- Helpers -----------------
async def download_file_bytes(file: types.File) -> bytes:
    """Download file bytes via Bot API (aiogram helper)."""
    file_path = file.file_path
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content

async def query_hf_inference(image_bytes: bytes) -> dict | None:
    """
    Send image bytes to HF inference endpoint (if configured).
    Expect JSON; return dict or None.
    """
    if not MODEL_API_URL:
        return None
    headers = {}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"
    # Accept either direct model inference or custom built endpoints. We try sending binary.
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(MODEL_API_URL, content=image_bytes, headers=headers)
            text = r.text
            if r.status_code != 200:
                logger.warning("HF returned status %s: %s", r.status_code, text[:200])
                return None
            # Try parse JSON
            try:
                return r.json()
            except Exception:
                logger.warning("HF returned HTML or invalid JSON: %.200s", text)
                return None
    except Exception as e:
        logger.exception("HF inference call failed: %s", e)
        return None

def skin_mask_ratio(pil_image: Image.Image) -> float:
    """
    Very simple skin detection heuristic:
    - convert to YCrCb and mark pixels in common skin Cr/Cb ranges
    Returns ratio (0..1) of skin-like pixels to total.
    This is a fallback heuristic and not perfect, but useful when HF endpoint is dead.
    """
    # Resize for performance
    w, h = pil_image.size
    scale = 1.0
    if w * h > 2000000:
        scale = (2000000 / (w * h)) ** 0.5
    if scale < 1.0:
        new_w = int(w * scale)
        new_h = int(h * scale)
        pil_image = pil_image.resize((new_w, new_h), Image.BILINEAR)

    arr = np.asarray(pil_image.convert("RGB"))
    if arr.size == 0:
        return 0.0
    # Convert RGB -> YCrCb
    # Y  =  0.299 R + 0.587 G + 0.114 B
    # Cr = (R - Y) * 0.713 + 128
    # Cb = (B - Y) * 0.564 + 128
    R = arr[:, :, 0].astype(np.float32)
    G = arr[:, :, 1].astype(np.float32)
    B = arr[:, :, 2].astype(np.float32)
    Y = 0.299 * R + 0.587 * G + 0.114 * B
    Cr = (R - Y) * 0.713 + 128
    Cb = (B - Y) * 0.564 + 128

    # Empirical skin ranges (typical): Cr in [140,180], Cb in [100,135]
    skin_mask = (Cr >= 140) & (Cr <= 180) & (Cb >= 95) & (Cb <= 135)
    # Also require pixel not extremely dark or extremely bright
    bright_mask = (Y >= 40) & (Y <= 240)
    final_mask = skin_mask & bright_mask

    skin_pixels = final_mask.sum()
    total_pixels = final_mask.size
    ratio = float(skin_pixels) / float(total_pixels)
    return ratio

async def nsfw_score_from_bytes(image_bytes: bytes) -> tuple[float, str]:
    """
    Return (score, source) where score ~probability of NSFW (0..1).
    If HF used, return HF score. Otherwise fallback to skin heuristic.
    """
    # Try HF inference
    hf_res = await query_hf_inference(image_bytes)
    if hf_res is not None:
        # Try to parse common HF detector outputs
        # Some HF detectors return list of dicts like [{"label":"nsfw","score":0.xxx}, ...]
        try:
            if isinstance(hf_res, dict):
                # many custom endpoints return {'nsfw': 0.9} or {'label': 'nsfw', 'score': ...}
                # try a few keys
                for key in ("nsfw", "porn", "adult", "sexual"):
                    if key in hf_res and isinstance(hf_res[key], (int, float)):
                        return float(hf_res[key]), "hf"
                # maybe single detection
                if "label" in hf_res and "score" in hf_res:
                    lbl = str(hf_res.get("label", "")).lower()
                    sc = float(hf_res.get("score", 0.0))
                    if "nsfw" in lbl or "sexual" in lbl or "porn" in lbl or "adult" in lbl:
                        return sc, "hf"
                # maybe nested scores
                # bail out to fallback if can't parse
            elif isinstance(hf_res, list):
                # list of labels/scores
                # find any label that looks like nsfw/adult/porn
                for item in hf_res:
                    if isinstance(item, dict) and "label" in item and "score" in item:
                        lbl = str(item["label"]).lower()
                        if any(k in lbl for k in ("nsfw", "adult", "porn", "sexual")):
                            return float(item["score"]), "hf"
                # else maybe first item probability is NSFW
                first = hf_res[0]
                if isinstance(first, dict) and "score" in first:
                    return float(first["score"]), "hf"
        except Exception as e:
            logger.warning("Error parsing HF response: %s", e)
        logger.info("HF returned but couldn't parse; falling back to heuristic")
    # Fallback: skin ratio heuristic
    try:
        pil = Image.open(io.BytesIO(image_bytes))
        w, h = pil.size
        if w * h < MIN_PIXELS_TO_CHECK:
            # small image - cannot judge reliably
            return 0.0, "fallback-small"
        ratio = skin_mask_ratio(pil)
        # We treat higher skin ratio as more likely porn; it's a heuristic:
        score = float(min(1.0, ratio / SKIN_RATIO_THRESHOLD))
        # clamp to [0,1]
        score = max(0.0, min(1.0, score))
        return score, "fallback-skin"
    except Exception as e:
        logger.exception("Fallback heuristic failed: %s", e)
        return 0.0, "fallback-error"

async def take_action(chat_id: int, user_id: int, message_id: int, reason: str):
    """Delete message and mute user for MUTE_SECONDS."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning("Failed delete: %s", e)
    try:
        until = types.ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(chat_id, user_id, permissions=until, until_date=types.datetime.timedelta(seconds=MUTE_SECONDS))
        # Note: aiogram's restrict_chat_member signature may vary by version; if you get errors,
        # replace call with bot.restrict_chat_member(chat_id, user_id, permissions=until, until_date=...)
    except Exception as e:
        logger.warning("Failed mute: %s", e)
    logger.info("Action taken: deleted message %s in chat %s and muted %s for %s sec (%s)",
                message_id, chat_id, user_id, MUTE_SECONDS, reason)

# ----------------- Handlers -----------------
@dp.message_handler(content_types=types.ContentType.PHOTO)
async def on_photo(msg: types.Message):
    logger.info("Photo received: chat=%s user=%s msg=%s", msg.chat.id, msg.from_user.id if msg.from_user else None, msg.message_id)
    # get highest resolution photo
    photo = msg.photo[-1]
    file = await bot.get_file(photo.file_id)
    try:
        b = await download_file_bytes(file)
    except Exception as e:
        logger.exception("Failed to download image: %s", e)
        return

    score, source = await nsfw_score_from_bytes(b)
    logger.info("Score=%s source=%s user=%s chat=%s", score, source, msg.from_user.id if msg.from_user else None, msg.chat.id)

    # Decision logic: prefer HF if used, else fallback threshold
    if source == "hf":
        if score >= HF_NSFW_SCORE_THRESHOLD:
            # delete + mute
            await take_action(msg.chat.id, msg.from_user.id, msg.message_id, f"hf:{score:.3f}")
            try:
                await msg.answer(f"Message removed — detected NSFW ({score:.2f}).")
            except Exception:
                pass
        else:
            logger.info("HF judged safe (%.3f)", score)
    else:
        # fallback skin heuristic: if skin-ratio high enough, act
        # map score (~ratio/SKIN_THRESHOLD) back to ratio estimate
        estimated_ratio = score * SKIN_RATIO_THRESHOLD
        if estimated_ratio >= SKIN_RATIO_THRESHOLD:
            await take_action(msg.chat.id, msg.from_user.id, msg.message_id, f"skin_ratio:{estimated_ratio:.3f}")
            try:
                await msg.answer("Message removed — detected explicit content (fallback heuristic).")
            except Exception:
                pass
        else:
            logger.info("Fallback judged safe (skin_ratio=%.3f)", estimated_ratio)

@dp.message_handler(content_types=types.ContentType.DOCUMENT)
async def on_doc(msg: types.Message):
    # handle images sent as document (jpg/png)
    if not msg.document:
        return
    # only check common image mime types
    if not (msg.document.mime_type and msg.document.mime_type.startswith("image/")):
        return
    logger.info("Document image received: chat=%s user=%s msg=%s", msg.chat.id, msg.from_user.id if msg.from_user else None, msg.message_id)
    file = await bot.get_file(msg.document.file_id)
    try:
        b = await download_file_bytes(file)
    except Exception as e:
        logger.exception("Failed to download doc image: %s", e)
        return
    score, source = await nsfw_score_from_bytes(b)
    logger.info("Doc Score=%s source=%s", score, source)
    if source == "hf":
        if score >= HF_NSFW_SCORE_THRESHOLD:
            await take_action(msg.chat.id, msg.from_user.id, msg.message_id, f"hf:{score:.3f}")
            try:
                await msg.answer(f"Message removed — detected NSFW ({score:.2f}).")
            except Exception:
                pass
    else:
        estimated_ratio = score * SKIN_RATIO_THRESHOLD
        if estimated_ratio >= SKIN_RATIO_THRESHOLD:
            await take_action(msg.chat.id, msg.from_user.id, msg.message_id, f"skin_ratio:{estimated_ratio:.3f}")
            try:
                await msg.answer("Message removed — detected explicit content (fallback heuristic).")
            except Exception:
                pass

# Optional - command to test scoring
@dp.message_handler(commands=["check"])
async def cmd_check(msg: types.Message):
    # reply to an image with /check to show score
    if not msg.reply_to_message:
        await msg.reply("Reply to an image with /check")
        return
    target = msg.reply_to_message
    if target.photo:
        photo = target.photo[-1]
        file = await bot.get_file(photo.file_id)
        b = await download_file_bytes(file)
    elif target.document and target.document.mime_type and target.document.mime_type.startswith("image/"):
        file = await bot.get_file(target.document.file_id)
        b = await download_file_bytes(file)
    else:
        await msg.reply("Replied message is not an image")
        return
    score, source = await nsfw_score_from_bytes(b)
    await msg.reply(f"Score={score:.3f} (source={source})")

# ----------------- Start -----------------
if __name__ == "__main__":
    logger.info("Starting NSFW moderator bot...")
    executor.start_polling(dp, skip_updates=True)