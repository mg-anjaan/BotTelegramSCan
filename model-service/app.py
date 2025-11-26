# model-service/app.py
import io
import os
import logging
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-service")

# Import wrapper that exposes nsfw_model.classify(img)
try:
    from model_loader import nsfw_model
except Exception as e:
    logger.exception("Failed to import nsfw model (check model_loader.py). Exception:")
    raise

app = FastAPI(title="NSFW model service")

MODEL_API_KEY = os.getenv("MODEL_API_KEY", "mgPROTECT123")
if MODEL_API_KEY:
    logger.info("MODEL_API_KEY loaded from environment (value hidden).")

@app.get("/ping")
async def ping():
    return {"status": "ok", "service": "nsfw-model"}

@app.post("/score")
async def score(image: UploadFile = File(...), authorization: str = Header(None)):
    # Expect Authorization: Bearer <MODEL_API_KEY>
    if authorization is None or not authorization.startswith("Bearer "):
        logger.warning("Unauthorized request (missing Authorization header)")
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = authorization.split(" ", 1)[1].strip()
    if token != MODEL_API_KEY:
        logger.warning("Unauthorized request (invalid API key)")
        raise HTTPException(status_code=401, detail="Invalid API key")

    contents = await image.read()
    if not contents:
        logger.warning("Empty file uploaded")
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception:
        logger.exception("Failed to parse uploaded image")
        raise HTTPException(status_code=400, detail="Invalid image file")

    try:
        score_val = nsfw_model.classify(img)
    except AttributeError:
        logger.exception("nsfw_model has no 'classify' method. Check model_loader implementation.")
        raise HTTPException(status_code=500, detail="Model not callable")
    except Exception:
        logger.exception("Model inference failed")
        raise HTTPException(status_code=500, detail="Model inference error")

    try:
        score_float = float(score_val)
    except Exception:
        logger.exception("Model returned non-numeric score")
        raise HTTPException(status_code=500, detail="Invalid model output")

    return JSONResponse({"score": score_float})