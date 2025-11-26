# model-service

FastAPI service to serve NSFW model over an HTTP endpoint.

Endpoints
- POST /score — multipart form file image; responds with {"score": 0.87}. Requires Authorization: Bearer <MODEL_API_KEY>.

Model
- Use ONNX quantized model at MODEL_PATH for best CPU inference performance.
- See convert_model.sh to convert a PyTorch checkpoint to ONNX.
