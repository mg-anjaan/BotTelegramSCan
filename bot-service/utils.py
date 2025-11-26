# bot-service/utils.py
import os
import httpx
import asyncio
from typing import Optional

MODEL_API_URL = os.getenv("MODEL_API_URL")
MODEL_SECRET = os.getenv("MODEL_SECRET")

if not MODEL_API_URL:
    raise RuntimeError("MODEL_API_URL not set in environment")

async def get_image_score(image_bytes: bytes, filename: str = "image.jpg") -> Optional[float]:
    headers = {"Authorization": f"Bearer {MODEL_SECRET}"} if MODEL_SECRET else {}
    files = {"image": (filename, image_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(MODEL_API_URL, headers=headers, files=files)
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("score", 0.0))