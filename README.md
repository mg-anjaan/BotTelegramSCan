Railway NSFW Moderator

Two services (bot + model) meant for Railway deployments.

- bot-service: aiogram Telegram bot. Downloads media, posts to model-service, decides action, stores offenses in Postgres, and forwards borderline images to admin review.
- model-service: FastAPI service that loads a lightweight NSFW model (ONNX or PyTorch) and exposes POST /score to return { "score": float }.

Quick deploy steps (Railway)
1. Create a GitHub repo with this project and connect it to Railway.
2. Create two services in Railway: bot-service and model-service (point each service to the corresponding folder). Use Dockerfile detection or use Railway's Docker deployment.
3. Add Railway Postgres plugin and set DATABASE_URL env var.
4. Set env vars listed in railway.example.env for each service.
5. Deploy. For bot-service set it as a worker (long-polling) process.

Notes
- Railway does not provide GPUs — use an ONNX-quantized or lightweight model. See model-service/convert_model.sh.
- Protect model-service with MODEL_API_KEY and call it from the bot with Authorization header.
- Use caching to avoid duplicate inference.
