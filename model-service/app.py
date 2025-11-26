# app.py
import os
import io
import traceback
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
from PIL import Image
import numpy as np

app = FastAPI(title="NSFW ONNX Inference")

# Try import onnxruntime with a helpful error if it fails
try:
    import onnxruntime as ort
except Exception as e:
    ort = None
    print("⚠️ onnxruntime import failed:", e)
    traceback.print_exc()

# Change these to match how your model expects input
MODEL_PATH = os.getenv("MODEL_PATH", "nsfw_model.onnx")
SESSION = None

def load_session():
    global SESSION
    if ort is None:
        return None
    if SESSION is None:
        SESSION = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    return SESSION

def preprocess_image_bytes(img_bytes):
    # example preprocessing: resize 224x224, RGB, normalize (adjust to your model)
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    im = im.resize((224, 224))
    arr = np.array(im).astype(np.float32) / 255.0
    # shape (1,3,224,224) if model expects channels-first
    arr = np.transpose(arr, (2,0,1))[None, ...]
    return arr

class Prediction(BaseModel):
    nsfw_score: float

@app.post("/predict", response_model=Prediction)
async def predict(file: UploadFile = File(...)):
    if ort is None:
        # Friendly error so logs show reason
        raise HTTPException(status_code=500, detail="onnxruntime not available in this environment. See container logs.")
    try:
        session = load_session()
        if session is None:
            raise HTTPException(status_code=500, detail="Failed to initialize onnxruntime session")
        content = await file.read()
        input_arr = preprocess_image_bytes(content)
        # unify input name from model - get actual input name
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: input_arr})
        # adjust according to model's output shape
        # Suppose outputs[0] = [prob_nsfw] or [prob_safe, prob_nsfw]
        out = outputs[0]
        # if out shape (1,2) and second col is NSFW:
        if out.ndim == 2 and out.shape[1] >= 2:
            nsfw_prob = float(out[0,1])
        else:
            nsfw_prob = float(out.flat[0])
        return {"nsfw_score": nsfw_prob}
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"prediction error: {ex}")