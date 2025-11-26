# model-service/app.py  (REPLACE your current file with this)
import os
import io
import logging
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse
from PIL import Image

# Setup logging so Railway logs are informative
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-service")

# Load API key from environment (fall back to a debug default only if not set)
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "mgPROTECT123")

# Attempt import of your model module with clear error if wrong
try:
    from model_loader import nsfw_model
except Exception as e:
    logger.exception("Failed to import nsfw model (check model_loader.py). Exception:")
    raise

app = FastAPI(title="NSFW model service")


@app.get("/ping")
async def ping():
    """Health check"""
    return {"status": "ok", "service": "nsfw-model"}


@app.post("/score")
async def score(image: UploadFile = File(...), authorization: str = Header(None)):
    """
    Accepts multipart/form-data file "image".
    Header: Authorization: Bearer <KEY>
    Returns: {"score": 0.87}
    """
    # Auth — allow header forms like "Bearer <KEY>"
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("Unauthorized request (missing Authorization header)")
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization.split(" ", 1)[1]
    if token != MODEL_API_KEY:
        logger.warning("Unauthorized request (invalid API key)")
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Read image bytes
    contents = await image.read()
    if not contents:
        logger.warning("Empty file uploaded")
        raise HTTPException(status_code=400, detail="Empty file")

    # Try to load image using Pillow
    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception:
        logger.exception("Failed to parse uploaded image")
        raise HTTPException(status_code=400, detail="Invalid image file")

    # Run the classifier — support both predict() and classify()
    try:
        if hasattr(nsfw_model, "predict") and callable(getattr(nsfw_model, "predict")):
            score_val = nsfw_model.predict(img)
            logger.debug("Used nsfw_model.predict")
        elif hasattr(nsfw_model, "classify") and callable(getattr(nsfw_model, "classify")):
            score_val = nsfw_model.classify(img)
            logger.debug("Used nsfw_model.classify")
        else:
            logger.error("nsfw_model has no 'predict' or 'classify' method. Check model_loader implementation.")
            raise HTTPException(status_code=500, detail="Server misconfiguration: model method not found")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Model inference failed")
        raise HTTPException(status_code=500, detail="Model inference error")

    # Ensure numeric and convert to float for JSON
    try:
        score_float = float(score_val)
    except Exception:
        logger.exception("Model returned non-numeric score")
        raise HTTPException(status_code=500, detail="Invalid model output")

    # Clip to [0,1] just in case and return
    if score_float < 0:
        score_float = 0.0
    elif score_float > 1:
        score_float = 1.0

    logger.info("Score returned: %.4f", score_float)
    return JSONResponse({"score": score_float})