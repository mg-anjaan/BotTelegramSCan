# model_loader.py
import os
import logging
from typing import Dict

from PIL import Image
import numpy as np

logger = logging.getLogger("model_loader")

MODEL_TYPE = os.getenv("MODEL_TYPE", "dummy")
MODEL_PATH = os.getenv("MODEL_PATH", "/app/model.onnx")

class DummyModel:
    """
    Simple heuristic fallback:
    - skin_ratio: fraction of pixels detected as 'skin-like' in HSV thresholds
    - genitals & breasts: coarse mapping from skin_ratio with small random-like smoothing
    - score: max(genitals, breasts) (so older bot behaviour using 'score' still works)
    This is lightweight and only intended for testing / bootstrapping.
    """
    def __init__(self):
        pass

    def _skin_ratio(self, pil_img: Image.Image) -> float:
        arr = np.array(pil_img.resize((200, 200)))  # speed
        # convert to HSV-like using RGB->HSV via normalized formula
        # We'll operate in RGB but approximate thresholds.
        r = arr[..., 0].astype(float) / 255.0
        g = arr[..., 1].astype(float) / 255.0
        b = arr[..., 2].astype(float) / 255.0

        # Simple skin detection heuristic:
        # r > 0.45 and r > g and r > b and abs(r - g) > 0.03 and (max - min) > 0.15
        mx = np.maximum(np.maximum(r, g), b)
        mn = np.minimum(np.minimum(r, g), b)
        cond = (r > 0.45) & (r > g) & (r > b) & (np.abs(r - g) > 0.03) & ((mx - mn) > 0.15)
        skin_ratio = float(np.clip(cond.mean(), 0.0, 1.0))
        return skin_ratio

    def classify(self, pil_img: Image.Image) -> Dict[str, float]:
        skin = self._skin_ratio(pil_img)
        # coarse mapping - tuned to encourage genitals if skin high and concentrated
        genitals = min(1.0, max(0.0, (skin - 0.30) * 2.0))  # amplify beyond 0.3
        breasts = min(1.0, max(0.0, (skin - 0.20) * 1.5))
        # final score used by bot is max of categories
        score = max(genitals, breasts)
        return {"score": score, "genitals": genitals, "breasts": breasts, "skin_ratio": skin}

# Try to load ONNX runtime model only if MODEL_TYPE == "onnx"
nsfw_model = None
if MODEL_TYPE == "onnx":
    try:
        import onnxruntime as ort
        logger.info("Loading ONNX model at %s", MODEL_PATH)
        sess = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
        # NOTE: real onnx model requires pre/post-processing logic specific to model.
        # We'll implement a generic wrapper that expects model to output a single float or dict in named outputs.
        class ONNXWrapper:
            def __init__(self, sess):
                self.sess = sess
                # Inspect outputs
                self.out_names = [o.name for o in sess.get_outputs()]

            def classify(self, pil_img):
                # Basic preprocessing: resize to model input if shape available
                import numpy as np
                # pick first input shape
                inp = self.sess.get_inputs()[0]
                shape = inp.shape  # e.g. [1,3,224,224] or [None,3,224,224]
                # determine target spatial size
                if len(shape) >= 4 and shape[-2] and shape[-1]:
                    h, w = int(shape[-2]), int(shape[-1])
                else:
                    h, w = 224, 224
                img = pil_img.resize((w, h)).convert("RGB")
                arr = np.array(img).astype("float32") / 255.0
                # move channel first if model expects NCHW
                if len(shape) >= 4 and shape[1] == 3:
                    arr = np.transpose(arr, (2, 0, 1))[None, ...]
                else:
                    arr = arr[None, ...]
                feed = {inp.name: arr}
                out = self.sess.run(None, feed)
                # Interpret outputs: if single scalar -> map to score; else try to parse dict-like outputs
                if len(out) == 1:
                    sc = float(out[0].ravel()[0])
                    return {"score": sc, "genitals": sc, "breasts": 0.0, "skin_ratio": 0.0}
                else:
                    # attempt map by names
                    res = {"score": 0.0, "genitals": 0.0, "breasts": 0.0, "skin_ratio": 0.0}
                    for name, val in zip(self.out_names, out):
                        v = float(val.ravel()[0]) if hasattr(val, "ravel") else float(val)
                        if "genit" in name.lower():
                            res["genitals"] = v
                        elif "breast" in name.lower():
                            res["breasts"] = v
                        elif "skin" in name.lower():
                            res["skin_ratio"] = v
                        elif "score" in name.lower() or "nsfw" in name.lower():
                            res["score"] = v
                    # fallback
                    if res["score"] == 0.0:
                        res["score"] = max(res["genitals"], res["breasts"])
                    return res
        nsfw_model = ONNXWrapper(sess)
        logger.info("ONNX model wrapper ready.")
    except Exception:
        logger.exception("Failed to load onnxruntime or ONNX model; falling back to DummyModel.")
        nsfw_model = DummyModel()
else:
    logger.info("MODEL_TYPE != 'onnx' -> using DummyModel (fast fallback)")
    nsfw_model = DummyModel()