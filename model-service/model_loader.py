# model-service/model_loader.py
# Minimal stubbed model loader.
# Replace this with the real model loading & inference code later.
import random
from PIL import Image

class StubModel:
    def classify(self, pil_image: Image.Image) -> float:
        """
        Dummy classifier: returns a pseudo-random NSFW score between 0.0 and 1.0.
        This is only for testing/deploying. Replace with real model code.
        """
        # Optionally use image size to slightly vary
        w, h = pil_image.size
        seed = (w + h) % 100
        random.seed(seed)
        return random.uniform(0.0, 1.0)

nsfw_model = StubModel()