"""Функция: парсинг участников чатов с фильтрами и выгрузкой в Excel.

Если участники скрыты — собираем авторов последних N сообщений (N настраивается).
Фильтры: давность последнего визита, наличие аватарки, наличие юзернейма.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from telethon import TelegramClient, errors
from telethon.tl.types import (
    Channel,
    User,
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from bot.services import base
from bot.services.base import ChatRef, TgError, progress_line


@dataclass
class ParseFilters:
    seen_hours: int = 0        # 0 = любой; иначе не старше N часов
    photo: str = "any"         # any | yes | no
    username: str = "any"      # any | yes | no


@dataclass
class ParseStats:
    chat_title: str = "…"
    chats_total: int = 0
    chats_done: int = 0
    chats_failed: int = 0
    hidden_chats: int = 0
    raw_found: int = 0
    users_found: int = 0
    skipped_filter: int = 0
    failed: int = 0
    scan_limit: int = 500
    total_units: int = 0
    started_at: float = 0.0
    users: List[dict] = field(default_factory=list)


def render_parse_progress(s: ParseStats) -> str:
    return (
        "🕵️ <b>Парсинг участников</b>\n"
        f"Чаты: <b>{s.chats_done}/{s.chats_total}</b>"
        + (f" (сбоев: {s.chats_failed})" if s.chats_failed else "")
        + f" · текущий: <b>{s.chat_title}</b>\n"
        + (f"🙈 Скрытых чатов (по авторам сообщений): {s.hidden_chats}\n" if s.hidden_chats else "")
        + f"\n👀 Найдено записей: {s.raw_found}\n"
        f"✅ Подошло под фильтры: <b>{s.users_found}</b>\n"
        f"🚫 Отфильтровано: {s.skipped_filter}\n"
        f"⚠️ Ошибок: {s.failed}\n\n"
        f"{progress_line(s.chats_done, s.total_units, s.started_at)}"
    )


def render_parse_summary(s: ParseStats) -> str:
    return (
        "🏁 <b>Парсинг завершён!</b>\n\n"
        f"Чаты обработано: <b>{s.chats_done}/{s.chats_total}</b>"
        + (f" (сбоев: {s.chats_failed})" if s.chats_failed else "") + "\n"
        f"👀 Найдено записей: {s.raw_found}\n"
        f"✅ Подошло под фильтры: <b>{s.users_found}</b>\n"
        f"🚫 Отфильтровано: {s.skipped_filter}\n"
        f"🙈 Чатов со скрытыми участниками: {s.hidden_chats}"
    )


def short_parse(s: ParseStats) -> str:
    return f"🕵️ ✅{s.users_found} · чаты {s.chats_done}/{s.chats_total}"


def last_seen_dt(user: User) -> Optional[datetime]:
    """Примерное время последней активности (UTC) или None, если неизвестно."""
    status = getattr(user, "status", None)
    now = datetime.now(timezone.utc)
    if isinstance(status, UserStatusOnline):
        return now
    if isinstance(status, UserStatusOffline):
        return getattr(status, "last_online_date", None)
    if isinstance(status, UserStatusRecently):
        return now - timedelta(hours=24)  # «недавно»
    if isinstance(status, UserStatusLastWeek):
        return now - timedelta(days=7)
    if isinstance(status, UserStatusLastMonth):
        return now - timedelta(days=30)
    return None  # UserStatusEmpty / скрыто


def last_seen_str(user: User) -> str:
    status = getattr(user, "status", None)
    if isinstance(status, UserStatusOnline):
        return "в сети"
    dt = last_seen_dt(user)
    if dt is None:
        return "скрыто"
    delta = datetime.now(timezone.utc) - dt
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "только что"
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    if days < 30:
        return f"{days} дн назад"
    return dt.strftime("%d.%m.%Y")


def _passes_filters(user: User, f: ParseFilters) -> bool:
    if f.photo == "yes" and user.photo is None:
        return False
    if f.photo == "no" and user.photo is not None:
        return False
    if f.username == "yes" and not user.username:
        return False
    if f.username == "no" and user.username:
        return False
    if f.seen_hours > 0:
        dt = last_seen_dt(user)
        if dt is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(hours=f.seen_hours)
        if dt < cutoff:
            return False
    return True


def _make_user_record(user: User, source: str) -> dict:
    name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    return {
        "id": user.id,
        "name": name or "—",
        "username": ("@" + user.username) if user.username else "—",
        "last_seen": last_seen_str(user),
        "photo": user.photo is not None,
        "source": source,
    }


async def _scan_message_authors(
    client: TelegramClient, entity, limit: int, flood_cap: int
) -> List[User]:
    """Авторы последних сообщений (для чатов со скрытыми участниками)."""
    authors: dict[int, User] = {}
    iterator = client.iter_messages(entity, limit=limit)
    while True:
        try:
            msg = await anext(iterator)
        except StopAsyncIteration:
            break
        except (errors.FloodWaitError, errors.RPCError, ValueError):
            break
        sender = getattr(msg, "sender", None)
        if isinstance(sender, User) and not sender.bot:
            authors[sender.id] = sender
    return list(authors.values())


async def _parse_single_chat(
    client: TelegramClient,
    ref: ChatRef,
    filters: ParseFilters,
    peer_limit: Optional[int],
    scan_limit: int,
    flood_cap: int,
    progress,
    stats: ParseStats,
) -> None:
    entity = await base.resolve_chat(client, ref)
    username = getattr(entity, "username", None)
    stats.chat_title = getattr(entity, "title", None) or (f"@{username}" if username else ref.value)
    source = stats.chat_title

    users: List[User] = []
    hidden = False
    try:
        iterator = client.iter_participants(entity, limit=peer_limit or None)
        while True:
            try:
                user = await anext(iterator)
            except StopAsyncIteration:
                break
            except errors.FloodWaitError as e:
                if e.seconds + 1 > flood_cap:
                    raise TgError(
                        f"FloodWait {e.seconds} c — Telegram ограничил чтение. Запусти позже."
                    ) from e
                progress.note(f"Пауза {e.seconds} c (FloodWait)…")
                await asyncio.sleep(e.seconds + 1)
                continue
            if isinstance(user, User) and not user.bot:
                users.append(user)
                stats.raw_found += 1
    except errors.RPCError:
        hidden = True

    if hidden or not users:
        if hidden:
            stats.hidden_chats += 1
        progress.note(f"🙈 «{source}»: участники скрыты — сканирую последние {scan_limit} сообщений.")
        authors = await _scan_message_authors(client, entity, scan_limit, flood_cap)
        known = {u.id for u in users}
        for u in authors:
            if u.id not in known:
                users.append(u)
                stats.raw_found += 1

    added = 0
    for user in users:
        if _passes_filters(user, filters):
            stats.users.append(_make_user_record(user, source))
            stats.users_found += 1
            added += 1
        else:
            stats.skipped_filter += 1
    progress.note(f"«{source}»: +{added} новых по фильтрам.")


async def run_parse(
    client: TelegramClient,
    chat_refs: List[ChatRef],
    filters: ParseFilters,
    *,
    peer_limit: Optional[int],
    scan_limit: int,
    flood_cap: int,
    progress,
    stats: Optional[ParseStats] = None,
) -> ParseStats:
    """Собирает участников всех чатов (или авторов сообщений) с фильтрами.

    Результат — в stats.users (список словарей), выгрузка в Excel — на стороне хендлера.
    """
    if stats is None:
        stats = ParseStats()
    stats.chats_total = len(chat_refs)
    stats.total_units = len(chat_refs)
    stats.scan_limit = scan_limit
    progress.bind(stats)

    for idx, ref in enumerate(chat_refs, 1):
        stats.chat_title = ref.value
        try:
            await _parse_single_chat(
                client, ref, filters, peer_limit, scan_limit, flood_cap, progress, stats
            )
        except TgError as e:
            stats.chats_failed += 1
            progress.note(f"Чат {ref.value}: {e}")
        except Exception as e:
            stats.chats_failed += 1
            stats.failed += 1
            progress.note(f"Чат {ref.value}: {type(e).__name__}")
        finally:
            stats.chats_done = idx

    return stats
