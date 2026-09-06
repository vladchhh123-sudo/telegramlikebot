"""Функция: инвайтинг — добавление пользователей в чат/группу от лица аккаунта.

Надёжность и честность:
- Telegram МОЛЧА отклоняет инвайты (приватность/Premium) через missing_invitees —
  такие пользователи НЕ считаются добавленными и попадают в отчёт;
- телефоны резолвятся через импорт контактов (и удаляются из контактов после);
- ID резолвятся по участникам целевого чата / кэшу сессии, иначе честный отказ;
- паузы, пачки, дневной лимит, стоп при ошибках подряд и PeerFlood;
- причина каждой остановки фиксируется (stop_reason) и показывается;
- скип уже попробованных пользователей (история по чату в БД).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from telethon import TelegramClient, errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import (
    Channel,
    Chat,
    InputPhoneContact,
    InputUser,
    PeerUser,
    User,
    UserFull,
)

from bot.config import rand_in_range
from bot.services import base

logger = logging.getLogger(__name__)
from bot.services.base import TgError, normalize_invite_token, progress_line

# Столько молчаливых отклонений подряд = Telegram режет все инвайты, список жечь бессмысленно
_MAX_CONSECUTIVE_DECLINES = 20

# Ошибки Telegram → понятная причина
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


class TgErrorSkip(TgError):
    """Ошибка конкретного пользователя — идём дальше, фиксируем причину."""


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
    declined: int = 0        # молча отклонённые Telegram (приватность/Premium)
    failed: int = 0
    skipped: int = 0         # не найдены / телефоны вне Telegram
    skipped_history: int = 0 # уже были в прошлых запусках
    skipped_member: int = 0  # уже участник чата
    consecutive_errors: int = 0
    current: str = "—"
    total_units: int = 0
    started_at: float = 0.0
    stop_reason: str = ""    # почему задача закончилась раньше списка
    results: List[Tuple[str, str, str]] = field(default_factory=list)
    resolved: List[Tuple[int, int]] = field(default_factory=list)  # (id, access_hash) — в общий кэш


def render_invite_progress(s: InviteStats) -> str:
    stop = f"\n🛑 Остановка: <b>{s.stop_reason}</b>" if s.stop_reason else ""
    return (
        "📨 <b>Инвайтинг</b>\n"
        f"Чат: <b>{s.chat_title}</b>\n"
        f"Текущий: <code>{s.current}</code>\n\n"
        f"👥 Добавлено: <b>{s.invited}</b>\n"
        f"⛔️ Отклонено Telegram: {s.declined}\n"
        f"🚫 Ошибок: {s.failed}\n"
        f"⏭ Пропущено: {s.skipped} (история {s.skipped_history} · уже в чате {s.skipped_member})\n"
        f"📊 Прошло: {s.processed}/{s.total}\n\n"
        f"{progress_line(s.processed, s.total_units, s.started_at)}{stop}"
    )


def render_invite_summary(s: InviteStats) -> str:
    stop = f"\n🛑 <b>Остановлено раньше списка:</b> {s.stop_reason}" if s.stop_reason else ""
    return (
        "🏁 <b>Инвайтинг завершён!</b>\n\n"
        f"Чат: <b>{s.chat_title}</b>\n"
        f"✅ Реально добавлено: <b>{s.invited}</b> из {s.total}\n"
        f"⛔️ Отклонено Telegram молча: <b>{s.declined}</b>\n"
        f"   (приватность «кто может добавлять меня в группы» / нужен Premium у инвайтера)\n"
        f"🚫 Ошибок: {s.failed}\n"
        f"⏭ Пропущено: <b>{s.skipped + s.skipped_history + s.skipped_member}</b> "
        f"(не найдено {s.skipped} · уже пробовали раньше {s.skipped_history} · уже в чате {s.skipped_member})"
        f"{stop}\n\n"
        "📎 Подробный отчёт по каждому — в Excel-файле следующим сообщением."
    )


def short_invite(s: InviteStats) -> str:
    return f"📨 ✅{s.invited} · ⛔️{s.declined} · 🚫{s.failed} · {s.processed}/{s.total}"


def decline_reason(e: Exception) -> str:
    for cls, hint in _DECLINE_HINTS:
        if isinstance(e, cls):
            return hint
    return type(e).__name__


# ------------------------------------------------------------------ резолверы


async def _resolve_by_phone(client: TelegramClient, phone: str) -> User:
    """Телефон → пользователь через импорт контакта (стандартная практика инвайт-инструментов)."""
    contact = InputPhoneContact(client_id=0, phone=phone, first_name="inv", last_name="")
    res = await client(ImportContactsRequest(contacts=[contact]))
    users = list(getattr(res, "users", None) or [])
    if users and isinstance(users[0], User):
        return users[0]
    raise TgErrorSkip("телефон не найден в Telegram (не зарегистрирован или скрыт настройками)")


async def _delete_contact(client: TelegramClient, user: User) -> None:
    """Удаляет импортированный контакт, чтобы не засорять аккаунт."""
    try:
        await client(DeleteContactsRequest(id=[user.id]))
    except Exception:
        pass  # не критично (RPCError, ошибки резолва и т.п.)


async def _resolve_candidate(
    client: TelegramClient,
    token: str,
    known_users: Dict[int, User],
    extra_hashes: Optional[Dict[int, int]] = None,
) -> Tuple[User, bool]:
    """Токен → (пользователь, was_contact_imported). Бросает TgErrorSkip с причиной."""
    if token.startswith("@"):
        try:
            user = await client.get_entity(token)
        except ValueError:
            raise TgErrorSkip("юзернейм не найден")
        except errors.RPCError as e:
            raise TgErrorSkip(type(e).__name__)
        if not isinstance(user, User):
            raise TgErrorSkip("это не пользователь (канал/бот?)")
        return user, False

    if token.startswith("+"):
        user = await _resolve_by_phone(client, token)
        return user, True

    # числовой ID
    uid = int(token)
    known = known_users.get(uid)
    if known is not None:
        return known, False
    # общий кэш панели: access_hash запомнен при парсинге (любым аккаунтом)
    h = (extra_hashes or {}).get(uid)
    if h is not None:
        try:
            users = await client(GetUsersRequest(id=[InputUser(uid, int(h))]))
            if users and isinstance(users[0], User):
                return users[0], False
        except Exception:
            pass  # хэш устарел — пробуем остальные способы
    try:
        user = await client.get_entity(PeerUser(uid))
        return user, False
    except (ValueError, TypeError):
        pass
    except errors.RPCError as e:
        raise TgErrorSkip(f"ID недоступен ({type(e).__name__})")
    # последняя попытка: InputUser с access_hash=0 (иногда срабатывает для виденных)
    try:
        users = await client(GetUsersRequest(id=[InputUser(uid, 0)]))
        if users and isinstance(users[0], User):
            return users[0], False
    except Exception:
        pass
    raise TgErrorSkip(
        "ID не найти: Telegram не отдаёт профиль по голому ID. "
        "Сначала спарси чат-источник через 🕵️ Парсинг (бот запомнит профили), "
        "или используй @юзернейм / телефон"
    )


async def run_invites(
    client: TelegramClient,
    entity,
    tokens: List[str],
    opts: InviteOptions,
    progress,
    stats: Optional[InviteStats] = None,
    *,
    attempted: Optional[Set[str]] = None,
    known_users: Optional[Dict[int, User]] = None,
    extra_hashes: Optional[Dict[int, int]] = None,
) -> InviteStats:
    """Приглашает пользователей по списку. attempted — нормализованные токены прошлых
    запусков (скипаются), known_users — участники целевого чата (скип «уже в чате»)."""
    if stats is None:
        stats = InviteStats()
    stats.total = len(tokens)
    stats.total_units = len(tokens)
    if not stats.started_at:
        stats.started_at = time.time()
    attempted = attempted or set()
    known_users = known_users or {}

    username = getattr(entity, "username", None)
    stats.chat_title = getattr(entity, "title", None) or (f"@{username}" if username else "чат")

    is_channel = isinstance(entity, Channel) and getattr(entity, "broadcast", False)
    if is_channel:
        raise TgError(
            "это канал (только чтение) — добавлять пользователей нельзя. "
            "Нужна супергруппа или обычная группа, где аккаунт — админ с правом добавления."
        )
    is_small_chat = isinstance(entity, Chat)

    def _stop(reason: str) -> None:
        if not stats.stop_reason:
            stats.stop_reason = reason
            progress.note(f"🛑 {reason}")

    invited_in_batch = 0
    declines_row = 0
    logger.info(
        "📨 старт: всего %d · в истории %d · участников чата %d · ID с кэшем %d · скип пробованных: %s",
        len(tokens), len(attempted), len(known_users), len(extra_hashes or {}),
        "вкл" if attempted else "выкл",
    )

    for token in tokens:
        if opts.daily_limit and stats.invited >= opts.daily_limit:
            _stop(f"дневной лимит {opts.daily_limit} добавлений исчерпан (настройка «🚦 Лимит/день»)")
            break
        if stats.consecutive_errors >= opts.max_consecutive_errors:
            _stop(
                f"{stats.consecutive_errors} ошибок подряд — проверь права аккаунта и список; "
                "продолжи с этого места позже"
            )
            break

        stats.processed += 1
        stats.current = token
        if stats.processed % 100 == 0:
            logger.info(
                "📨 ход: %d/%d · ✅%d ⛔️%d 🚫%d · скип(история %d, чат %d, прочее %d)",
                stats.processed, stats.total, stats.invited, stats.declined, stats.failed,
                stats.skipped_history, stats.skipped_member, stats.skipped,
            )

        # --- скип по истории прошлых запусков ---
        key = normalize_invite_token(token)
        if key in attempted:
            stats.skipped_history += 1
            prev = ""
            stats.results.append((token, "⏭ скип", "уже пробовали в прошлом запуске"))
            continue

        # --- резолв пользователя ---
        try:
            user, was_contact = await _resolve_candidate(client, token, known_users, extra_hashes)
            u_hash = getattr(user, "access_hash", None)
            if u_hash is not None:
                stats.resolved.append((user.id, int(u_hash)))
        except TgErrorSkip as e:
            stats.skipped += 1
            stats.results.append((token, "пропущен", str(e)))
            continue
        except TgError:
            raise

        # --- уже участник целевого чата? ---
        if user.id in known_users and known_users[user.id] is not None:
            stats.skipped_member += 1
            stats.results.append((token, "⏭ скип", "уже участник чата"))
            continue

        if isinstance(user, InputUser) or not hasattr(user, "id"):
            stats.skipped += 1
            stats.results.append((token, "пропущен", "не удалось определить пользователя"))
            continue

        # --- приглашение ---
        try:
            if is_small_chat:
                result = await base.call_with_flood_retry(
                    lambda u=user: client(AddChatUserRequest(entity.id, u, fwd_limit=0)),
                    cap_seconds=opts.flood_cap,
                )
            else:
                result = await base.call_with_flood_retry(
                    lambda u=user: client(InviteToChannelRequest(entity, [u])),
                    cap_seconds=opts.flood_cap,
                )
        except TgError as e:
            # долгий FloodWait (call_with_flood_retry сдался) — пропускаем человека,
            # задача живёт; серия таких уронит её через consecutive_errors
            stats.failed += 1
            stats.consecutive_errors += 1
            stats.results.append((token, "ошибка", str(e)))
            progress.note(f"{token}: {e}")
            await asyncio.sleep(rand_in_range(opts.delay_range))
            continue
        except errors.PeerFloodError:
            stats.failed += 1
            stats.results.append((token, "ошибка", "PeerFlood — Telegram ограничил инвайты"))
            _stop("PeerFlood — Telegram ограничил инвайты. Повтори через несколько часов, уменьши темп")
            break
        except _ABORT_ERRORS as e:
            stats.failed += 1
            stats.results.append((token, "ошибка", decline_reason(e)))
            _stop(decline_reason(e))
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
        finally:
            if was_contact:
                await _delete_contact(client, user)

        # --- проверка МОЛЧАЛИВЫХ отказов Telegram (missing_invitees) ---
        declined_entry = None
        for m in getattr(result, "missing_invitees", None) or []:
            if getattr(m, "user_id", None) == user.id:
                declined_entry = m
                break
        if declined_entry is not None:
            if getattr(declined_entry, "premium_would_allow_invite", False):
                reason = "молча отклонён: приватность пользователя (добавил бы аккаунт с Premium)"
            else:
                reason = "молча отклонён: настройки приватности пользователя"
            stats.declined += 1
            declines_row += 1
            stats.results.append((token, "⛔️ отклонён", reason))
            if declines_row >= _MAX_CONSECUTIVE_DECLINES:
                stats.results.append((token, "⛔️ отклонён", reason))
                _stop(
                    f"{declines_row} отклонений подряд — Telegram массово режет инвайты "
                    "(приватность аудитории или лимит аккаунта). Продолжи позже или смени чат"
                )
                break
            if stats.declined == 1:
                progress.note(
                    "ℹ️ Telegram молча отклоняет часть инвайтов (приватность). "
                    "Они НЕ считаются добавленными — все видны в отчёте."
                )
            await asyncio.sleep(rand_in_range(opts.delay_range))
            continue

        stats.invited += 1
        stats.consecutive_errors = 0
        declines_row = 0
        stats.results.append((token, "✅ добавлен", ""))
        invited_in_batch += 1

        # --- паузы (после последнего токена не ждём) ---
        if stats.processed >= stats.total:
            break
        if opts.batch_size and invited_in_batch >= opts.batch_size:
            invited_in_batch = 0
            pause = rand_in_range(opts.batch_pause_range)
            progress.note(f"📦 Пачка из {opts.batch_size} готова — перерыв {int(pause)} c.")
            await asyncio.sleep(pause)
        else:
            await asyncio.sleep(rand_in_range(opts.delay_range))

    if not stats.stop_reason and stats.processed >= stats.total:
        stats.stop_reason = "список полностью пройден"
    logger.info(
        "📨 итог: ✅%d ⛔️%d 🚫%d · скип(история %d, чат %d, прочее %d) из %d · стоп: %s",
        stats.invited, stats.declined, stats.failed,
        stats.skipped_history, stats.skipped_member, stats.skipped,
        stats.total, stats.stop_reason or "—",
    )
    return stats
