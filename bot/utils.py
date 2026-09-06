"""Мелкие общие помощники для обработчиков."""
from __future__ import annotations

import logging
from typing import Optional

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)


async def safe_edit(cb: CallbackQuery, text: str, reply_markup=None) -> Optional[Message]:
    """Редактирует сообщение из callback. Возвращает объект сообщения или None."""
    msg = cb.message
    if msg is None or not hasattr(msg, "edit_text"):
        return None
    try:
        return await msg.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return msg
        logger.debug("Не удалось отредактировать сообщение: %s", e)
        return None


async def safe_delete(bot, chat_id: int, message_id: int) -> None:
    """Пытается удалить сообщение, молча игнорируя неудачу."""
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        logger.debug("Не удалось удалить сообщение %s/%s", chat_id, message_id, exc_info=True)
