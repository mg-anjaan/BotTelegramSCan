from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse
from model_loader.py import nsfw_model
from PIL import Image
import io

app = FastAPI()

# -----------------------------
# Authorization Key
# -----------------------------
MODEL_API_KEY = "mgPROTECT123"


# -----------------------------
# /ping  → Health Check
# -----------------------------
@app.get("/ping")
async def ping():
    return {"status": "ok", "service": "nsfw-model"}


# -----------------------------
# /score → NSFW Detection
# -----------------------------
@app.post("/score")
async def score(
    image: UploadFile = File(...),
    authorization: str = Header(None)
):
    # Auth check
    if authorization != f"Bearer {MODEL_API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Ensure image
    contents = await image.read()
    try:
        img = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image file")

    # Run model
    nsfw_score = nsfw_model.classify(img)

    return JSONResponse({"score": float(nsfw_score)})
