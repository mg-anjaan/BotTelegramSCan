# model-service/app.py
import io
import logging
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse
from PIL import Image

# Setup logging so Railway logs are informative
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nsfw-service")

# Attempt import of your model module with clear error if wrong
try:
    # <-- CORRECT import: no ".py" extension here
    from model_loader import nsfw_model
except Exception as e:
    # Log and re-raise so the container startup fails visibly in logs
    logger.exception("Failed to import nsfw model (check model_loader.py). Exception:")
    # Re-raise to stop startup (uvicorn will show the stacktrace in logs)
    raise

app = FastAPI(title="NSFW model service")

# API key used by your tests (keep in sync with tests / curl header)
MODEL_API_KEY = "mgPROTECT123"


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
    # Auth
    if authorization != f"Bearer {MODEL_API_KEY}":
        logger.warning("Unauthorized request (missing/invalid API key)")
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Read image bytes
    contents = await image.read()
    if not contents:
        logger.warning("Empty file uploaded")
        raise HTTPException(status_code=400, detail="Empty file")

    # Try to load image using Pillow
    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception as exc:
        logger.exception("Failed to parse uploaded image")
        raise HTTPException(status_code=400, detail="Invalid image file")

    # Run the classifier — adapt if your model API differs
    try:
        # expecting nsfw_model.classify(image) -> float or numeric
        score_val = nsfw_model.classify(img)
    except AttributeError:
        logger.exception("nsfw_model has no 'classify' method. Check model_loader implementation.")
        raise HTTPException(status_code=500, detail="Model not callable")
    except Exception:
        logger.exception("Model inference failed")
        raise HTTPException(status_code=500, detail="Model inference error")

    # Ensure numeric and convert to float for JSON
    try:
        score_float = float(score_val)
    except Exception:
        logger.exception("Model returned non-numeric score")
        raise HTTPException(status_code=500, detail="Invalid model output")

    return JSONResponse({"score": score_float})

