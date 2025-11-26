# bot.py
import os
import io
import asyncio
import logging
from typing import Optional

from PIL import Image
import numpy as np
import httpx
from aiogram import Bot, Dispatcher
from aiogram.types import Message, ContentType
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus

# ---------- Config from environment ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # REQUIRED
HF_MODEL_URL = os.getenv("HF_MODEL_URL")  # optional, e.g. "https://api-inference.huggingface.co/models/owner/model"
# If HF_MODEL_URL is set but your model needs a token, set HF_AUTH_HEADER e.g. "Bearer <token>"
HF_AUTH_HEADER = os.getenv("HF_AUTH_HEADER")  # optional
FALLBACK_THRESHOLD = float(os.getenv("FALLBACK_THRESHOLD", "0.60"))  # tune to be stricter/lenient
AUTOMUTE = os.getenv("AUTOMUTE", "false").lower() in ("1", "true", "yes")
MUTE_SECONDS = int(os.getenv("MUTE_SECONDS", "86400"))  # default 1 day
MAX_DOWNSCALE = int(os.getenv("MAX_DOWNSCALE", "300"))  # used for blob computation
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable required")

# ---------- Logging ----------
logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger("nsfw-moderator")

# ---------- Bot setup ----------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# ---------- Utility functions ----------

async def call_hf_nsfw(bytes_image: bytes) -> Optional[float]:
    """
    Call HF inference endpoint. Expected to return JSON containing a probability or scores.
    This function tries a few common response shapes, but if HF returns non-JSON or fails,
    we return None to fallback to local detector.
    """
    if not HF_MODEL_URL:
        return None
    headers = {}
    if HF_AUTH_HEADER:
        headers["Authorization"] = HF_AUTH_HEADER
    # If HF model expects bytes directly:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(HF_MODEL_URL, content=bytes_image, headers=headers)
            text = resp.text
            # try parse json
            try:
                j = resp.json()
            except Exception:
                log.error("HF returned HTML or invalid JSON")
                return None
            # common formats:
            # 1) { "label": "nsfw", "score": 0.98 } or { "nsfw": 0.9 }
            if isinstance(j, dict):
                if "score" in j and isinstance(j["score"], (int, float)):
                    return float(j["score"])
                # label+score
                if "label" in j and "score" in j:
                    return float(j["score"])
                # map of labels to scores
                for key in ("nsfw", "porn", "sexual", "adult"):
                    if key in j and isinstance(j[key], (int, float)):
                        return float(j[key])
                # shortlist list outputs: [{"label":"NSFW","score":0.99}, ...]
            if isinstance(j, list) and len(j) > 0 and isinstance(j[0], dict):
                # find NSFW-like label
                for item in j:
                    lbl = item.get("label", "").lower()
                    sc = item.get("score")
                    if sc is None:
                        continue
                    if "nsfw" in lbl or "porn" in lbl or "adult" in lbl or "sexual" in lbl:
                        return float(sc)
                # otherwise return top score
                top = max((it.get("score", 0.0) for it in j if isinstance(it, dict)), default=0.0)
                return float(top)
    except httpx.HTTPStatusError as e:
        log.exception("HF HTTP error")
    except Exception:
        log.exception("HF call failed")
    return None


