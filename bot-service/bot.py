#!/usr/bin/env python3
"""
bot.py
Telegram bot (python-telegram-bot v20+) that:
- watches group messages for photos / image files / videos
- forwards the file to external model-service (/score) with Bearer MODEL_SECRET
- if returned score >= threshold: deletes the message, mutes the sender (long mute), notifies admins/owner
- otherwise leaves message intact
Environment variables:
- BOT_TOKEN (required)
- MODEL_API_URL (required) e.g. https://ramscan-production.up.railway.app
- MODEL_SECRET (required) e.g. mgPROTECT12345
- NSFW_THRESHOLD (optional, default 0.70)
- OWNER_CHAT_ID (optional) - telegram id to notify; if missing, notifies chat admins
- MUTE_DAYS (optional, default 36500 ~ 100 years)
"""
import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

import httpx
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nsfw-bot")

# -------- env / defaults ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODEL_API_URL = os.getenv("MODEL_API_URL")  # e.g. https://ramscan-production.up.railway.app
MODEL_SECRET = os.getenv("MODEL_SECRET")  # e.g. mgPROTECT12345
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.7"))
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")  # option: notify this id
MUTE_DAYS = int(os.getenv("MUTE_DAYS", "36500"))  # default ~100 years

if not BOT_TOKEN or not MODEL_API_URL or not MODEL_SECRET:
    logger.critical("Missing required environment variables (BOT_TOKEN, MODEL_API_URL, MODEL_SECRET). Exiting.")