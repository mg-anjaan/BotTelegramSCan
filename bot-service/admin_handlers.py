# bot-service/admin_handlers.py
from aiogram import Router, F
from aiogram.types import Message
import os
from .db import unmute_user_record, get_offenses
from aiogram import Bot

router = Router()
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))

@router.message(commands=["unmute"])
async def cmd_unmute(message: Message, bot: Bot):
    if message.from_user.id != OWNER_CHAT_ID:
        await message.reply("Only owner can use this command.")
        return
    # Usage: /unmute <chat_id> <user_id>
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("Usage: /unmute <chat_id> <user_id>")
        return
    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        await message.reply("Chat ID and User ID must be integers.")
        return
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions=None)  # clear restrictions; None may not be allowed
    except Exception:
        # best-effort: lift restrictions to allow send messages
        from aiogram.types import ChatPermissions
        await bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=True))
    unmute_user_record(chat_id, user_id)
    await message.reply(f"User {user_id} unmuted in chat {chat_id}.")

@router.message(commands=["status"])
async def cmd_status(message: Message):
    if message.from_user.id != OWNER_CHAT_ID:
        await message.reply("Only owner can check status.")
        return
    await message.reply("Bot is running.")