def pil_image_from_bytes(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def simple_skin_mask(npimg: np.ndarray) -> np.ndarray:
    """
    A classic rule-based skin detection in RGB (fast, no OpenCV):
    Condition from literature:
      R > 95 and G > 40 and B > 20 and (max(R,G,B) - min(R,G,B)) > 15
      and |R - G| > 15 and R > G and R > B
    Returns boolean mask same HxW.
    """
    R = npimg[:, :, 0].astype(int)
    G = npimg[:, :, 1].astype(int)
    B = npimg[:, :, 2].astype(int)
    maxc = np.maximum(np.maximum(R, G), B)
    minc = np.minimum(np.minimum(R, G), B)

    cond = (
        (R > 95) &
        (G > 40) &
        (B > 20) &
        ((maxc - minc) > 15) &
        (np.abs(R - G) > 15) &
        (R > G) &
        (R > B)
    )
    return cond


def largest_blob_ratio(mask: np.ndarray, max_downscale: int = MAX_DOWNSCALE) -> float:
    """
    Compute largest connected component ratio on a downscaled boolean mask.
    We downscale for speed. Returns fraction of total pixels that the largest connected skin blob covers.
    """
    h, w = mask.shape
    scale = 1.0
    if max(h, w) > max_downscale:
        scale = max_downscale / max(h, w)
    if scale < 1.0:
        # downscale using simple slicing (preserve structure roughly)
        new_h = max(1, int(h * scale))
        new_w = max(1, int(w * scale))
        mask_small = Image.fromarray((mask * 255).astype(np.uint8)).resize((new_w, new_h), Image.NEAREST)
        mask_small = np.array(mask_small) > 127
    else:
        mask_small = mask

    # BFS connected components (4-neighbors)
    visited = np.zeros_like(mask_small, dtype=bool)
    H, W = mask_small.shape
    max_count = 0
    # neighbors offsets
    from collections import deque
    for i in range(H):
        for j in range(W):
            if not mask_small[i, j] or visited[i, j]:
                continue
            q = deque()
            q.append((i, j))
            visited[i, j] = True
            cnt = 0
            while q:
                y, x = q.popleft()
                cnt += 1
                # 4 neighbors
                if y > 0 and mask_small[y-1, x] and not visited[y-1, x]:
                    visited[y-1, x] = True; q.append((y-1, x))
                if y+1 < H and mask_small[y+1, x] and not visited[y+1, x]:
                    visited[y+1, x] = True; q.append((y+1, x))
                if x > 0 and mask_small[y, x-1] and not visited[y, x-1]:
                    visited[y, x-1] = True; q.append((y, x-1))
                if x+1 < W and mask_small[y, x+1] and not visited[y, x+1]:
                    visited[y, x+1] = True; q.append((y, x+1))
            if cnt > max_count:
                max_count = cnt

    total_small = mask_small.size
    if total_small == 0:
        return 0.0
    return float(max_count) / float(total_small)


def fallback_nsfw_score(pil_img: Image.Image) -> float:
    """
    Simple fallback scoring combining skin ratio and largest blob.
    Returns value in [0,1]. Tweak weights if needed.
    """
    npimg = np.array(pil_img)
    h, w, _ = npimg.shape
    # compute mask
    mask = simple_skin_mask(npimg)
    skin_ratio = float(np.count_nonzero(mask)) / float(h * w)
    blob_ratio = largest_blob_ratio(mask)
    # weights: skin ratio matters a lot, blob helps bump up porn-like images
    score = (skin_ratio * 0.75) + (blob_ratio * 0.25)
    # clamp
    return max(0.0, min(1.0, score))


# ---------- Bot handlers ----------

@dp.message.register(Command(commands=["start", "help"]))
async def cmd_start(message: Message):
    await message.reply("NSFW Scanner bot active. I only scan images and delete porn. Contact owner to change settings.")


async def moderate_image_bytes(chat_id: int, user_id: int, message_id: int, content_bytes: bytes) -> Optional[float]:
    """
    Returns final NSFW score (0..1). Also carries out deletion + optional mute if above threshold.
    """
    # 1) Try HF
    hf_score = await call_hf_nsfw(content_bytes)
    if hf_score is not None:
        log.info("HF score=%.3f user=%s chat=%s", hf_score, user_id, chat_id)
        score = float(hf_score)
    else:
        # fallback
        try:
            pil = pil_image_from_bytes(content_bytes)
            score = fallback_nsfw_score(pil)
        except Exception:
            log.exception("fallback detection failed")
            score = 0.0
        log.info("Fallback Score = %.3f", score)

    # action
    if score >= FALLBACK_THRESHOLD:
        # delete message
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            log.info("Deleted porn message user=%s chat=%s score=%.3f", user_id, chat_id, score)
        except Exception:
            log.exception("Failed to delete message (bot needs admin rights with delete_messages)")

        # optional automute (restrict user from sending messages)
        if AUTOMUTE:
            try:
                # restrict_member API
                await bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions={
                        "can_send_messages": False,
                        "can_send_media_messages": False,
                        "can_send_other_messages": False,
                        "can_add_web_page_previews": False,
                    },
                    until_date=int(asyncio.get_event_loop().time()) + MUTE_SECONDS
                )
                log.info("Auto-muted user=%s in chat=%s", user_id, chat_id)
            except Exception:
                log.exception("Failed to automute (bot needs admin rights with restrict_members)")

        return score
    return score


@dp.message.register(content_types=[ContentType.PHOTO, ContentType.DOCUMENT])
async def on_image(message: Message):
    # ensure it's an image (document can be other types)
    try:
        # if document, check mime
        if message.content_type == ContentType.DOCUMENT:
            doc = message.document
            if not doc or not (doc.mime_type and doc.mime_type.startswith("image/")):
                return  # ignore non-image documents

        # download file bytes
        file = await message.download(destination=io.BytesIO())
        file.seek(0)
        content = file.read()

        # moderate
        score = await moderate_image_bytes(message.chat.id, message.from_user.id, message.message_id, content)

        # notify done if safe but we deleted: send ephemeral warning in chat
        if score is not None and score >= FALLBACK_THRESHOLD:
            try:
                await message.answer(
                    f"⚠️ <b>Removed media</b> — content flagged as explicit (score {score:.2f}). Please follow the rules."
                )
            except Exception:
                pass
        else:
            # you can log safe images; don't notify chat
            pass

    except Exception:
        log.exception("Error handling image message")


# ---------- run ----------
if __name__ == "__main__":
    # run polling loop
    # Note: dp.run_polling accepts a bot or dispatcher settings depending on aiogram version.
    log.info("Starting NSFW scanner bot...")
    try:
        dp.run_polling(bot)
    finally:
        asyncio.run(bot.session.close())