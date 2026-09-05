"""Функция 1: постановка реакций на сообщения чатов от лица загруженных аккаунтов.

- несколько чатов в одной задаче (обход по порядку);
- набор эмодзи: на каждое сообщение случайная из выбранных;
- фильтр свежести: только сообщения не старше N часов;
- ⚡️ авто-режим: после прогона реагировать на новые сообщения до остановки.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from telethon import TelegramClient, errors, events
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import Message, ReactionEmoji

from bot.config import rand_in_range
from bot.services import base
from bot.services.base import ChatRef, TgError, progress_line


@dataclass
class ReactStats:
    chat_title: str = "…"
    emojis: List[str] = field(default_factory=lambda: ["👍"])
    limit: int = 0          # 0 = без лимита
    live: bool = False
    chats_total: int = 0
    chats_done: int = 0
    chats_failed: int = 0
    seen: int = 0
    reacted: int = 0
    skipped_own: int = 0
    skipped_already: int = 0
    skipped_old: int = 0
    skipped_other: int = 0
    failed: int = 0
    total_units: int = 0
    started_at: float = 0.0


def emojis_label(emojis: Sequence[str]) -> str:
    return "".join(emojis)


def render_react_progress(s: ReactStats) -> str:
    pct = f" ({s.seen * 100 // s.limit}%)" if s.limit else ""
    mode = " · ⚡️ авто-режим" if s.live else ""
    return (
        "💬 <b>Реакции на сообщения</b>\n"
        f"Чаты: <b>{s.chats_done}/{s.chats_total}</b>"
        + (f" (сбоев: {s.chats_failed})" if s.chats_failed else "")
        + f" · текущий: <b>{s.chat_title}</b>\n"
        f"Реакции: <b>{emojis_label(s.emojis)}</b> (случайно) · Лимит: {s.limit if s.limit else '∞'}{mode}\n\n"
        f"📄 Проверено сообщений: {s.seen}{pct}\n"
        f"✅ Поставлено реакций: {s.reacted}\n"
        f"⏭ Пропущено: свои {s.skipped_own} · уже с реакцией {s.skipped_already}"
        f" · старые {s.skipped_old} · прочее {s.skipped_other}\n"
        f"⚠️ Ошибок: {s.failed}\n\n"
        f"{progress_line(s.seen, s.total_units, s.started_at)}"
    )


def render_react_summary(s: ReactStats) -> str:
    mode = "\n⚡️ Авто-режим остановлен." if s.live else ""
    return (
        "🏁 <b>Готово!</b>\n"
        f"Чаты обработано: <b>{s.chats_done}/{s.chats_total}</b>"
        + (f" (сбоев: {s.chats_failed})" if s.chats_failed else "") + "\n"
        f"Реакции: <b>{emojis_label(s.emojis)}</b>\n\n"
        f"✅ Поставлено реакций: <b>{s.reacted}</b>\n"
        f"📄 Проверено сообщений: {s.seen} (лимит на чат: {s.limit if s.limit else '∞'})\n"
        f"⏭ Пропущено: свои {s.skipped_own} · уже с реакцией {s.skipped_already}"
        f" · старые {s.skipped_old} · прочее {s.skipped_other}\n"
        f"⚠️ Ошибок: {s.failed}{mode}"
    )


def short_react(s: ReactStats) -> str:
    """Короткая строка статуса для списка задач."""
    live = " ⚡️" if s.live else ""
    return f"{emojis_label(s.emojis)} ✅{s.reacted} · 📄{s.seen} · {s.chats_done}/{s.chats_total}{live}"


def _has_our_reaction(msg: Message, emojis: Sequence[str]) -> bool:
    """True, если наш аккаунт УЖЕ ставил любую из выбранных реакций (chosen_order — только свои)."""
    reactions = msg.reactions
    if reactions is None or not reactions.results:
        return False
    ours = set(emojis)
    for rc in reactions.results:
        if rc.chosen_order is not None and getattr(rc.reaction, "emoticon", None) in ours:
            return True
    return False


async def _react_single_chat(
    client: TelegramClient,
    chat_ref: ChatRef,
    emojis: List[str],
    *,
    include_own: bool,
    max_age_hours: int,
    limit: Optional[int],
    delay_range: Tuple[float, float],
    flood_cap: int,
    progress,
    stats: ReactStats,
):
    """Один чат: resolve → фильтры → реакции. Возвращает entity (для авто-режима) или None."""
    entity = await base.resolve_chat(client, chat_ref)

    allowed = await base.get_allowed_reactions(client, entity)
    if allowed == frozenset():
        raise TgError("реакции в этом чате запрещены настройками")

    username = getattr(entity, "username", None)
    stats.chat_title = getattr(entity, "title", None) or (f"@{username}" if username else chat_ref.value)

    usable = list(emojis)
    if isinstance(allowed, frozenset) and allowed:
        filtered = [e for e in usable if e in allowed]
        if not filtered:
            filtered = [sorted(allowed)[0]]
            progress.note(f"В «{stats.chat_title}» выбранные реакции недоступны — использую {filtered[0]}.")
        elif len(filtered) < len(usable):
            dropped = "".join(e for e in usable if e not in allowed)
            progress.note(f"В «{stats.chat_title}» реакции {dropped} недоступны — исключены.")
        usable = filtered

    cutoff: Optional[datetime] = None
    if max_age_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    async def _react(msg: Message) -> None:
        emoji = pick_emoji(usable)
        try:
            await base.call_with_flood_retry(
                lambda: client(
                    SendReactionRequest(peer=entity, msg_id=msg.id, reaction=[ReactionEmoji(emoji)])
                ),
                cap_seconds=flood_cap,
            )
        except TgError:
            raise
        except errors.RPCError as e:
            stats.failed += 1
            if stats.failed <= 5:
                progress.note(base.friendly_error(e))
            return
        stats.reacted += 1

    iterator = client.iter_messages(entity, limit=limit or None)
    while True:
        try:
            msg = await anext(iterator)
        except StopAsyncIteration:
            break
        except errors.FloodWaitError as e:
            if e.seconds + 1 > flood_cap:
                raise TgError(
                    f"FloodWait {e.seconds} c — Telegram ограничил чтение. Запусти задачу позже."
                ) from e
            progress.note(f"Пауза на чтение {e.seconds} c (FloodWait)…")
            await asyncio.sleep(e.seconds + 1)
            continue

        stats.seen += 1

        if not isinstance(msg, Message) or getattr(msg, "action", None) is not None:
            stats.skipped_other += 1
            continue
        if msg.out and not include_own:
            stats.skipped_own += 1
            continue
        if _has_our_reaction(msg, usable):
            stats.skipped_already += 1
            continue
        if cutoff is not None and msg.date is not None and msg.date < cutoff:
            stats.skipped_old += 1
            break  # сообщения идут от новых к старым — дальше только старше

        await _react(msg)
        await asyncio.sleep(rand_in_range(delay_range))

    return entity, usable


async def run_reactions(
    client: TelegramClient,
    chat_refs: List[ChatRef],
    emojis: List[str],
    *,
    include_own: bool = False,
    live: bool = False,
    max_age_hours: int = 0,
    limit: Optional[int],
    delay_range: Tuple[float, float],
    flood_cap: int,
    progress,
    stats: Optional[ReactStats] = None,
) -> ReactStats:
    """Обходит все чаты по порядку и ставит реакции. limit=None/0 — без лимита на чат."""
    if stats is None:
        stats = ReactStats()
    stats.emojis = list(emojis)
    stats.limit = limit or 0
    stats.live = live
    stats.chats_total = len(chat_refs)
    stats.total_units = (limit or 0) * len(chat_refs)  # 0 = точный объём неизвестен (∞)
    if not stats.started_at:
        stats.started_at = time.time()
    progress.bind(stats)

    # ---------------------------------------------------------------- основной проход
    progress.note("Читаю сообщения чата…")

    # Случайный выбор эмодзи без повторов подряд (выглядит по-человечески)
    last_pick: List[str] = []

    def pick_emoji(pool: List[str]) -> str:
        options = pool
        if last_pick and len(pool) > 1:
            options = [e for e in pool if e != last_pick[0]] or pool
        choice = random.choice(options)
        last_pick.clear()
        last_pick.append(choice)
        return choice

    live_entities = []
    live_emojis: set = set()
    for idx, ref in enumerate(chat_refs, 1):
        stats.chat_title = ref.value
        try:
            entity, usable = await _react_single_chat(
                client,
                ref,
                emojis,
                include_own=include_own,
                max_age_hours=max_age_hours,
                limit=limit,
                delay_range=delay_range,
                flood_cap=flood_cap,
                progress=progress,
                stats=stats,
                pick_emoji=pick_emoji,
            )
            if entity is not None and live:
                live_entities.append(entity)
                live_emojis.update(usable)
        except TgError as e:
            stats.chats_failed += 1
            progress.note(f"Чат {ref.value}: {e}")
        finally:
            stats.chats_done = idx

    if not live:
        return stats

    # ---------------------------------------------------------------- ⚡️ авто-режим
    if not live_entities:
        progress.note("⚡️ Авто-режим не запущен: ни один чат не обработан.")
        return stats

    progress.note("⚡️ Авто-режим: реагирую на новые сообщения. Останови задачу кнопкой ⏹.")

    async def _make_handler(entity):
        pool = list(live_emojis) or list(emojis)

        async def _on_new_message(event) -> None:
            msg = event.message
            if not isinstance(msg, Message) or getattr(msg, "action", None) is not None:
                return
            if msg.out and not include_own:
                return
            if _has_our_reaction(msg, emojis):
                return
            stats.seen += 1
            try:
                await base.call_with_flood_retry(
                    lambda: client(
                        SendReactionRequest(
                            peer=entity, msg_id=msg.id, reaction=[ReactionEmoji(pick_emoji(pool))]
                        )
                    ),
                    cap_seconds=flood_cap,
                )
                stats.reacted += 1
            except TgError:
                pass  # FloodWait дольше лимита — игнорируем в авто-режиме
            except errors.RPCError:
                stats.failed += 1
            await asyncio.sleep(rand_in_range(delay_range))

        return _on_new_message

    handlers = []
    for entity in live_entities:
        handler = await _make_handler(entity)
        client.add_event_handler(handler, events.NewMessage(chats=entity))
        handlers.append(handler)

    try:
        await asyncio.Event().wait()  # до остановки задачи
    finally:
        for handler, entity in zip(handlers, live_entities):
            client.remove_event_handler(handler)
    return stats
