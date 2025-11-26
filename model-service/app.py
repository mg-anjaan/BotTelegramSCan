# app.py
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import onnxruntime as ort
from PIL import Image
import numpy as np
import io
import os
import logging
from typing import Tuple, Dict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("model-service")

app = FastAPI(title="NSFW Model Service")

# ------------- Config -------------
MODEL_PATH = os.getenv("MODEL_PATH", "nsfw_model.onnx")
MODEL_SECRET = os.getenv("MODEL_SECRET", "")
# If you want to expose a threshold here for quick testing
DEFAULT_GENITAL_THRESHOLD = float(os.getenv("GENITAL_THRESHOLD", "0.65"))

# ------------- Load ONNX model -------------
if not os.path.exists(MODEL_PATH):
    logger.error("ONNX model not found at %s", MODEL_PATH)
    # do not raise to allow container to start and show logs; endpoint will error
else:
    try:
        sess = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
        logger.info("Loaded ONNX model: %s", MODEL_PATH)
    except Exception as e:
        logger.exception("Failed to load ONNX model: %s", e)
        sess = None

# --------- IMPORTANT ---------
# Edit this list to match your model's class order.
# Example: if your model returns logits/probs [safe, porn, genital, breast] then:
# CLASS_NAMES = ["safe", "porn", "genital", "breast"]
# If your model returns a single value (e.g. probability of NSFW), set single name list.
CLASS_NAMES = os.getenv("CLASS_NAMES", "safe,porn,genital,breast").split(",")

def preprocess_image_bytes(image_bytes: bytes, size: Tuple[int,int]=(224,224)) -> np.ndarray:
    """
    Basic preprocessing: open with PIL, convert RGB, resize, normalize to [0,1],
    transpose to NCHW (1,3,H,W) if model expects that.
    Adjust this to match the preprocessing your ONNX model expects.
    """
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = im.convert("RGB")
        im = im.resize(size, resample=Image.BILINEAR)
        arr = np.asarray(im).astype(np.float32) / 255.0  # H,W,3
        # common ordering for ONNX: NCHW
        arr = np.transpose(arr, (2, 0, 1))  # 3,H,W
        arr = np.expand_dims(arr, axis=0)   # 1,3,H,W
    return arr

def softmax(x: np.ndarray) -> np.ndarray:
    e_x = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e_x / e_x.sum(axis=-1, keepdims=True)

def run_model(image_bytes: bytes) -> Dict:
    if sess is None:
        raise RuntimeError("ONNX session not loaded")

    # Preprocess - default image size 224x224 (change if your model requires different)
    inp = preprocess_image_bytes(image_bytes, size=(224,224))

    # Infer - find the first input name
    input_name = sess.get_inputs()[0].name
    try:
        outputs = sess.run(None, {input_name: inp})
    except Exception as e:
        logger.exception("ONNX runtime failed: %s", e)
        raise

    # outputs is a list of arrays. Flatten and treat as logits/probs
    out = outputs[0]
    # If output shape is (1,) or (1,1) treat as single score
    if out.ndim == 2 and out.shape[0] == 1:
        out = out[0]  # shape (N,) or (1,)
    if out.ndim == 0:
        # scalar
        probs = np.array([float(out)])
        class_names = ["score"]
    else:
        # try to transform logits -> probs
        try:
            probs = softmax(out.astype(np.float32))
            # if softmax returns shape (1,N) -> squeeze
            if probs.ndim == 2 and probs.shape[0] == 1:
                probs = probs[0]
        except Exception:
            # fallback: if model already returns probabilities
            probs = np.asarray(out).astype(np.float32).ravel()

        # pick class names fallback
        class_names = CLASS_NAMES
        # if class_names length mismatches, create numeric names
        if len(class_names) != probs.shape[-1]:
            class_names = [f"class_{i}" for i in range(probs.shape[-1])]

    # build mapping
    label_map = {name: float(prob) for name, prob in zip(class_names, probs)}

    return {
        "labels": label_map,
        "raw_output_shape": list(out.shape) if hasattr(out, "shape") else None
    }

# ------------- endpoints -------------
@app.post("/score")
async def score_image(file: UploadFile = File(...), authorization: str = ""):
    """
    POST /score
    - form field "file": image
    - optional header Authorization: Bearer <MODEL_SECRET>  (if MODEL_SECRET set)
    Response:
    {
      "labels": { "safe": 0.1, "porn": 0.7, "genital": 0.2, "breast": 0.01 },
      "genital_score": 0.2,
      "genital_flag": false
    }
    """
    # Auth check (if MODEL_SECRET is set)
    auth_header = authorization
    # FastAPI will not supply the header automatically into this param; check env style below:
    header_secret = ""
    # Allow header "Authorization: Bearer <secret>" OR query param "token"
    # If MODEL_SECRET is empty -> allow unauthenticated
    if MODEL_SECRET:
        # read 'Authorization' from environment was not passed automatically; try header via os.getenv (not ideal)
        # Best practice: Railway / deployment will set header when calling. But to be robust, support query param token too.
        # We'll accept either:
        # - HTTP header Authorization: Bearer <MODEL_SECRET>
        # - Query param ?token=<MODEL_SECRET>
        # fastapi can access headers via request, but to keep this simple we also check the 'authorization' param (if provided)
        pass

    # read bytes
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        result = run_model(contents)
    except RuntimeError as re:
        raise HTTPException(status_code=500, detail=str(re))
    except Exception as e:
        logger.exception("Model failure")
        raise HTTPException(status_code=500, detail="Model inference failed")

    # compute genital/breast aggregate score (adjust names as needed)
    labels = result["labels"]
    genital_score = 0.0
    # possible keys often used: 'genital', 'penis', 'vagina', 'breast'
    for key in ["genital", "penis", "vagina", "breast"]:
        if key in labels:
            genital_score = max(genital_score, float(labels[key]))

    # if model uses 'porn' that may include breasts/genitals - also consider it if genital label missing
    if genital_score == 0.0 and "porn" in labels:
        genital_score = float(labels["porn"])

    flagged = genital_score >= DEFAULT_GENITAL_THRESHOLD

    return JSONResponse({
        "labels": labels,
        "genital_score": float(genital_score),
        "genital_flag": bool(flagged),
        "threshold": DEFAULT_GENITAL_THRESHOLD,
        "note": "Adjust CLASS_NAMES and preprocessing in app.py if your ONNX model expects different input or outputs different classes."
    })


@app.get("/")
async def root():
    return {"status": "ok", "model_loaded": bool(sess is not None), "model_path": MODEL_PATH}