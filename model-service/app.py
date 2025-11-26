# app.py - FastAPI model server
import os
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from model_loader import nsfw_model
from PIL import Image
import io

app = FastAPI()
API_KEY = os.getenv("MODEL_API_KEY", "change_me_to_a_secret")

@app.post("/score")
async def score(image: UploadFile = File(...), authorization: str = Header(None)):
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization")
    token = authorization.split(" ", 1)[1]
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

    contents = await image.read()
    try:
        pil = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image")

    score = nsfw_model.predict(pil)
    return {"score": float(score)}
