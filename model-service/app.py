# app.py
import io
import logging
import os
from typing import Dict

from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-model-service")

# Config
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "mgmodelsecret123")
MODEL_TYPE = os.getenv("MODEL_TYPE", "dummy")  # "onnx" or "dummy"
MODEL_PATH = os.getenv("MODEL_PATH", "/app/model.onnx")
# thresholds not used by service but helpful for local tests
GENITAL_THRESHOLD = float(os.getenv("GENITAL_THRESHOLD", "0.70"))
BREAST_THRESHOLD = float(os.getenv("BREAST_THRESHOLD", "0.70"))

# Attempt to import/load model wrapper
try:
    from model_loader import nsfw_model  # must expose classify(PIL.Image) -> dict
except Exception as e:
    logger.exception("Failed to import model_loader (service will fail). Exception:")
    raise

app = FastAPI(title="NSFW model service (score + categories)")

@app.get("/ping")
async def ping():
    return {"status": "ok", "service": "nsfw-model"}

@app.post("/score")
async def score(image: UploadFile = File(...), authorization: str = Header(None)):
    """
    Headers:
      Authorization: Bearer <MODEL_API_KEY>

    Returns JSON:
      {"score": 0.87, "genitals": 0.83, "breasts": 0.12, "skin_ratio": 0.24}
    """
    # Auth
    if authorization != f"Bearer {MODEL_API_KEY}":
        logger.warning("Unauthorized request (missing/invalid API key)")
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Read image
    contents = await image.read()
    if not contents:
        logger.warning("Empty file uploaded")
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        pil_img = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception:
        logger.exception("Failed to parse uploaded image")
        raise HTTPException(status_code=400, detail="Invalid image file")

    # Run model wrapper; expect dict (score, genitals, breasts, skin_ratio)
    try:
        out = nsfw_model.classify(pil_img)
    except AttributeError:
        logger.exception("nsfw_model has no 'classify' method. Check model_loader.")
        raise HTTPException(status_code=500, detail="Model not callable")
    except Exception:
        logger.exception("Model inference failed")
        raise HTTPException(status_code=500, detail="Model inference error")

    # Validate output
    if not isinstance(out, dict):
        logger.error("Model returned invalid output (not a dict): %r", out)
        raise HTTPException(status_code=500, detail="Invalid model output")

    # Normalize keys
    score = float(out.get("score", 0.0))
    genitals = float(out.get("genitals", 0.0))
    breasts = float(out.get("breasts", 0.0))
    skin_ratio = float(out.get("skin_ratio", 0.0))

    resp = {"score": score, "genitals": genitals, "breasts": breasts, "skin_ratio": skin_ratio}
    return JSONResponse(resp)