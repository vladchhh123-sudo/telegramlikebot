"""Middleware белого списка: бот отвечает только пользователям из ADMIN_IDS."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


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
