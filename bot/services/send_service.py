"""Функция 3: отправка сообщений (текст/фото+подпись) в выбранные чаты от лица аккаунта."""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import List, Optional, Tuple

from telethon import TelegramClient, errors
from telethon.tl.types import Message

from bot.config import rand_in_range
from bot.services import base
from bot.services.base import ChatRef, TgError, progress_line


@dataclass
class SendStats:
    chat_title: str = "…"
    chats_total: int = 0
    chats_done: int = 0
    chats_failed: int = 0
    sent: int = 0
    failed: int = 0
    with_photo: bool = False
    total_units: int = 0
    started_at: float = 0.0


def render_send_progress(s: SendStats) -> str:
    return (
        "✉️ <b>Рассылка сообщений</b>\n"
        f"Чаты: <b>{s.chats_done}/{s.chats_total}</b>"
        + (f" (сбоев: {s.chats_failed})" if s.chats_failed else "")
        + f" · текущий: <b>{s.chat_title}</b>\n\n"
        f"✅ Отправлено: {s.sent}\n"
        f"⚠️ Ошибок: {s.failed}\n\n"
        f"{progress_line(s.chats_done, s.total_units, s.started_at)}"
    )


def render_send_summary(s: SendStats) -> str:
    return (
        "🏁 <b>Рассылка завершена!</b>\n\n"
        f"✅ Отправлено: <b>{s.sent}</b> из {s.chats_total}\n"
        f"⚠️ Ошибок: <b>{s.failed}</b>"
        + (f"\n🚫 Чатов недоступно: {s.chats_failed}" if s.chats_failed else "")
    )


def short_send(s: SendStats) -> str:
    return f"✉️ ✅{s.sent} · {s.chats_done}/{s.chats_total}"


async def run_sends(
    client: TelegramClient,
    chat_refs: List[ChatRef],
    *,
    text: str,
    photo_bytes: Optional[bytes],
    delay_range: Tuple[float, float],
    flood_cap: int,
    progress,
    stats: Optional[SendStats] = None,
) -> SendStats:
    """Отправляет сообщение (и/или фото с подписью) в каждый чат по порядку."""
    if stats is None:
        stats = SendStats()
    stats.chats_total = len(chat_refs)
    stats.total_units = len(chat_refs)
    if not stats.started_at:
        import time as _time

        stats.started_at = _time.time()
    stats.with_photo = photo_bytes is not None
    progress.bind(stats)

    for idx, ref in enumerate(chat_refs, 1):
        stats.chat_title = ref.value
        try:
            entity = await base.resolve_chat(client, ref)
            username = getattr(entity, "username", None)
            stats.chat_title = getattr(entity, "title", None) or (f"@{username}" if username else ref.value)

            try:
                await base.call_with_flood_retry(
                    lambda: _send_one(client, entity, text, photo_bytes),
                    cap_seconds=flood_cap,
                )
            except TgError:
                raise
            except errors.RPCError as e:
                stats.failed += 1
                if stats.failed <= 5:
                    progress.note(f"{stats.chat_title}: {base.friendly_error(e)}")
                continue
            stats.sent += 1
        except TgError as e:
            stats.chats_failed += 1
            progress.note(f"Чат {ref.value}: {e}")
        finally:
            stats.chats_done = idx
        await asyncio.sleep(rand_in_range(delay_range))

    return stats


async def _send_one(client: TelegramClient, entity, text: str, photo_bytes: Optional[bytes]) -> Message:
    if photo_bytes is not None:
        buffer = io.BytesIO(photo_bytes)
        buffer.name = "photo.jpg"
        return await client.send_file(entity, buffer, caption=text or None)
    return await client.send_message(entity, text)
