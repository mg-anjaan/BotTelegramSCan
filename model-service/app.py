# model-service/app.py
import io
import logging
import os
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-service")

# read secret from env
MODEL_SECRET = os.environ.get("MODEL_SECRET", "mgPROTECT12345")

# try to import a model loader that exposes nsfw_model.classify(img) -> float
try:
    from model_loader import nsfw_model
except Exception:
    logger.exception("Could not import model_loader or nsfw_model. Make sure model_loader.py exists.")
    # Allow app to start but any inference attempt will error clearly.
    nsfw_model = None

app = FastAPI(title="NSFW model service")


@app.get("/ping")
async def ping():
    return {"status": "ok", "service": "nsfw-model"}


@app.post("/score")
async def score(image: UploadFile = File(...), authorization: str = Header(None)):
    """
    Accepts multipart/form-data file "image".
    Header: Authorization: Bearer <MODEL_SECRET>
    Returns: {"score": 0.87}
    """
    # Check header
    if authorization is None:
        logger.warning("Unauthorized request (missing Authorization header)")
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.startswith("Bearer "):
        logger.warning("Unauthorized request (invalid Authorization header format)")
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    token = authorization.split(" ", 1)[1].strip()
    if token != MODEL_SECRET:
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

    if nsfw_model is None:
        logger.error("Model not loaded")
        raise HTTPException(status_code=500, detail="Model not loaded")

    try:
        score_val = nsfw_model.classify(img)  # expects numeric
    except Exception:
        logger.exception("Model inference failed")
        raise HTTPException(status_code=500, detail="Model inference error")

    try:
        score_float = float(score_val)
    except Exception:
        logger.exception("Model returned non-numeric score")
        raise HTTPException(status_code=500, detail="Invalid model output")

    return JSONResponse({"score": score_float})