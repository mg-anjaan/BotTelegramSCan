# admin_handlers.py - handles inline callbacks for admin review
from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import AsyncSessionLocal, Offense, Cache
import os

router = Router()

@router.callback_query(lambda c: c.data and c.data.startswith('approve:'))
async def approve_cb(callback: types.CallbackQuery):
    key = callback.data.split(':',1)[1]
    await callback.answer('Approved — no action taken')
    await callback.message.edit_reply_markup(reply_markup=None)

@router.callback_query(lambda c: c.data and c.data.startswith('delete:'))
async def delete_cb(callback: types.CallbackQuery):
    # callback.data format: delete:<key>:<user_id>
    try:
        _, key, user_id = callback.data.split(':',2)
    except:
        await callback.answer('Invalid data')
        return
    # Here you can lookup message id or stored info and take action.
    await callback.answer('Deleted and user will be banned (admin action simulated)')
    await callback.message.edit_reply_markup(reply_markup=None)
