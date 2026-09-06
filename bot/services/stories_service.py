"""Функция 2: истории участников чатов — просмотр и лайки.

- несколько чатов в одной задаче;
- если список участников скрыт/недоступен — фолбэк: собираем АВТОРОВ последних
  сообщений чата (у них тоже бывают активные истории) + сам чат (если канал/юзер).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional, Tuple

from telethon import TelegramClient, errors
from telethon.tl.functions.stories import GetPeerStoriesRequest, ReadStoriesRequest
from telethon.tl.functions.stories import SendReactionRequest as SendStoryReactionRequest
from telethon.tl.types import Channel, Message, ReactionEmoji, User

from bot.config import rand_in_range
from bot.services import base
from bot.services.base import ChatRef, TgError, progress_line

_FALLBACK_SCAN = 400  # сколько последних сообщений сканировать при скрытых участниках


@dataclass
class StoriesStats:
    chat_title: str = "…"
    emoji: str = "❤️"
    like: bool = True
    peer_limit: int = 0      # 0 = без лимита
    chats_total: int = 0
    chats_done: int = 0
    chats_failed: int = 0
    hidden_chats: int = 0
    peers_seen: int = 0
    peers_with_stories: int = 0
    stories_found: int = 0
    viewed: int = 0
    liked: int = 0
    skipped_already: int = 0
    failed: int = 0
    total_units: int = 0
    started_at: float = 0.0


def render_stories_progress(s: StoriesStats) -> str:
    mode = "👀❤️ смотреть и лайкать" if s.like else "👀 только смотреть"
    peer_part = f"/{s.peer_limit}" if s.peer_limit else ""
    hidden = f" · 🙈 скрытых: {s.hidden_chats}" if s.hidden_chats else ""
    return (
        "📸 <b>Стории участников</b>\n"
        f"Чаты: <b>{s.chats_done}/{s.chats_total}</b>"
        + (f" (сбоев: {s.chats_failed})" if s.chats_failed else "")
        + f" · текущий: <b>{s.chat_title}</b>\n"
        f"Режим: <b>{mode}</b> · Лимит участников: {s.peer_limit if s.peer_limit else '∞'}{hidden}\n\n"
        f"👥 Проверено участников: {s.peers_seen}{peer_part}\n"
        f"🟣 С активными историями: {s.peers_with_stories} (найдено сторий: {s.stories_found})\n"
        f"👀 Просмотрено сторий: {s.viewed}\n"
        + (f"❤️ Лайкнуто сторий: {s.liked} · уже были с лайком: {s.skipped_already}\n" if s.like else "")
        + f"⚠️ Ошибок: {s.failed}\n\n"
        + progress_line(s.peers_seen, s.total_units, s.started_at)
    )


def render_stories_summary(s: StoriesStats) -> str:
    mode = "смотреть + лайкать" if s.like else "только смотреть"
    lines = [
        "🏁 <b>Готово!</b>",
        f"Чаты обработано: <b>{s.chats_done}/{s.chats_total}</b>"
        + (f" (сбоев: {s.chats_failed})" if s.chats_failed else ""),
        f"Режим: <b>{mode}</b>",
        "",
        f"👥 Проверено участников: <b>{s.peers_seen}</b>",
        f"🟣 Участников с историями: <b>{s.peers_with_stories}</b> (найдено {s.stories_found})",
        f"👀 Просмотрено сторий: <b>{s.viewed}</b>",
    ]
    if s.like:
        lines.append(f"❤️ Лайкнуто сторий: <b>{s.liked}</b> · уже были с лайком: {s.skipped_already}")
    if s.hidden_chats:
        lines.append(f"🙈 Чатов со скрытыми участниками (фолбэк по авторам сообщений): {s.hidden_chats}")
    lines.append(f"⚠️ Ошибок: {s.failed}")
    return "\n".join(lines)


def short_stories(s: StoriesStats) -> str:
    parts = [f"👥{s.peers_seen}", f"👀{s.viewed}"]
    if s.like:
        parts.append(f"❤️{s.liked}")
    parts.append(f"{s.chats_done}/{s.chats_total}")
    return " · ".join(parts)


async def _scan_message_authors(
    client: TelegramClient, entity, limit: int, flood_cap: int
) -> List[User]:
    """Фолбэк для чатов со скрытыми участниками: авторы последних сообщений."""
    authors: dict[int, User] = {}
    iterator = client.iter_messages(entity, limit=limit)
    while True:
        try:
            msg = await anext(iterator)
        except StopAsyncIteration:
            break
        except errors.FloodWaitError as e:
            if e.seconds + 1 > flood_cap:
                break
            await asyncio.sleep(e.seconds + 1)
            continue
        except errors.RPCError:
            break
        sender = getattr(msg, "sender", None)
        if isinstance(sender, User):
            authors[sender.id] = sender
    return list(authors.values())


async def _collect_targets(
    client: TelegramClient,
    entity,
    peer_limit: Optional[int],
    flood_cap: int,
    progress,
    stats: StoriesStats,
) -> List:
    """Цели: сам чат + участники; при скрытых участниках — авторы последних сообщений."""
    targets: List = []
    if isinstance(entity, (Channel, User)):
        targets.append(entity)  # у самого канала/пользователя тоже могут быть истории

    users: List[User] = []
    participants_hidden = False
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
                        f"FloodWait {e.seconds} c — Telegram ограничил чтение участников. Запусти позже."
                    ) from e
                await asyncio.sleep(e.seconds + 1)
                continue
            if isinstance(user, User):
                users.append(user)
    except errors.RPCError:
        participants_hidden = True

    if participants_hidden or (not users):
        if participants_hidden:
            stats.hidden_chats += 1
        progress.note("🙈 Участники скрыты/недоступны — собираю авторов последних сообщений.")
        scan_limit = min(_FALLBACK_SCAN, peer_limit or _FALLBACK_SCAN)
        users.extend(u for u in await _scan_message_authors(client, entity, scan_limit, flood_cap) if u not in users)
    else:
        # расширим список авторами свежих сообщений — в больших чатах список участников неполный
        extra = await _scan_message_authors(client, entity, min(200, peer_limit or 200), flood_cap)
        users.extend(u for u in extra if u not in users)

    targets.extend(users)
    return targets


async def _stories_single_chat(
    client: TelegramClient,
    chat_ref: ChatRef,
    *,
    like: bool,
    emoji: str,
    peer_limit: Optional[int],
    story_delay: Tuple[float, float],
    peer_delay: Tuple[float, float],
    flood_cap: int,
    progress,
    stats: StoriesStats,
) -> None:
    entity = await base.resolve_chat(client, chat_ref)
    username = getattr(entity, "username", None)
    stats.chat_title = getattr(entity, "title", None) or (f"@{username}" if username else chat_ref.value)

    targets = await _collect_targets(client, entity, peer_limit, flood_cap, progress, stats)
    if not targets:
        raise TgError("ни участников, ни авторов сообщений получить не удалось")
    # точный объём работы известен только после сбора целей:
    remaining_chats = max(1, stats.chats_total - stats.chats_done + 1)
    if stats.total_units <= 0:
        stats.total_units = min(len(targets) * remaining_chats, peer_limit or len(targets) * remaining_chats)

    for target in targets:
        if peer_limit and stats.peers_seen >= peer_limit:
            progress.note(f"Достигнут лимит участников ({peer_limit}).")
            return
        stats.peers_seen += 1
        await asyncio.sleep(rand_in_range(peer_delay))

        try:
            res = await base.call_with_flood_retry(
                lambda t=target: client(GetPeerStoriesRequest(peer=t)),
                cap_seconds=flood_cap,
            )
        except TgError:
            raise
        except ValueError:
            # нет access_hash у автора сообщения (min-сущность) — пропускаем тихо
            stats.failed += 1
            continue
        except errors.RPCError as e:
            stats.failed += 1
            if stats.failed <= 5:
                progress.note(f"{base.friendly_error(e)} — пропуск участника")
            continue

        peer_stories = getattr(res, "stories", None)
        items = list(getattr(peer_stories, "stories", None) or []) if peer_stories is not None else []
        if not items:
            continue
        stats.peers_with_stories += 1
        stats.stories_found += len(items)

        max_id = max(item.id for item in items)
        try:
            await base.call_with_flood_retry(
                lambda t=target, mx=max_id: client(ReadStoriesRequest(peer=t, max_id=mx)),
                cap_seconds=flood_cap,
            )
            stats.viewed += len(items)
        except TgError:
            raise
        except (errors.RPCError, ValueError) as e:
            stats.failed += 1
            progress.note(base.friendly_error(e))

        if not like:
            continue

        for item in items:
            if getattr(item, "sent_reaction", None) is not None:
                stats.skipped_already += 1
                continue
            try:
                await base.call_with_flood_retry(
                    lambda t=target, sid=item.id: client(
                        SendStoryReactionRequest(peer=t, story_id=sid, reaction=ReactionEmoji(emoji))
                    ),
                    cap_seconds=flood_cap,
                )
            except TgError:
                raise
            except (errors.RPCError, ValueError) as e:
                stats.failed += 1
                if stats.failed <= 5:
                    progress.note(base.friendly_error(e))
                continue
            stats.liked += 1
            await asyncio.sleep(rand_in_range(story_delay))


async def run_stories(
    client: TelegramClient,
    chat_refs: List[ChatRef],
    *,
    like: bool = True,
    emoji: str = "❤️",
    peer_limit: Optional[int],
    story_delay: Tuple[float, float],
    peer_delay: Tuple[float, float],
    flood_cap: int,
    progress,
    stats: Optional[StoriesStats] = None,
) -> StoriesStats:
    """Обходит все чаты: участники (или авторы сообщений, если участники скрыты) →
    просмотр историй (stories.readStories) + лайки (stories.sendReaction)."""
    if stats is None:
        stats = StoriesStats()
    stats.like = like
    stats.emoji = emoji
    stats.peer_limit = peer_limit or 0
    stats.chats_total = len(chat_refs)
    if not stats.started_at:
        import time as _time

        stats.started_at = _time.time()
    progress.bind(stats)

    for idx, ref in enumerate(chat_refs, 1):
        stats.chat_title = ref.value
        try:
            await _stories_single_chat(
                client,
                ref,
                like=like,
                emoji=emoji,
                peer_limit=peer_limit,
                story_delay=story_delay,
                peer_delay=peer_delay,
                flood_cap=flood_cap,
                progress=progress,
                stats=stats,
            )
        except TgError as e:
            stats.chats_failed += 1
            progress.note(f"Чат {ref.value}: {e}")
        finally:
            stats.chats_done = idx

    return stats
