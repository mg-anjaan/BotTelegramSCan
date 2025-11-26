# model_loader.py - simple ONNX runtime loader
import os
import onnxruntime as ort
from PIL import Image
import numpy as np

MODEL_PATH = os.getenv("MODEL_PATH", "/app/models/nsfw_model.onnx")

class NSFWModel:
    def __init__(self, model_path=MODEL_PATH):
        providers = ["CPUExecutionProvider"]
        if not os.path.exists(model_path):
            raise RuntimeError(f"Model file not found at {model_path}. Place your ONNX model there.")
        self.sess = ort.InferenceSession(model_path, providers=providers)
        inp = self.sess.get_inputs()[0]
        self.input_name = inp.name
        self.input_shape = inp.shape

    def preprocess(self, pil: Image.Image):
        pil = pil.convert("RGB").resize((224,224))
        arr = np.array(pil).astype(np.float32) / 255.0
        mean = np.array([0.485,0.456,0.406], dtype=np.float32)
        std = np.array([0.229,0.224,0.225], dtype=np.float32)
        arr = (arr - mean) / std
        arr = arr.transpose(2,0,1)
        arr = np.expand_dims(arr, axis=0).astype(np.float32)
        return arr

    def predict(self, pil):
        x = self.preprocess(pil)
        out = self.sess.run(None, {self.input_name: x})
        s = float(out[0].ravel()[0])
        if s < 0 or s > 1:
            import math
            s = 1 / (1 + math.exp(-s))
        return s

nsfw_model = NSFWModel()
