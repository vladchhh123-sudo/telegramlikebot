"""Функция: 🚪 врата подписки.

Привязанный к каналу чат открыт для всех, но писать могут только те, кто
подал заявку в канал. Сама заявка НЕ принимается — просто висит в канале.
Логика:
- человек пишет в чат → бот удаляет сообщение и присылает текст с кнопкой;
- нажимает кнопку → подаёт заявку в канал → бот получает chat_join_request,
  запоминает человека и шлёт ему в ЛС подтверждение (на «вы»);
- с этого момента сообщения человека в чате не удаляются.

Работает чисто на токене бота (юзербот-аккаунты не участвуют).
Бот должен быть админом: в канале (право «Приглашение пользователей»)
и в чатах (право «Удаление сообщений»). Чаты задаёт владелец — это любые
отдельные группы, привязывать их к каналу не нужно.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Dict, Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import keyboards, texts
from bot.db import Database
from bot.utils import safe_edit

logger = logging.getLogger(__name__)
router = Router(name="gate")

# ключи настроек (таблица settings)
K_ENABLED = "gate_enabled"
K_CHANNEL = "gate_channel_id"
K_TITLE = "gate_channel_title"
K_URL = "gate_button_url"
K_TEXT = "gate_text"
K_DM = "gate_dm_text"
K_CHATS = "gate_chats"  # JSON: {"-100...": "Название группы", ...}

_REPEAT_SEC = 60       # повторные врата одному человеку не чаще раза в минуту
_TEMP_DELETE_SEC = 300 # сообщение-врата удаляется через 5 минут
_CACHE_TTL = 5.0       # кэш настроек
_ADMINS_TTL = 300.0    # кэш админов чата


class GateStates(StatesGroup):
    set_channel = State()
    set_chat = State()
    set_text = State()
    set_dm = State()
    set_url = State()


class GateSettings:
    def __init__(self) -> None:
        self.enabled: bool = False
        self.channel_id: Optional[int] = None
        self.title: str = ""
        self.url: str = ""
        self.chats: Dict[int, str] = {}   # чаты, где врата работают
        self.text: str = texts.GATE_MSG_DEFAULT
        self.dm: str = texts.GATE_DM_DEFAULT


_cache: Dict[str, Tuple[float, GateSettings]] = {}
_admins_cache: Dict[int, Tuple[float, frozenset]] = {}
_last_gate_msg: Dict[Tuple[int, int], Tuple[float, int, Optional[int]]] = {}


def _invalidate() -> None:
    _cache.clear()


async def _load(db: Database) -> GateSettings:
    cached = _cache.get("s")
    if cached and time.monotonic() - cached[0] < _CACHE_TTL:
        return cached[1]
    s = GateSettings()
    s.enabled = (await db.get_setting(K_ENABLED)) == "1"
    chan = await db.get_setting(K_CHANNEL)
    if chan and chan.lstrip("-").isdigit():
        s.channel_id = int(chan)
    s.title = await db.get_setting(K_TITLE) or ""
    s.url = await db.get_setting(K_URL) or ""
    try:
        raw_chats = json.loads(await db.get_setting(K_CHATS) or "{}")
        s.chats = {int(k): str(v) for k, v in raw_chats.items()}
    except Exception:
        s.chats = {}
    s.text = await db.get_setting(K_TEXT) or texts.GATE_MSG_DEFAULT
    s.dm = await db.get_setting(K_DM) or texts.GATE_DM_DEFAULT
    _cache["s"] = (time.monotonic(), s)
    return s


async def _is_group_admin(bot: Bot, group_id: int, user_id: int) -> bool:
    cached = _admins_cache.get(group_id)
    if not cached or time.monotonic() - cached[0] >= _ADMINS_TTL:
        ids: frozenset = frozenset()
        with suppress(Exception):
            admins = await bot.get_chat_administrators(group_id)
            ids = frozenset(m.user.id for m in admins)
        _admins_cache[group_id] = (time.monotonic(), ids)
        cached = _admins_cache[group_id]
    return user_id in cached[1]


def _status_screen(s: GateSettings, passes: int) -> str:
    if s.chats:
        names = [f"<b>{texts.esc(t)}</b>" for t in list(s.chats.values())[:3]]
        if len(s.chats) > 3:
            names.append(f"… +{len(s.chats) - 3}")
        chats_line = "\n         ".join(names)
    else:
        chats_line = texts.GATE_NO_CHAT
    return texts.GATE_STATUS.format(
        state="🟢 включены" if s.enabled else "🔴 выключены",
        channel=(f"<b>{texts.esc(s.title)}</b> · <code>{s.channel_id}</code>" if s.channel_id else texts.GATE_NO_CHANNEL),
        chats=chats_line,
        url=(f'<a href="{texts.esc(s.url)}">есть</a>' if s.url else "—"),
        passes=passes,
        hint=texts.GATE_HINT_ON if s.enabled else texts.GATE_HINT_OFF,
    )


@router.message(F.chat.type == "private", Command("gate_debug"))
async def gate_debug(message: Message, db: Database, bot: Bot) -> None:
    s = await _load(db)
    me = await bot.get_me()
    lines = [
        "🛠 <b>gate_debug</b>",
        f"enabled={s.enabled} channel={s.channel_id} url={'да' if s.url else 'НЕТ'}",
        f"chats={ {k: v for k, v in s.chats.items()} }",
        f"bot=@{me.username} passes={await db.gate_pass_count(s.channel_id) if s.channel_id else 0}",
    ]
    await message.answer("\n".join(texts.esc(str(x)) for x in lines))


# ---------------------------------------------------------------------- меню настройки


@router.callback_query(F.data == keyboards.CB_GATE_OPEN)
async def cb_gate_open(cb: CallbackQuery, db: Database) -> None:
    s = await _load(db)
    passes = await db.gate_pass_count(s.channel_id) if s.channel_id else 0
    await safe_edit(cb, _status_screen(s, passes), keyboards.gate_settings_kb(s.enabled))
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_GATE_TEST)
async def cb_gate_test(cb: CallbackQuery, db: Database, bot: Bot) -> None:
    s = await _load(db)
    lines = ["🧪 <b>Проверка настройки</b>"]
    lines.append("Состояние: " + ("🟢 включены" if s.enabled else "🔴 выключены"))
    if s.channel_id:
        with suppress(Exception):
            ch = await bot.get_chat(s.channel_id)
            lines.append(f"📢 Канал: ✅ {texts.esc(ch.title or str(s.channel_id))}")
    else:
        lines.append("📢 Канал: ❌ не указан")
    lines.append("🔗 Ссылка-кнопка: " + ("✅ есть" if s.url else "❌ нет — пересохраните канал или задайте вручную"))
    if not s.chats:
        lines.append("💬 Чаты: ❌ ни одного не добавлено")
    me = await bot.get_me()
    for cid, title in list(s.chats.items())[:5]:
        try:
            m = await bot.get_chat_member(cid, me.id)
            st = getattr(m, "status", "")
            if st == "administrator" and getattr(m, "can_delete_messages", False):
                lines.append(f"💬 {texts.esc(title)}: ✅ бот админ, удаление есть")
            elif st == "administrator":
                lines.append(f"💬 {texts.esc(title)}: ⚠️ бот админ, но БЕЗ права «Удаление сообщений»")
            else:
                lines.append(f"💬 {texts.esc(title)}: ❌ бот НЕ админ этой группы")
        except Exception:
            lines.append(f"💬 {texts.esc(title)}: ❌ бот не видит чат (не добавлен?)")
    passes = await db.gate_pass_count(s.channel_id) if s.channel_id else 0
    lines.append(f"✍️ Уже пропущено по заявкам: {passes}")
    if passes:
        lines.append(texts.GATE_TEST_PASSED_HINT)
    lines.append(
        "\nТест вживую: напишите сообщение в чат с аккаунта, который НЕ админ "
        "и НЕ подписан на канал — сообщение должно удалиться и появиться кнопка."
    )
    text = _status_screen(s, passes) + "\n\n" + "\n".join(lines)
    await safe_edit(cb, text, keyboards.gate_settings_kb(s.enabled))
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_GATE_RESET)
async def cb_gate_reset(cb: CallbackQuery, db: Database) -> None:
    s = await _load(db)
    n = 0
    if s.channel_id:
        n = await db.clear_gate_pass(s.channel_id)
    await safe_edit(cb, _status_screen(s, 0), keyboards.gate_settings_kb(s.enabled))
    await cb.answer(texts.GATE_RESET_DONE.format(n=n), show_alert=True)


@router.callback_query(F.data == keyboards.CB_GATE_TOGGLE)
async def cb_gate_toggle(cb: CallbackQuery, db: Database) -> None:
    s = await _load(db)
    if not s.enabled and (not s.channel_id or not s.chats):
        if not s.channel_id:
            await cb.answer(texts.GATE_NEED_CHANNEL, show_alert=True)
        else:
            await cb.answer(texts.GATE_NEED_CHAT, show_alert=True)
        return
    if not s.enabled and not s.url:
        await cb.answer(texts.GATE_NEED_URL, show_alert=True)
        return
    new_val = "0" if s.enabled else "1"
    await db.set_setting(K_ENABLED, new_val)
    _invalidate()
    s = await _load(db)
    passes = await db.gate_pass_count(s.channel_id) if s.channel_id else 0
    await safe_edit(cb, _status_screen(s, passes), keyboards.gate_settings_kb(s.enabled))
    await cb.answer(texts.GATE_ENABLED if s.enabled else texts.GATE_DISABLED)


@router.callback_query(F.data == keyboards.CB_GATE_SET_CHANNEL)
async def cb_gate_set_channel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GateStates.set_channel)
    await safe_edit(cb, texts.GATE_ASK_CHANNEL, keyboards.cancel_kb())
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_GATE_SET_CHAT)
async def cb_gate_set_chat(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GateStates.set_chat)
    await safe_edit(cb, texts.GATE_ASK_CHAT, keyboards.cancel_kb())
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_GATE_SET_TEXT)
async def cb_gate_set_text(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    s = await _load(db)
    await state.set_state(GateStates.set_text)
    ask = (
        texts.GATE_ASK_TEXT
        .replace("{current}", texts.esc(s.text))
        .replace("{link}", texts.esc(s.url or "https://t.me/ссылка_на_канал"))
    )
    await safe_edit(cb, ask, keyboards.cancel_kb())
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_GATE_SET_DM)
async def cb_gate_set_dm(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    s = await _load(db)
    await state.set_state(GateStates.set_dm)
    await safe_edit(cb, texts.GATE_ASK_DM.format(current=texts.esc(s.dm)), keyboards.cancel_kb())
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_GATE_SET_URL)
async def cb_gate_set_url(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    s = await _load(db)
    await state.set_state(GateStates.set_url)
    current = s.url if s.url else "— не задана —"
    await safe_edit(cb, texts.GATE_ASK_URL.format(current=texts.esc(current)), keyboards.cancel_kb())
    await cb.answer()


# ---------------------------------------------------------------------- ввод значений


async def _resolve_channel_input(message: Message, bot: Bot) -> Optional[Tuple[int, str]]:
    """Пересланное сообщение из канала / -100ID / @username / t.me/name."""
    fwd = message.forward_origin if hasattr(message, "forward_origin") else None
    chat_src = None
    if fwd is not None and getattr(fwd, "chat", None) is not None:
        chat_src = fwd.chat
    if chat_src is None and getattr(message, "forward_from_chat", None) is not None:
        chat_src = message.forward_from_chat
    if chat_src is not None and chat_src.type in ("channel", "supergroup", "group"):
        return int(chat_src.id), chat_src.title or ""
    raw = (message.text or "").strip()
    if raw.startswith("-100") and raw[4:].isdigit():
        with suppress(Exception):
            chat = await bot.get_chat(int(raw))
            return int(chat.id), chat.title or raw
        return int(raw), raw
    if raw.startswith("@") or "t.me/" in raw:
        handle = raw.split("t.me/")[-1].lstrip("@").split("/")[0].split("?")[0].strip()
        if handle:
            with suppress(Exception):
                chat = await bot.get_chat(f"@{handle}")
                if chat.type in ("channel", "supergroup"):
                    return int(chat.id), chat.title or f"@{handle}"
    return None


@router.message(GateStates.set_channel)
async def gate_channel_input(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    resolved = await _resolve_channel_input(message, bot)
    if resolved is None:
        await message.answer(texts.GATE_CHANNEL_BAD)
        return
    channel_id, title = resolved
    await db.set_setting(K_CHANNEL, str(channel_id))
    await db.set_setting(K_TITLE, title)
    _invalidate()
    url = (await _load(db)).url
    note = ""
    with suppress(Exception):
        link = await bot.create_chat_invite_link(channel_id, name="Gate", creates_join_request=True)
        url = link.invite_link
        await db.set_setting(K_URL, url)
        _invalidate()
    if not url:
        note = texts.GATE_CHANNEL_NO_RIGHTS
    await state.clear()
    await message.answer(
        texts.GATE_CHANNEL_SET.format(title=texts.esc(title), id=channel_id,
                                      url=f'<a href="{texts.esc(url)}">ссылка</a>' if url else "— нет —")
        + note,
        reply_markup=keyboards.gate_settings_kb(True),
    )


@router.message(GateStates.set_chat)
async def gate_chat_input(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    resolved = await _resolve_channel_input(message, bot)
    if resolved is None:
        await message.answer(texts.GATE_CHANNEL_BAD)
        return
    chat_id, title = resolved
    s = await _load(db)
    chats = dict(s.chats)
    chats[chat_id] = title or str(chat_id)
    await db.set_setting(
        K_CHATS, json.dumps({str(k): v for k, v in chats.items()}, ensure_ascii=False)
    )
    _invalidate()
    note = ""
    if chat_id < 0:  # группа: проверяем, что бот там админ
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(chat_id, me.id)
            if getattr(member, "status", "") != "administrator":
                note = texts.GATE_CHAT_NOT_ADMIN
        except Exception:
            note = texts.GATE_CHAT_NOT_ADMIN
    await state.clear()
    await message.answer(
        texts.GATE_CHAT_SET.format(title=texts.esc(title or str(chat_id)), n=len(chats)) + note,
        reply_markup=keyboards.gate_settings_kb(True),
    )


@router.message(GateStates.set_text)
async def gate_text_input(message: Message, state: FSMContext, db: Database) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(texts.GATE_URL_BAD)
        return
    await db.set_setting(K_TEXT, raw)
    _invalidate()
    await state.clear()
    await message.answer(texts.GATE_SAVED, reply_markup=keyboards.gate_settings_kb(True))


@router.message(GateStates.set_dm)
async def gate_dm_input(message: Message, state: FSMContext, db: Database) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(texts.GATE_URL_BAD)
        return
    await db.set_setting(K_DM, raw)
    _invalidate()
    await state.clear()
    await message.answer(texts.GATE_SAVED, reply_markup=keyboards.gate_settings_kb(True))


@router.message(GateStates.set_url)
async def gate_url_input(message: Message, state: FSMContext, db: Database) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("t.me/"):
        raw = "https://" + raw
    if not raw.startswith("https://t.me/"):
        await message.answer(texts.GATE_URL_BAD)
        return
    await db.set_setting(K_URL, raw)
    _invalidate()
    await state.clear()
    await message.answer(texts.GATE_SAVED, reply_markup=keyboards.gate_settings_kb(True))


# ---------------------------------------------------------------------- работа в чате


def _gate_markup(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.GATE_BUTTON_LABEL, url=url)]]
    )


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def gate_watch(message: Message, bot: Bot, db: Database) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if message.sender_chat is not None:  # посты канала / анонимные админы
        return
    if message.new_chat_members or message.left_chat_member:
        return
    s = await _load(db)
    if not s.enabled or not s.channel_id or not s.url:
        return
    # срабатываем только в выбранных чатах (любые отдельные группы, привязка не нужна)
    if message.chat.id not in s.chats:
        return
    uid = message.from_user.id
    if await _is_group_admin(bot, message.chat.id, uid):
        logger.info("🚪 %s — админ чата, пропускаю", uid)
        return
    # 1) сейчас состоит в канале (принят вручную) — пропускаем
    with suppress(Exception):
        member = await bot.get_chat_member(s.channel_id, uid)
        st = getattr(member, "status", "")
        if st in {"creator", "administrator", "member"} or (
            st == "restricted" and getattr(member, "is_member", False)
        ):
            logger.info("🚪 %s — подписчик канала, пропускаю", uid)
            return
    # 2) заявка ещё висит (не принята) — пропускаем; отписавшихся пропуск не спасёт:
    #    при выходе из канала бот снимает пропуск (см. gate_channel_member)
    if await db.has_gate_pass(s.channel_id, uid):
        logger.info("🚪 %s — заявка в канале висит, пропускаю", uid)
        return
    # не пропущен: удаляем сообщение и показываем врата (не чаще, чем раз в 5 мин)
    logger.info("🚪 %s — НЕ подписчик: удаляю сообщение и показываю врата", uid)
    try:
        await bot.delete_message(message.chat.id, message.message_id)
        logger.info("🚪 ✅ сообщение удалено (msg_id=%s)", message.message_id)
    except Exception as e:
        logger.warning("🚪 ❌ НЕ смог удалить сообщение: %s: %s", type(e).__name__, e)

    key = (message.chat.id, uid)
    now = time.monotonic()
    prev = _last_gate_msg.get(key)
    if prev and now - prev[0] < _REPEAT_SEC:
        logger.info("🚪 врата %s показывал недавно — только удалил сообщение, текст не повторяю", uid)
        return

    url = (s.url or "").strip()
    markup = None
    if url.startswith(("https://t.me/", "tg://")):
        markup = _gate_markup(url)
    else:
        logger.warning("🚪 ссылка-кнопка невалидна (%r) — шлю текст без кнопки", url[:60])
    text = s.text.replace("{link}", s.url or "")
    thread_id = getattr(message, "message_thread_id", None)  # группа с темами (форум)?
    sent = None
    sent_tid = None
    for label, txt, mk, tid in (
        ("туда же, где написал человек", text, markup, thread_id),
        ("без HTML", texts.esc(text), markup, thread_id),
        ("без темы", text, markup, None),
        ("совсем просто", texts.esc(text), None, None),
    ):
        if sent is not None:
            break
        try:
            sent = await bot.send_message(
                message.chat.id, txt, reply_markup=mk, message_thread_id=tid
            )
            sent_tid = tid
            logger.info("🚪 ✅ врата отправлены (%s, msg_id=%s)", label, sent.message_id)
        except TelegramBadRequest as e:
            logger.warning("🚪 ❌ не вышло (%s): %s", label, e)
        except Exception as e:
            logger.warning("🚪 ❌ не вышло (%s): %s: %s", label, type(e).__name__, e)

    if sent is not None:
        _last_gate_msg[key] = (now, sent.message_id, sent_tid)

        async def _cleanup() -> None:
            await asyncio.sleep(_TEMP_DELETE_SEC)
            with suppress(Exception):
                await bot.delete_message(sent.chat.id, sent.message_id)

        asyncio.create_task(_cleanup())


@router.chat_member()
async def gate_channel_member(event, db: Database) -> None:
    """Следим за каналом: отписался/выкинут — пропуск сгорает, снова через врата."""
    s = await _load(db)
    if not s.enabled or event.chat.id != s.channel_id:
        return
    new_st = getattr(event.new_chat_member, "status", "")
    if new_st in {"left", "kicked"}:
        await db.remove_gate_pass(s.channel_id, event.new_chat_member.user.id)
        logger.info(
            "🚪 %s — больше не в канале (%s), пропуск снят: снова потребуем подписку",
            event.new_chat_member.user.id, new_st,
        )


@router.chat_join_request()
async def gate_join_request(req: ChatJoinRequest, bot: Bot, db: Database) -> None:
    s = await _load(db)
    if not s.enabled or req.chat.id != s.channel_id:
        return
    is_new = await db.add_gate_pass(s.channel_id, req.from_user.id)
    if not is_new:
        return
    # заявку НЕ принимаем — она просто висит. Подтверждение — прямо в чате (не в ЛС):
    # убираем сообщение-врата и пишем короткое «можно писать», оно само удалится через 60 c
    found = None
    for (cid, uid2), (_t, mid, tid) in list(_last_gate_msg.items()):
        if uid2 == req.from_user.id:
            found = (cid, mid, tid)
    if found is None:
        logger.info("🚪 %s — заявка принята (врата не были показаны, подтверждать некуда)", req.from_user.id)
        return
    cid, gate_mid, tid = found
    with suppress(Exception):
        await bot.delete_message(cid, gate_mid)  # убрать «🚫 Чтобы писать в чат…»
    try:
        conf = await bot.send_message(cid, s.dm, message_thread_id=tid)
        logger.info("🚪 ✅ подтверждение отправлено в чат (msg_id=%s), удалю через 60 c", conf.message_id)

        async def _del_conf() -> None:
            await asyncio.sleep(60)
            with suppress(Exception):
                await bot.delete_message(conf.chat.id, conf.message_id)

        asyncio.create_task(_del_conf())
    except Exception as e:
        logger.warning("🚪 ❌ не смог отправить подтверждение в чат: %s: %s", type(e).__name__, e)
