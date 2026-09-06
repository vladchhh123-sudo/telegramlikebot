"""Реестр фоновых задач: параллельный запуск, статусы, очередь, остановка.

Каждый запуск (реакции/стории на чат) — отдельная задача со своим id, живым
статистическим объектом и статусом. Задачи одного пользователя выполняются
параллельно, но не более MAX_CONCURRENT одновременно (остальные ждут в очереди).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from html import escape
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Статусы задачи
STATE_QUEUED = "queued"      # в очереди (лимит параллельных задач исчерпан)
STATE_RUNNING = "running"    # выполняется
STATE_DONE = "done"          # успешно завершена
STATE_STOPPED = "stopped"    # остановлена пользователем
STATE_ERROR = "error"        # завершена ошибкой

_STATE_ICON = {
    STATE_QUEUED: "⏸",
    STATE_RUNNING: "▶️",
    STATE_DONE: "✅",
    STATE_STOPPED: "🛑",
    STATE_ERROR: "❌",
}
_KIND_ICON = {"react": "💬", "stories": "📸", "send": "✉️"}

# Жёсткий потолок задач в очереди на пользователя (защита от абьюза)
HARD_QUEUE_CAP = 50


def _esc(value: Any) -> str:
    return escape(str(value), quote=False)


@dataclass
class TaskInfo:
    """Публичная информация о задаче (показывается в «Моих задачах»)."""

    task_id: str
    kind: str                 # "react" | "stories" | "send"
    chat: str                 # что просил пользователь (ссылка/имя)
    detail: str               # например "реакция 👍" или "стории 👀❤️"
    account_name: str = ""    # имя аккаунта-исполнителя
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    state: str = STATE_QUEUED
    error: str = ""
    stats_obj: Any = None     # живой объект статистики (мутится сервисом)
    short_renderer: Optional[Callable[[Any], str]] = None
    _task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)

    @property
    def is_active(self) -> bool:
        return self.state in (STATE_QUEUED, STATE_RUNNING)

    @property
    def kind_icon(self) -> str:
        return _KIND_ICON.get(self.kind, "•")

    @staticmethod
    def _short(text: str, limit: int = 38) -> str:
        text = str(text)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def status_line(self) -> str:
        """Компактная запись для списка задач (HTML), в 4 аккуратные строки."""
        icon = _STATE_ICON.get(self.state, "•")
        head = f"{icon} {self.kind_icon} <code>{self.task_id}</code>"
        if self.account_name:
            head += f" · <b>{_esc(self.account_name)}</b>"

        chat = self.chat
        if self.stats_obj is not None:
            chat = getattr(self.stats_obj, "chat_title", None) or chat
        line2 = f"    💬 {_esc(self._short(chat))}"

        line3 = ""
        if self.state == STATE_QUEUED:
            line3 = "    ⏳ ждёт свободный слот"
        elif self.stats_obj is not None and self.short_renderer is not None:
            try:
                line3 = "    " + self.short_renderer(self.stats_obj)
            except Exception:
                line3 = ""

        started = time.strftime("%H:%M", time.localtime(self.started_at))
        tail = f"старт {started}"
        if self.finished_at:
            secs = max(0, int(self.finished_at - self.started_at))
            tail += f" · длилась {secs // 60} мин {secs % 60} c" if secs >= 60 else f" · длилась {secs} c"
        state_word = {
            STATE_QUEUED: "в очереди",
            STATE_RUNNING: "выполняется",
            STATE_DONE: "готово",
            STATE_STOPPED: "остановлено",
            STATE_ERROR: "ошибка",
        }.get(self.state, self.state)
        line4 = f"    <i>{state_word} · {tail}</i>"

        err = ""
        if self.state == STATE_ERROR and self.error:
            err = f"\n    ⚠️ {_esc(self._short(self.error, 80))}"

        return "\n".join(x for x in (head, line2, line3, err, line4) if x)


class TaskRegistry:
    """Хранит задачи всех пользователей: запуск, отмена, статусы, история."""

    def __init__(self, max_concurrent: int = 5, history_per_user: int = 30) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._history_limit = max(5, history_per_user)
        self._tasks: dict[int, dict[str, TaskInfo]] = {}
        self._sems: dict[int, asyncio.Semaphore] = {}
        self._counter = 0

    # ------------------------------------------------------------------ служебное

    def _sem(self, user_id: int) -> asyncio.Semaphore:
        if user_id not in self._sems:
            self._sems[user_id] = asyncio.Semaphore(self._max_concurrent)
        return self._sems[user_id]

    def _trim(self, user_id: int) -> None:
        tasks = self._tasks.get(user_id)
        if not tasks or len(tasks) <= self._history_limit:
            return
        finished = sorted(
            (t for t in tasks.values() if not t.is_active),
            key=lambda t: t.finished_at or t.started_at,
        )
        while len(tasks) > self._history_limit and finished:
            tasks.pop(finished.pop(0).task_id, None)

    # ------------------------------------------------------------------ запросы

    def active_count(self, user_id: int) -> int:
        tasks = self._tasks.get(user_id, {})
        return sum(1 for t in tasks.values() if t.is_active)

    def queue_free(self, user_id: int) -> bool:
        return self.active_count(user_id) < HARD_QUEUE_CAP

    def get(self, user_id: int, task_id: str) -> Optional[TaskInfo]:
        return self._tasks.get(user_id, {}).get(task_id)

    def list_tasks(self, user_id: int) -> list[TaskInfo]:
        """Активные (по времени старта), затем завершённые (свежие сверху)."""
        tasks = list(self._tasks.get(user_id, {}).values())
        active = sorted((t for t in tasks if t.is_active), key=lambda t: t.started_at)
        finished = sorted(
            (t for t in tasks if not t.is_active),
            key=lambda t: t.finished_at or t.started_at,
            reverse=True,
        )
        return active + finished

    # ------------------------------------------------------------------ запуск / остановка

    def start(
        self,
        user_id: int,
        coro_factory: Callable[[TaskInfo], Any],
        *,
        kind: str,
        chat: str,
        detail: str,
        account_name: str = "",
        short_renderer: Optional[Callable[[Any], str]] = None,
        stats_obj: Any = None,
    ) -> Optional[TaskInfo]:
        """Создаёт и запускает задачу. coro_factory(info) -> корутина.

        Возвращает TaskInfo или None, если достигнут жёсткий потолок очереди.
        """
        if not self.queue_free(user_id):
            return None

        self._counter += 1
        task_id = f"{kind[0]}{self._counter}"
        info = TaskInfo(
            task_id=task_id,
            kind=kind,
            chat=chat,
            detail=detail,
            account_name=account_name,
            short_renderer=short_renderer,
            stats_obj=stats_obj,
        )
        sem = self._sem(user_id)

        async def _runner() -> None:
            async with sem:
                if info.state == STATE_QUEUED:
                    info.state = STATE_RUNNING
                try:
                    await coro_factory(info)
                    if info.state == STATE_RUNNING:
                        info.state = STATE_DONE
                except asyncio.CancelledError:
                    info.state = STATE_STOPPED
                    raise
                except Exception as e:  # защита: статус не должен потеряться
                    logger.exception("Необработанная ошибка задачи %s", task_id)
                    info.state = STATE_ERROR
                    info.error = f"{type(e).__name__}: {e}"[:300]
                finally:
                    if not info.finished_at:
                        info.finished_at = time.time()

        info._task = asyncio.create_task(_runner(), name=f"task:{user_id}:{task_id}")
        self._tasks.setdefault(user_id, {})[task_id] = info
        self._trim(user_id)
        logger.info("Задача %s запущена (user=%s): %s %s", task_id, user_id, kind, chat)
        return info

    def stop(self, user_id: int, task_id: str) -> bool:
        info = self.get(user_id, task_id)
        if info is None or not info.is_active or info._task is None:
            return False
        info._task.cancel()
        logger.info("Задача %s (user=%s) отменена", task_id, user_id)
        return True

    def stop_all(self, user_id: int) -> int:
        stopped = 0
        for info in self._tasks.get(user_id, {}).values():
            if info.is_active and info._task is not None:
                info._task.cancel()
                stopped += 1
        if stopped:
            logger.info("Остановлено задач: %d (user=%s)", stopped, user_id)
        return stopped

    def clear_finished(self, user_id: int) -> int:
        tasks = self._tasks.get(user_id, {})
        done_ids = [t.task_id for t in tasks.values() if not t.is_active]
        for task_id in done_ids:
            tasks.pop(task_id, None)
        return len(done_ids)
