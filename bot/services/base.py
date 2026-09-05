"""Общие утилиты: разбор ссылок, вступление в закрытый чат, ограничения реакций, FloodWait."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union

from telethon import TelegramClient, errors
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    GetFullChatRequest,
    ImportChatInviteRequest,
)
from telethon.tl.types import (
    Chat,
    Channel,
    ChatInviteAlready,
    ChatInvitePeek,
    ChatReactionsAll,
    ChatReactionsNone,
    ChatReactionsSome,
    PeerChannel,
    ReactionEmoji,
)


class TgError(Exception):
    """Ошибка выполнения задачи, текст которой показывается пользователю."""


@dataclass(frozen=True)
class ChatRef:
    """Разобранная ссылка на чат: kind = invite | username | id."""

    kind: str
    value: str


_INVITE_RE = re.compile(
    r"(?:t(?:elegram)?\.(?:me|dog)/(?:\+|joinchat/))([A-Za-z0-9_-]{10,})",
    re.IGNORECASE,
)
_TME_USERNAME_RE = re.compile(
    r"^(?:https?://)?t(?:elegram)?\.(?:me|dog)/@?([A-Za-z0-9_]{4,64})/?$",
    re.IGNORECASE,
)
_USERNAME_RE = re.compile(r"^@?([A-Za-z0-9_]{4,64})$")
_INT_RE = re.compile(r"^-?\d{4,}$")


def parse_chat_input(text: str) -> Optional[ChatRef]:
    """Разбирает ввод пользователя: ссылка-приглашение / t.me-ссылка / @юзернейм / id."""
    text = (text or "").strip()
    m = _INVITE_RE.search(text)
    if m:
        return ChatRef("invite", m.group(1))
    m = _TME_USERNAME_RE.match(text)
    if m:
        return ChatRef("username", m.group(1))
    if _INT_RE.match(text):
        return ChatRef("id", text)
    m = _USERNAME_RE.match(text)
    if m:
        return ChatRef("username", m.group(1))
    return None


def parse_chat_input_list(text: str) -> list[ChatRef]:
    """Разбирает НЕСКОЛЬКО ссылок из одного сообщения (разделители: новая строка, пробел, запятая).

    Дубликаты отбрасываются, порядок сохраняется.
    """
    refs: list[ChatRef] = []
    seen: set[tuple[str, str]] = set()
    for raw in re.split(r"[\n\r,;]+", text or ""):
        raw = raw.strip()
        if not raw:
            continue
        ref = parse_chat_input(raw)
        if ref is None:
            continue
        key = (ref.kind, ref.value.lower())
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs


_MESSAGE_LINK_C_RE = re.compile(
    r"^(?:https?://)?t(?:elegram)?\.(?:me|dog)/c/(\d{4,})/(\d{1,})/?$",
    re.IGNORECASE,
)
_MESSAGE_LINK_PUBLIC_RE = re.compile(
    r"^(?:https?://)?t(?:elegram)?\.(?:me|dog)/@?([A-Za-z0-9_]{4,64})/(\d{1,})/?$",
    re.IGNORECASE,
)


def parse_message_link(text: str) -> Optional[tuple[ChatRef, int]]:
    """Разбирает ссылку на СООБЩЕНИЕ: t.me/юзернейм/12345 или t.me/c/2226319014/12345."""
    text = (text or "").strip()
    m = _MESSAGE_LINK_C_RE.match(text)
    if m:
        return ChatRef("id", m.group(1)), int(m.group(2))
    m = _MESSAGE_LINK_PUBLIC_RE.match(text)
    if m:
        return ChatRef("username", m.group(1)), int(m.group(2))
    return None


def chat_ref_from_tg_id(chat_id: int) -> ChatRef:
    """ChatRef из id чата, как его отдаёт Bot API (для пересланных сообщений).

    -1001234567890 (Bot API) → PeerChannel(1234567890); обычные группы — отрицательный id.
    """
    if chat_id < 0:
        s = str(-chat_id)
        if s.startswith("100") and len(s) > 10:
            return ChatRef("id", s[3:])
        return ChatRef("id", str(chat_id))
    return ChatRef("id", str(chat_id))


async def resolve_chat(client: TelegramClient, ref: ChatRef):
    """Превращает ChatRef в сущность чата; при необходимости вступает в закрытый чат."""
    try:
        if ref.kind == "invite":
            invite = await client(CheckChatInviteRequest(ref.value))
            if isinstance(invite, (ChatInviteAlready, ChatInvitePeek)):
                return invite.chat
            # ChatInvite — нужно вступить
            updates = await client(ImportChatInviteRequest(ref.value))
            chats = list(getattr(updates, "chats", None) or [])
            if not chats:
                raise TgError("Вступил в чат, но не смог определить его. Пришли id или @юзернейм.")
            return chats[0]

        if ref.kind == "id":
            value = int(ref.value)
            if value > 0:
                # Голый id канала — пробуем как PeerChannel, затем как user id
                try:
                    return await client.get_entity(PeerChannel(value))
                except (ValueError, errors.ChannelPrivateError):
                    return await client.get_entity(value)
            return await client.get_entity(value)

        # username
        return await client.get_entity(ref.value)
    except TgError:
        raise
    except errors.InviteHashExpiredError as e:
        raise TgError("Ссылка-приглашение истекла или была отозвана.") from e
    except errors.InviteHashInvalidError as e:
        raise TgError("Ссылка-приглашение недействительна.") from e
    except errors.ChannelPrivateError as e:
        raise TgError("Приватный чат недоступен для этого аккаунта.") from e
    except errors.FloodWaitError as e:
        raise TgError(f"Telegram просит подождать {e.seconds + 1} c. Повтори попытку позже.") from e
    except (ValueError, TypeError) as e:
        raise TgError(
            "Не удалось найти чат. Проверь ссылку и права аккаунта "
            "(для закрытых чатов аккаунт должен быть участником)."
        ) from e
    except errors.RPCError as e:
        raise TgError(f"Telegram вернул ошибку: <b>{type(e).__name__}</b>") from e


async def get_allowed_reactions(client: TelegramClient, entity) -> Union[str, frozenset]:
    """Возвращает 'all', frozenset() (реакции запрещены) или frozenset разрешённых эмодзи."""
    try:
        if isinstance(entity, Channel):
            full = await client(GetFullChannelRequest(entity))
            raw = full.full_chat.available_reactions
        elif isinstance(entity, Chat):
            full = await client(GetFullChatRequest(entity.id))
            raw = full.full_chat.available_reactions
        else:
            return "all"
    except errors.RPCError:
        return "all"

    if raw is None or isinstance(raw, ChatReactionsAll):
        return "all"
    if isinstance(raw, ChatReactionsNone):
        return frozenset()
    if isinstance(raw, ChatReactionsSome):
        return frozenset(r.emoticon for r in raw.reactions if isinstance(r, ReactionEmoji))
    return "all"


def normalize_emoji(text: str) -> str:
    """Приводит эмодзи к каноничному виду реакций Telegram (без VS16 и пробелов)."""
    return (text or "").strip().replace("\ufe0f", "").replace(" ", "")


async def call_with_flood_retry(
    factory: Callable[[], Awaitable],
    cap_seconds: int,
    max_retries: int = 5,
):
    """Выполняет запрос, автоматически пережидая FloodWaitError.

    Если Telegram просит ждать дольше cap_seconds — бросает TgError.
    """
    for attempt in range(max_retries + 1):
        try:
            return await factory()
        except errors.FloodWaitError as e:
            if e.seconds + 1 > cap_seconds or attempt == max_retries:
                raise TgError(
                    f"FloodWait {e.seconds} c — Telegram ограничил активность аккаунта. "
                    "Запусти задачу позже."
                ) from e
            await asyncio.sleep(e.seconds + 1)


_ERROR_HINTS = (
    (errors.ReactionInvalidError, "эта реакция недоступна в данном чате"),
    (errors.ReactionsTooManyError, "слишком много разных реакций в чате"),
    (errors.PremiumAccountRequiredError, "нужна Telegram Premium"),
    (errors.UserBannedInChannelError, "аккаунт ограничен в этом чате"),
    (errors.ChatWriteForbiddenError, "аккаунту запрещено писать в этом чате"),
    (errors.ChatAdminRequiredError, "нет прав на это действие"),
    (errors.ChannelPrivateError, "приватный чат недоступен"),
    (errors.SlowModeWaitError, "включён slow mode"),
)


def friendly_error(e: Exception) -> str:
    """Короткое понятное описание ошибки для заметок в прогрессе."""
    for cls, hint in _ERROR_HINTS:
        if isinstance(e, cls):
            return f"{type(e).__name__}: {hint}"
    return f"{type(e).__name__}"


# ------------------------------------------------------------------ ETA-прогресс


def eta_seconds(processed: int, total: int, started_at: float) -> Optional[float]:
    """Оценка оставшегося времени (сек) или None, если оценить нельзя."""
    import time as _time

    if total <= 0 or processed <= 0 or started_at <= 0:
        return None
    elapsed = max(0.0, _time.time() - started_at)
    if processed >= total:
        return 0.0
    return (elapsed / processed) * (total - processed)


def fmt_eta(seconds: Optional[float]) -> str:
    """Человекочитаемая строка прогресса: «Прогресс: 12/50 (24%) · осталось ≈ 4 мин 30 с»."""
    return str(seconds)  # заглушка не используется: см. progress_line


def progress_line(processed: int, total: int, started_at: float) -> str:
    """Строка «⏳ Прогресс: X/Y (P%) · осталось ≈ T» для любых задач."""
    if total > 0:
        pct = min(100, processed * 100 // total)
        if processed >= total:
            return f"⏳ Прогресс: <b>{processed}/{total}</b> ({pct}%) · завершается…"
        eta = eta_seconds(processed, total, started_at)
        if eta is not None:
            m, s = divmod(int(eta) + 59, 60)
            h, m = divmod(m, 60)
            left = (f"{h} ч " if h else "") + (f"{m} мин " if m else "") + f"{s} с"
            return f"⏳ Прогресс: <b>{processed}/{total}</b> ({pct}%) · осталось ≈ {left}"
        return f"⏳ Прогресс: <b>{processed}/{total}</b> ({pct}%)"
    return f"⏳ Обработано: <b>{processed}</b>"


# ------------------------------------------------------------------ токены для инвайтинга


def parse_invite_token(text: str) -> Optional[str]:
    """Распознаёт пользователя: @юзернейм / t.me/юзернейм / t.me/+инвайт-нет / id / +телефон.

    Возвращает нормализованный токен или None.
    """
    text = (text or "").strip()
    if not text:
        return None
    m = re.match(r"^(?:https?://)?t(?:elegram)?\.(?:me|dog)/@?([A-Za-z][A-Za-z0-9_]{4,31})/?$", text, re.IGNORECASE)
    if m:
        return "@" + m.group(1)
    if re.match(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$", text):
        return text
    if re.match(r"^\d{5,}$", text):
        return text
    if re.match(r"^\+?\d{7,15}$", text):
        return text if text.startswith("+") else "+" + text
    return None
