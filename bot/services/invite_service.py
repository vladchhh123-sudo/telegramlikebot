"""Функция: инвайтинг — добавление пользователей в чат/группу от лица аккаунта.

Безопасность и надёжность:
- пауза между приглашениями (настраиваемая), пачками с длинным перерывом;
- дневной лимит, стоп при N ошибок подряд, остановка по FloodWait-потолку;
- каждый отказ фиксируется с причиной в отчёт (Excel) и в прогресс.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import Channel, Chat, InputUser, UserFull

from bot.config import rand_in_range
from bot.services import base
from bot.services.base import TgError, progress_line

# Ошибки Telegram → понятная причина (проверено: все классы существуют)
_DECLINE_HINTS = (
    (errors.UserPrivacyRestrictedError, "настройки приватности не разрешают добавлять"),
    (errors.UserNotMutualContactError, "нет взаимного контакта (нужен mutual contact)"),
    (errors.UserChannelsTooMuchError, "у пользователя лимит каналов/групп"),
    (errors.UserAlreadyParticipantError, "уже участник чата"),
    (errors.UserKickedError, "заблокирован в этом чате"),
    (errors.InputUserDeactivatedError, "аккаунт пользователя удалён"),
    (errors.UserDeactivatedBanError, "аккаунт пользователя забанен"),
    (errors.UserBotError, "это бот — не добавляю"),
    (errors.UserBlockedError, "пользователь заблокировал аккаунт-исполнитель"),
)

# Ошибки, при которых продолжать бессмысленно
_ABORT_ERRORS = (
    errors.ChatAdminRequiredError,
    errors.ChatWriteForbiddenError,
    errors.ChannelPrivateError,
)


@dataclass
class InviteOptions:
    delay_range: Tuple[float, float] = (30.0, 60.0)
    batch_size: int = 5            # 0 = без пачек
    batch_pause_range: Tuple[float, float] = (120.0, 240.0)
    daily_limit: int = 50          # 0 = ∞
    flood_cap: int = 300
    max_consecutive_errors: int = 8


@dataclass
class InviteStats:
    chat_title: str = "…"
    total: int = 0
    processed: int = 0
    invited: int = 0
    failed: int = 0
    skipped: int = 0
    consecutive_errors: int = 0
    current: str = "—"
    total_units: int = 0
    started_at: float = 0.0
    results: List[Tuple[str, str, str]] = field(default_factory=list)  # (токен, статус, причина)


def render_invite_progress(s: InviteStats) -> str:
    return (
        "📨 <b>Инвайтинг</b>\n"
        f"Чат: <b>{s.chat_title}</b>\n"
        f"Текущий: <code>{s.current}</code>\n\n"
        f"👥 Добавлено: <b>{s.invited}</b>\n"
        f"🚫 Не удалось: {s.failed} · ⏭ Пропущено: {s.skipped}\n"
        f"📊 Прошло: {s.processed}/{s.total}\n"
        f"🔥 Ошибок подряд: {s.consecutive_errors}\n\n"
        f"{progress_line(s.processed, s.total_units, s.started_at)}"
    )


def render_invite_summary(s: InviteStats) -> str:
    return (
        "🏁 <b>Инвайтинг завершён!</b>\n\n"
        f"Чат: <b>{s.chat_title}</b>\n"
        f"✅ Добавлено: <b>{s.invited}</b> из {s.total}\n"
        f"🚫 Не удалось: {s.failed}\n"
        f"⏭ Пропущено (уже были/не найдены): {s.skipped}\n\n"
        "📎 Подробный отчёт — в Excel-файле следующим сообщением."
    )


def short_invite(s: InviteStats) -> str:
    return f"📨 ✅{s.invited} · 🚫{s.failed} · {s.processed}/{s.total}"


def decline_reason(e: Exception) -> str:
    for cls, hint in _DECLINE_HINTS:
        if isinstance(e, cls):
            return hint
    return type(e).__name__


async def _resolve_candidate(client: TelegramClient, token: str):
    """Токен → Telethon-пользователь. Бросает TgError с понятной причиной."""
    try:
        if token.startswith("@"):
            return await client.get_entity(token)
        if token.startswith("+"):
            return await client.get_entity(token)  # телефон (если это контакт аккаунта)
        return await client.get_entity(int(token))  # числовой id
    except ValueError:
        raise TgErrorSkip("не найден / нет доступа к профилю")
    except errors.PhoneNumberInvalidError:
        raise TgErrorSkip("некорректный номер/идентификатор")
    except errors.FloodWaitError:
        raise
    except errors.RPCError as e:
        raise TgErrorSkip(type(e).__name__)


class TgErrorSkip(TgError):
    """Ошибка конкретного пользователя — идём дальше, фиксируем причину."""


async def run_invites(
    client: TelegramClient,
    entity,
    tokens: List[str],
    opts: InviteOptions,
    progress,
    stats: Optional[InviteStats] = None,
) -> InviteStats:
    """Приглашает пользователей по списку. Возвращает статистику + результаты для отчёта."""
    if stats is None:
        stats = InviteStats()
    stats.total = len(tokens)
    stats.total_units = len(tokens)

    username = getattr(entity, "username", None)
    stats.chat_title = getattr(entity, "title", None) or (f"@{username}" if username else "чат")

    is_channel = isinstance(entity, Channel) and getattr(entity, "broadcast", False)
    if is_channel:
        raise TgError(
            "это канал (только чтение) — добавлять пользователей нельзя. "
            "Нужна супергруппа или обычная группа, где аккаунт — админ с правом добавления."
        )
    is_small_chat = isinstance(entity, Chat)

    stats.started_at = stats.started_at or 0.0
    import time as _time

    if not stats.started_at:
        stats.started_at = _time.time()

    invited_in_batch = 0

    for token in tokens:
        if opts.daily_limit and stats.invited >= opts.daily_limit:
            progress.note(f"🚦 Дневной лимит ({opts.daily_limit}) достигнут — останавливаюсь.")
            break
        if stats.consecutive_errors >= opts.max_consecutive_errors:
            progress.note(
                f"🛑 {stats.consecutive_errors} ошибок подряд — стоп. "
                "Проверь права аккаунта и список; продолжишь с этого места в следующий раз."
            )
            break

        stats.current = token
        stats.processed += 1

        # --- резолв пользователя ---
        try:
            user = await _resolve_candidate(client, token)
        except TgErrorSkip as e:
            stats.skipped += 1
            stats.results.append((token, "пропущен", str(e)))
            continue
        except TgError:
            raise

        if isinstance(user, InputUser) or not hasattr(user, "id"):
            stats.skipped += 1
            stats.results.append((token, "пропущен", "не удалось определить пользователя"))
            continue

        # --- приглашение ---
        try:
            if is_small_chat:
                await base.call_with_flood_retry(
                    lambda u=user: client(AddChatUserRequest(entity.id, u, fwd_limit=0)),
                    cap_seconds=opts.flood_cap,
                )
            else:
                await base.call_with_flood_retry(
                    lambda u=user: client(InviteToChannelRequest(entity, [u])),
                    cap_seconds=opts.flood_cap,
                )
        except errors.FloodWaitError:
            raise
        except errors.PeerFloodError:
            stats.failed += 1
            stats.consecutive_errors += 1
            stats.results.append((token, "ошибка", "PeerFlood — Telegram ограничил инвайты"))
            progress.note("⛔️ PeerFlood: Telegram просит большой перерыв. Останавливаюсь — повтори через часы.")
            break
        except _ABORT_ERRORS as e:
            stats.failed += 1
            stats.results.append((token, "ошибка", decline_reason(e)))
            progress.note(f"⛔️ {decline_reason(e)} — задача остановлена.")
            break
        except errors.RPCError as e:
            reason = decline_reason(e)
            stats.failed += 1
            stats.consecutive_errors += 1
            stats.results.append((token, "ошибка", reason))
            if stats.failed <= 10:
                progress.note(f"{token}: {reason}")
            await asyncio.sleep(rand_in_range(opts.delay_range))
            continue
        except ValueError as e:
            stats.failed += 1
            stats.consecutive_errors += 1
            stats.results.append((token, "ошибка", f"не найден ({type(e).__name__})"))
            continue

        stats.invited += 1
        stats.consecutive_errors = 0
        stats.results.append((token, "✅ добавлен", ""))
        invited_in_batch += 1

        # --- паузы ---
        if opts.batch_size and invited_in_batch >= opts.batch_size:
            invited_in_batch = 0
            pause = rand_in_range(opts.batch_pause_range)
            progress.note(f"📦 Пачка из {opts.batch_size} готова — перерыв {int(pause)} c.")
            await asyncio.sleep(pause)
        else:
            await asyncio.sleep(rand_in_range(opts.delay_range))

    return stats
