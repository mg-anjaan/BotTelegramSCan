# Bot Service

Telegram bot that checks images/videos using model-service and removes NSFW content.

## Required Environment Variables
- BOT_TOKEN
- MODEL_API_URL (your model-service on Railway)
- MODEL_SECRET (same key as model-service)
- NSFW_THRESHOLD (default 0.7)
- OWNER_CHAT_ID (optional)
- MUTE_DAYS (default 36500)

## Features
- Auto delete NSFW images/videos
- Auto mute offender permanently
- Notify owner/admins