"""Middleware белого списка: панель отвечает только владельцу (ADMIN_IDS).

Исключение: сообщения в группах/супергруппах и заявки на вступление в канал
(chat_join_request) от обычных пользователей проходят дальше молча — их
обрабатывают «Врата подписки» (bot/handlers/gate.py).
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message, TelegramObject, Update

logger = logging.getLogger(__name__)

_GROUP_TYPES = {"group", "supergroup"}


def _is_gate_event(event: TelegramObject) -> bool:
    """События, которые нужны «вратам», пропускаем к обработчикам без прав админа."""
    if isinstance(event, ChatJoinRequest):
        return True
    if isinstance(event, ChatMemberUpdated):  # вступил/вышел из канала
        return True
    if isinstance(event, Message):
        return event.chat is not None and event.chat.type in _GROUP_TYPES
    if isinstance(event, Update):
        if event.chat_join_request is not None or event.chat_member is not None:
            return True
        msg = event.message or event.edited_message
        return msg is not None and msg.chat is not None and msg.chat.type in _GROUP_TYPES
    return False


class AccessMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: tuple[int, ...]) -> None:
        self._admin_ids = set(admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.is_bot:
            return await handler(event, data)

        if self._admin_ids and user.id not in self._admin_ids:
            if _is_gate_event(event):
                return await handler(event, data)
            logger.info("Доступ запрещён: пользователь %s не в ADMIN_IDS.", user.id)
            if isinstance(event, Message):
                await event.answer(
                    "⛔️ Доступ закрыт. Этот бот работает только для владельца "
                    "(укажите свой Telegram ID в ADMIN_IDS в .env)."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔️ Доступ закрыт (ADMIN_IDS в .env).", show_alert=True)
            return

        return await handler(event, data)
