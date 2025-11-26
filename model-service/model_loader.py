# model-service/model_loader.py
import os
import logging
from PIL import Image
import numpy as np

logger = logging.getLogger("model_loader")
logger.setLevel(logging.INFO)

MODEL_PATH = os.getenv("MODEL_PATH", "/app/models/nsfw_model.onnx")

# Try to import onnxruntime if available
_onnx_available = False
try:
    import onnxruntime as ort
    _onnx_available = True
    logger.info("onnxruntime available for inference.")
except Exception:
    logger.info("onnxruntime not installed — using placeholder classifier. Install onnxruntime for real model.")

class NSFWModel:
    def __init__(self, model_path=None):
        self.model_path = model_path or MODEL_PATH
        self.session = None
        if _onnx_available and os.path.exists(self.model_path):
            try:
                self.session = ort.InferenceSession(self.model_path, providers=['CPUExecutionProvider'])
                logger.info("Loaded ONNX model from %s", self.model_path)
            except Exception:
                logger.exception("Failed loading ONNX model; falling back to placeholder.")
                self.session = None
        else:
            logger.info("No ONNX model found at %s — placeholder in use.", self.model_path)

    def preprocess(self, pil_image: Image.Image):
        # MODIFY this to match your ONNX model preprocessing.
        img = pil_image.resize((224, 224)).convert("RGB")
        arr = np.array(img).astype(np.float32) / 255.0
        # NHWC -> NCHW
        arr = np.transpose(arr, (2, 0, 1))
        arr = np.expand_dims(arr, 0)
        return arr

    def classify(self, pil_image: Image.Image) -> float:
        """
        Returns a float between 0 and 1 indicating NSFW probability.
        If ONNX session present, runs real inference. Otherwise returns a placeholder based on brightness.
        """
        if self.session:
            try:
                input_name = self.session.get_inputs()[0].name
                x = self.preprocess(pil_image)
                outputs = self.session.run(None, {input_name: x})
                # Adjust below depending on your model's output format.
                score = float(outputs[0].squeeze().tolist()[0]) if hasattr(outputs[0], 'squeeze') else float(outputs[0].tolist()[0])
                score = max(0.0, min(1.0, score))
                logger.debug("onnx model score=%s", score)
                return score
            except Exception:
                logger.exception("ONNX inference failed; returning placeholder score.")
        # Placeholder — mean brightness heuristic
        arr = np.array(pil_image).astype(np.float32)
        score = float(np.clip(1.0 - (arr.mean() / 255.0), 0.0, 1.0))
        return score

nsfw_model = NSFWModel()