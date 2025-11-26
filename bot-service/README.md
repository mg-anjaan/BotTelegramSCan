# bot-service

This service runs the Telegram bot (aiogram). It uses Postgres for persistence and calls the model-service to get NSFW score.

Key env vars
See railway.example.env.

Start (Docker)
Railway will build the Dockerfile. Locally you can run:

# set envs then
python bot.py

Behavior summary
- On receiving media: downloads to temp file, computes MD5 cache key, if cached result found -> reuse
- Else POST to NSFW model /score with Authorization: Bearer <MODEL_API_KEY> and multipart/form-data file
- Based on score: delete/warn/mute or forward to admin review
- Admin review: inline Approve/Delete+Ban buttons handled by admin_handlers.py
