# bot.py — improved NSFW filter + robust automute (aiogram v3)
import os
import io
import logging
import asyncio
from typing import Optional
from functools import wraps

from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, ContentType, ChatMember
import httpx
from PIL import Image

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nsfw-moderator")

# ---------- CONFIG (from env)
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
HF_MODEL_URL = os.getenv("HF_MODEL_URL")  # optional huggingface model endpoint
HF_TOKEN = os.getenv("HF_TOKEN")  # optional token for HF
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.75"))
PERMANENT_MUTE = os.getenv("PERMANENT_MUTE", "true").lower() in ("1", "true", "yes")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "4"))  # limit concurrent HF calls
# -----------------------

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# semaphore for HF calls
hf_sem = asyncio.Semaphore(MAX_CONCURRENT)

# small decorator to catch & log exceptions in handlers so we don't silently skip
def safe_handler(func):
    @wraps(func)
    async def wrapper(*a, **kw):
        try:
            return await func(*a, **kw)
        except Exception as e:
            log.exception("Unhandled exception in handler:")
            # don't re-raise — we want handler to continue for other updates
    return wrapper

# ---------- Fallback detector (Pillow-only, no numpy)
def fallback_nsfw_score_pillow(img: Image.Image) -> float:
    """
    Skin-color heuristic using YCbCr thresholds.
    Returns score 0..1 (higher => more likely NSFW).
    """
    try:
        w, h = img.size
        # downscale to speed up heavy images
        max_pixels = 800 * 800
        if w * h > max_pixels:
            scale = (max_pixels / (w * h)) ** 0.5
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        ycbcr = img.convert("YCbCr")
        pixels = ycbcr.getdata()
        total = 0
        skin = 0
        for (y, cb, cr) in pixels:
            total += 1
            # common empirical ranges for skin-like YCbCr
            if 77 <= cb <= 127 and 133 <= cr <= 173 and y > 40:
                skin += 1
        ratio = skin / max(1, total)
        # amplify mild ratios a bit
        score = min(1.0, ratio * 1.8)
        return score
    except Exception:
        log.exception("Fallback detector error")
        return 0.0

# ---------- HF model check (safe, with semaphore & retries)
async def check_with_hf_model(image_bytes: bytes) -> Optional[float]:
    if not HF_MODEL_URL:
        return None
    headers = {}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"
    # set common headers but many HF endpoints accept raw bytes
    headers.setdefault("Content-Type", "application/octet-stream")
    # try few times for transient network errors
    async with hf_sem:
        for attempt in range(1, 3):
            try:
                async with httpx.AsyncClient(timeout=25.0) as client:
                    resp = await client.post(HF_MODEL_URL, content=image_bytes, headers=headers)
                    if resp.status_code >= 400:
                        log.warning("HF returned %s (attempt %d): %s", resp.status_code, attempt, resp.text[:300])
                        # If 410 or 404 -> model endpoint likely removed; don't retry
                        if resp.status_code in (404, 410):
                            return None
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    # parse JSON if possible
                    try:
                        data = resp.json()
                    except Exception:
                        # some HF endpoints return plain text/HTML on errors — treat as failure
                        log.warning("HF returned non-JSON (attempt %d): %s", attempt, resp.text[:300])
                        return None
                    # Interpret common response shapes
                    # 1) dict with numeric field
                    if isinstance(data, dict):
                        for k in ("score", "nsfw_score", "nsfw", "probability", "prob"):
                            if k in data:
                                try:
                                    return float(data[k])
                                except Exception:
                                    pass
                    # 2) list of label dicts: [{"label":"NSFW","score":0.98}, ...]
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        # prefer an entry labeled NSFW
                        for item in data:
                            label = str(item.get("label", "")).upper()
                            if "NSFW" in label and "score" in item:
                                try:
                                    return float(item["score"])
                                except Exception:
                                    pass
                        # fallback: use first numeric score if present
                        if "score" in data[0]:
                            try:
                                return float(data[0]["score"])
                            except Exception:
                                pass
                    # 3) direct number
                    if isinstance(data, (float, int)):
                        return float(data)
                    # else can't interpret
                    log.warning("HF JSON not interpretable (attempt %d): %s", attempt, str(data)[:300])
                    return None
            except (httpx.RequestError, httpx.TimeoutException) as ex:
                log.warning("HF request error (attempt %d): %s", attempt, ex)
                await asyncio.sleep(0.5 * attempt)
                continue
            except Exception:
                log.exception("Unexpected HF error")
                return None
    return None

