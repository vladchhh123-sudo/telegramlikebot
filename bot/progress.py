"""Live-прогресс фоновых задач: сообщение, которое бот периодически обновляет."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)


class Progress:
    """Периодически редактирует сообщение, показывая статистику из объекта-статистики."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message_id: int,
        renderer: Callable[[Any], str],
        interval: float = 8.0,
        running_markup: Optional[Callable[[], Optional[InlineKeyboardMarkup]]] = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._renderer = renderer
        self._interval = interval
        self._running_markup = running_markup
        self._stats: Any = None
        self._notes: deque[str] = deque(maxlen=3)
        self._finished = False
        self._last_text: Optional[str] = None
        self._loop_task: Optional[asyncio.Task] = None

    def bind(self, stats: Any) -> None:
        self._stats = stats

    def note(self, text: str) -> None:
        """Добавить служебную заметку (последние 3 показываются в прогрессе)."""
        self._notes.append(text)

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            first = True
            while not self._finished:
                # первый тик — быстро (чтобы сразу увидеть рамку прогресса), дальше по интервалу
                await asyncio.sleep(2.0 if first else self._interval)
                first = False
                markup = self._running_markup() if self._running_markup else None
                await self._edit(self._render(), markup)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Ошибка в цикле обновления прогресса")

    def _render(self) -> str:
        base = self._renderer(self._stats) if self._stats is not None else "⏳ Запускаюсь…"
        if self._notes:
            base += "\n\n<b>⚠️ Последние события:</b>\n" + "\n".join(f"• {n}" for n in self._notes)
        return base

    async def _edit(self, text: str, markup: Optional[InlineKeyboardMarkup]) -> None:
        if self._finished or text == self._last_text:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
                reply_markup=markup,
            )
            self._last_text = text
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():
                logger.debug("Не удалось обновить прогресс: %s", e)
        except TelegramForbiddenError:
            self._finished = True
            logger.info("Чат %s стал недоступен — прогресс остановлен", self._chat_id)

    async def finish(self, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
        """Финальный текст (по умолчанию — с кнопкой возврата в меню)."""
        self._finished = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        await self._edit(text, markup)
