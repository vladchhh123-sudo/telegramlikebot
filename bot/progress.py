"""Live-прогресс фоновых задач: сообщение, которое бот периодически обновляет.

Устойчивость к лимитам Telegram: при RetryAfter выдерживает указанную паузу,
при прочих сбоях редактирования увеличивает интервал (экспоненциальный бэкофф).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
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
        interval = 2.0  # первый тик — быстро
        try:
            while not self._finished:
                await asyncio.sleep(interval)
                if self._finished:
                    break
                markup = self._running_markup() if self._running_markup else None
                interval = await self._tick(markup)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Ошибка в цикле обновления прогресса")

    async def _tick(self, markup: Optional[InlineKeyboardMarkup]) -> float:
        """Один тик обновления. Возвращает следующий интервал (сек)."""
        try:
            await self._edit(self._render(), markup)
            return self._interval
        except TelegramRetryAfter as e:
            wait = min(300.0, float(getattr(e, "retry_after", 30)) + 2)
            logger.debug("Прогресс: Telegram просит паузу %s c", wait)
            return wait
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return self._interval
            logger.debug("Не удалось обновить прогресс: %s", e)
            return min(120.0, self._interval * 2)  # бэкофф
        except Exception as e:
            logger.debug("Сбой обновления прогресса: %s", e)
            return min(120.0, self._interval * 3)

    def _render(self) -> str:
        base = self._renderer(self._stats) if self._stats is not None else "⏳ Запускаюсь…"
        if self._notes:
            base += "\n\n<b>⚠️ Последние события:</b>\n" + "\n".join(f"• {n}" for n in self._notes)
        return base

    async def _edit(self, text: str, markup: Optional[InlineKeyboardMarkup]) -> None:
        if self._finished or text == self._last_text:
            return
        await self._bot.edit_message_text(
            chat_id=self._chat_id,
            message_id=self._message_id,
            text=text,
            reply_markup=markup,
        )
        self._last_text = text

    async def finish(self, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
        """Финальный текст — редактирование с тихими повторами (чтобы итог не потерялся)."""
        self._finished = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._last_text = None
        delay = 1.0
        for _ in range(4):
            try:
                await self._edit(text, markup)
                return
            except TelegramRetryAfter as e:
                delay = min(30.0, float(getattr(e, "retry_after", 5)) + 1)
            except TelegramForbiddenError:
                return
            except Exception:
                pass
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)