# ---------- helper to download Telegram file bytes
async def download_telegram_file(file: types.File) -> bytes:
    file_path = file.file_path
    tg_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(tg_url)
        r.raise_for_status()
        return r.content

# ---------- check if bot is admin with restrict rights in a chat
async def bot_can_restrict(chat_id: int) -> bool:
    try:
        me = await bot.get_me()
        member: ChatMember = await bot.get_chat_member(chat_id, me.id)
        # statuses: "administrator" or "creator"
        if member.status == "creator":
            return True
        if member.status == "administrator":
            # check rights object (may be missing in some environments)
            rights = getattr(member, "privileges", None) or getattr(member, "administrator_rights", None) or getattr(member, "can_restrict_members", None)
            # Different aiogram versions may present permissions differently; check robustly
            if hasattr(member, "can_restrict_members"):
                return bool(member.can_restrict_members)
            # fallback: assume admin can restrict (best-effort)
            return True
    except Exception:
        log.exception("Failed to determine bot admin status")
    return False

# ---------- Main handler: register for photos/documents/animations/webp
@safe_handler
async def process_image_message(msg: Message):
    # get file object depending on type
    file_obj = None
    if msg.photo:
        file_obj = await bot.get_file(msg.photo[-1].file_id)
    elif msg.document and (msg.document.mime_type or "").startswith("image"):
        file_obj = await bot.get_file(msg.document.file_id)
    elif msg.animation:  # animated gif/webp — still treat as image (frame extraction not done)
        file_obj = await bot.get_file(msg.animation.file_id)
    else:
        return  # ignore other messages

    # download bytes safely
    try:
        image_bytes = await download_telegram_file(file_obj)
    except Exception:
        log.exception("Failed to download image for message %s", msg.message_id)
        return

    score = 0.0
    source = "none"
    # Try HF model first if provided
    hf_score = await check_with_hf_model(image_bytes)
    if hf_score is not None:
        try:
            score = float(hf_score)
            source = "hf"
            log.info("Score from HF = %.3f user=%s chat=%s msg=%s", score, getattr(msg.from_user, "id", None), msg.chat.id, msg.message_id)
        except Exception:
            hf_score = None

    # If HF not available or failed, fallback to Pillow
    if hf_score is None:
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            score = fallback_nsfw_score_pillow(img)
            source = "fallback"
            log.info("Fallback Score = %.3f user=%s chat=%s msg=%s", score, getattr(msg.from_user, "id", None), msg.chat.id, msg.message_id)
        except Exception:
            log.exception("Fallback image processing failed for msg=%s", msg.message_id)
            score = 0.0

    # If above threshold => delete + optional ban
    if score >= NSFW_THRESHOLD:
        try:
            await msg.delete()
            log.info("Deleted NSFW msg=%s user=%s chat=%s score=%.3f (src=%s)", msg.message_id, getattr(msg.from_user, "id", None), msg.chat.id, score, source)
        except Exception:
            log.exception("Failed to delete NSFW message %s", msg.message_id)

        # perform automute/ban if requested & applicable
        if PERMANENT_MUTE and msg.chat.type in ("group", "supergroup"):
            can_restrict = await bot_can_restrict(msg.chat.id)
            if not can_restrict:
                log.error("Bot cannot restrict/ban members in chat %s — make me admin with ban rights", msg.chat.id)
            else:
                try:
                    user_id = msg.from_user.id if msg.from_user else None
                    if user_id:
                        # ban_chat_member arguments differ between versions; use the simple call
                        await bot.ban_chat_member(chat_id=msg.chat.id, user_id=user_id)
                        log.info("Banned user %s in chat %s (permanent mute)", user_id, msg.chat.id)
                except Exception:
                    log.exception("Failed to ban user after NSFW message in chat %s", msg.chat.id)
    else:
        # Below threshold — allow message. Optionally log low scores.
        log.debug("Allowed image msg=%s score=%.3f (src=%s)", msg.message_id, score, source)

# register handler for all relevant content types (photos, documents (images), animations)
# aiogram v3 registration style:
dp.message.register(process_image_message, ContentType.PHOTO)
dp.message.register(process_image_message, ContentType.DOCUMENT)
dp.message.register(process_image_message, ContentType.ANIMATION)

# start polling
if __name__ == "__main__":
    try:
        log.info("Bot starting (polling)...")
        asyncio.run(dp.start_polling(bot))
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
    finally:
        try:
            asyncio.run(bot.session.close())
        except Exception:
            pass