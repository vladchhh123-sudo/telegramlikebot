"""Функция 1: реакции на сообщения — выбор аккаунтов → чаты → эмодзи → настройки → запуск.

По каждому выбранному аккаунту создаётся отдельная задача, внутри которой
обходятся все указанные чаты.
"""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import suppress
from typing import Callable, List, Optional, Set, Tuple

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import keyboards, texts
from bot.config import Config
from bot.handlers.common import fetch_logged_accounts, selected_order
from bot.progress import Progress
from bot.registry import TaskInfo, TaskRegistry
from bot.services import react_service
from bot.services.base import ChatRef, TgError, normalize_emoji, parse_chat_input_list
from bot.tg_client import ClientManager
from bot.utils import safe_delete, safe_edit

logger = logging.getLogger(__name__)
router = Router(name="reactions")
router.message.filter(F.chat.type == "private")

_MESSAGE_GAP = 1.05
MAX_EMOJIS = 25
MAX_LIMIT = 1_000_000


class ReactStates(StatesGroup):
    waiting_accounts = State()
    waiting_link = State()
    waiting_emoji = State()
    waiting_custom = State()
    waiting_limit = State()
    waiting_confirm = State()


def _chat_list_block(refs: list[ChatRef], max_lines: int = 6) -> str:
    lines = [f"  • <code>{texts.esc(ref.value)}</code>" for ref in refs[:max_lines]]
    if len(refs) > max_lines:
        lines.append(f"  … и ещё {len(refs) - max_lines}")
    return "\n".join(lines)


def _age_text(hours: int) -> str:
    return {0: "♾ всё время", 24: "последние 24 часа", 72: "последние 3 дня", 168: "последние 7 дней"}.get(
        hours, f"последние {hours} ч"
    )


# ---------------------------------------------------------------------- шаг 0: аккаунты


@router.callback_query(F.data == keyboards.CB_FLOW_REACT)
async def cb_react_start(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    if not infos:
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return
    await state.set_state(ReactStates.waiting_accounts)
    await state.update_data(
        account_ids=None, chats=None, emojis=None, limit=None, age_hours=0, include_own=False, live=False
    )
    if len(infos) == 1:
        await state.update_data(account_ids=[infos[0]["id"]])
        await state.set_state(ReactStates.waiting_link)
        await safe_edit(cb, texts.REACT_ASK_LINK, keyboards.cancel_kb())
    else:
        await safe_edit(cb, texts.ACCSEL_ASK, keyboards.accounts_multiselect_kb(infos, set()))
    await cb.answer()


@router.callback_query(ReactStates.waiting_accounts, F.data.startswith(keyboards.CB_ACCSEL_TOGGLE))
async def react_acc_toggle(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    data = await state.get_data()
    selected: Set[int] = set(data.get("account_ids") or [])
    try:
        acc_id = int((cb.data or "").removeprefix(keyboards.CB_ACCSEL_TOGGLE))
    except ValueError:
        await cb.answer()
        return
    if acc_id in selected:
        selected.remove(acc_id)
    else:
        selected.add(acc_id)
    await state.update_data(account_ids=sorted(selected))
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_reply_markup"):
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(reply_markup=keyboards.accounts_multiselect_kb(infos, selected))
    await cb.answer()


@router.callback_query(ReactStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_ALL)
async def react_acc_all(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    await state.update_data(account_ids=[i["id"] for i in infos])
    with suppress(TelegramBadRequest):
        msg = cb.message
        if msg is not None and hasattr(msg, "edit_reply_markup"):
            await msg.edit_reply_markup(
                reply_markup=keyboards.accounts_multiselect_kb(infos, {i["id"] for i in infos})
            )
    await cb.answer(texts.ACCSEL_SELECTED.format(n=len(infos)))


@router.callback_query(ReactStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_DONE)
async def react_acc_done(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    data = await state.get_data()
    selected: List[int] = list(data.get("account_ids") or [])
    if not selected:
        await cb.answer(texts.ACCSEL_NONE, show_alert=True)
        return
    await state.set_state(ReactStates.waiting_link)
    await safe_edit(cb, texts.REACT_ASK_LINK, keyboards.cancel_kb())
    await cb.answer()


# ---------------------------------------------------------------------- шаг 1: ссылки


@router.message(ReactStates.waiting_link, F.text)
async def react_link(message: Message, state: FSMContext, bot: Bot) -> None:
    refs = parse_chat_input_list(message.text)
    if not refs:
        await message.answer(texts.REACT_BAD_LINK, reply_markup=keyboards.cancel_kb())
        return
    await safe_delete(bot, message.chat.id, message.message_id)
    await state.update_data(chats=refs, emojis=None)
    await state.set_state(ReactStates.waiting_emoji)
    await message.answer(
        texts.REACT_CHOOSE_EMOJI.format(count=len(refs), chat_list=_chat_list_block(refs)),
        reply_markup=keyboards.emoji_multiselect_kb([]),
    )


# ---------------------------------------------------------------------- шаг 2: набор эмодзи


@router.callback_query(ReactStates.waiting_emoji, F.data.startswith(keyboards.CB_REACT_EMOJI))
async def react_emoji_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected: List[str] = list(data.get("emojis") or [])
    emoji = normalize_emoji((cb.data or "").removeprefix(keyboards.CB_REACT_EMOJI))
    if not emoji:
        await cb.answer(texts.REACT_CUSTOM_BAD, show_alert=True)
        return
    if emoji in selected:
        selected.remove(emoji)
    else:
        if len(selected) >= MAX_EMOJIS:
            await cb.answer(f"Максимум {MAX_EMOJIS} эмодзи", show_alert=True)
            return
        selected.append(emoji)
    await state.update_data(emojis=selected)
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_reply_markup"):
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(reply_markup=keyboards.emoji_multiselect_kb(selected))
    await cb.answer()


@router.callback_query(ReactStates.waiting_emoji, F.data == keyboards.CB_REACT_RANDOM_SET)
async def react_random_set(cb: CallbackQuery, state: FSMContext) -> None:
    """🎲 Максимальная рандомизация: добавляет 10 случайных реакций к выбору."""
    data = await state.get_data()
    selected: List[str] = list(data.get("emojis") or [])
    pool = [e for e in keyboards.PRESET_REACTIONS if e not in selected]
    add = random.sample(pool, min(10, len(pool)))
    selected = (selected + add)[:MAX_EMOJIS]
    await state.update_data(emojis=selected)
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_reply_markup"):
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(reply_markup=keyboards.emoji_multiselect_kb(selected))
    await cb.answer(f"🎲 Добавлено {len(add)} случайных реакций — всего {len(selected)}")


@router.callback_query(ReactStates.waiting_emoji, F.data == keyboards.CB_REACT_EMOJIS_DONE)
async def react_emojis_done(cb: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    data = await state.get_data()
    emojis: List[str] = list(data.get("emojis") or [])
    if not emojis:
        await cb.answer("Выбери хотя бы одну реакцию 🙂", show_alert=True)
        return
    await state.set_state(ReactStates.waiting_confirm)
    data = await state.get_data()
    text, kb = _confirm_payload(data, cfg, len(data.get("account_ids") or []))
    await safe_edit(cb, text, kb)
    await cb.answer()


@router.callback_query(ReactStates.waiting_emoji, F.data == keyboards.CB_REACT_CUSTOM)
async def react_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReactStates.waiting_custom)
    await safe_edit(cb, texts.REACT_CUSTOM_ASK, keyboards.cancel_kb())
    await cb.answer()


@router.message(ReactStates.waiting_custom, F.text)
async def react_custom_text(message: Message, state: FSMContext, bot: Bot) -> None:
    emoji = normalize_emoji(message.text)
    await safe_delete(bot, message.chat.id, message.message_id)
    data = await state.get_data()
    selected: List[str] = list(data.get("emojis") or [])
    if not emoji or len(emoji) > 12:
        await message.answer(texts.REACT_CUSTOM_BAD, reply_markup=keyboards.cancel_kb())
        return
    if emoji not in selected:
        if len(selected) >= MAX_EMOJIS:
            await message.answer(f"❌ Максимум {MAX_EMOJIS} эмодзи. Или /cancel.", reply_markup=keyboards.cancel_kb())
            return
        selected.append(emoji)
    await state.update_data(emojis=selected)
    await state.set_state(ReactStates.waiting_emoji)
    chats: list[ChatRef] = data["chats"]
    await message.answer(
        texts.REACT_CHOOSE_EMOJI.format(count=len(chats), chat_list=_chat_list_block(chats)),
        reply_markup=keyboards.emoji_multiselect_kb(selected),
    )


# ---------------------------------------------------------------------- свой лимит


@router.callback_query(ReactStates.waiting_confirm, F.data == keyboards.CB_REACT_LIMIT_CUSTOM)
async def react_limit_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReactStates.waiting_limit)
    await safe_edit(cb, texts.LIMIT_ASK, keyboards.cancel_kb())
    await cb.answer()


@router.message(ReactStates.waiting_limit, F.text)
async def react_limit_text(message: Message, state: FSMContext, cfg: Config, bot: Bot) -> None:
    raw = (message.text or "").strip().replace(" ", "")
    await safe_delete(bot, message.chat.id, message.message_id)
    try:
        limit = int(raw)
    except ValueError:
        await message.answer(texts.LIMIT_BAD, reply_markup=keyboards.cancel_kb())
        return
    if limit < 0 or limit > MAX_LIMIT:
        await message.answer(texts.LIMIT_BAD, reply_markup=keyboards.cancel_kb())
        return
    await state.update_data(limit=limit)
    await state.set_state(ReactStates.waiting_confirm)
    data = await state.get_data()
    text, kb = _confirm_payload(data, cfg, len(data.get("account_ids") or []))
    await message.answer(text, reply_markup=kb)


# ---------------------------------------------------------------------- подтверждение


def _confirm_payload(data: dict, cfg: Config, accounts_n: int) -> Tuple[str, InlineKeyboardMarkup]:
    chats: list[ChatRef] = data["chats"]
    emojis: List[str] = data["emojis"]
    limit = int(data.get("limit") if data.get("limit") is not None else cfg.default_messages_limit)
    age_hours = int(data.get("age_hours") or 0)
    text = texts.REACT_CONFIRM.format(
        accounts=accounts_n,
        count=len(chats),
        chat_list=_chat_list_block(chats) + "\n",
        emojis=react_service.emojis_label(emojis),
        limit="♾ без лимита" if limit == 0 else f"{limit}",
        age=_age_text(age_hours),
        own="✅ лайкать" if data.get("include_own") else "❌ пропускать",
        live="⚡️ ВКЛ" if data.get("live") else "выкл",
        delay_lo=cfg.reaction_delay[0],
        delay_hi=cfg.reaction_delay[1],
    )
    kb = keyboards.react_confirm_kb(
        emojis=emojis,
        include_own=bool(data.get("include_own")),
        live=bool(data.get("live")),
        limit=limit,
        age_hours=age_hours,
    )
    return text, kb


async def _show_confirm(cb: CallbackQuery, state: FSMContext, cfg: Config, accounts_n: int) -> None:
    text, kb = _confirm_payload(await state.get_data(), cfg, accounts_n)
    await safe_edit(cb, text, kb)


@router.callback_query(ReactStates.waiting_confirm, F.data.startswith(keyboards.CB_REACT_LIMIT))
async def react_limit(cb: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    try:
        limit = int((cb.data or "").removeprefix(keyboards.CB_REACT_LIMIT))
    except ValueError:
        limit = cfg.default_messages_limit
    await state.update_data(limit=max(0, min(limit, MAX_LIMIT)))
    data = await state.get_data()
    await _show_confirm(cb, state, cfg, len(data.get("account_ids") or []))
    await cb.answer()


@router.callback_query(ReactStates.waiting_confirm, F.data == keyboards.CB_REACT_AGE)
async def react_age_cycle(cb: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    data = await state.get_data()
    current = int(data.get("age_hours") or 0)
    try:
        nxt = keyboards.AGE_CHOICES[(keyboards.AGE_CHOICES.index(current) + 1) % len(keyboards.AGE_CHOICES)]
    except ValueError:
        nxt = 0
    await state.update_data(age_hours=nxt)
    await _show_confirm(cb, state, cfg, len(data.get("account_ids") or []))
    await cb.answer()


@router.callback_query(ReactStates.waiting_confirm, F.data == keyboards.CB_REACT_OWN)
async def react_own_toggle(cb: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    data = await state.get_data()
    await state.update_data(include_own=not data.get("include_own", False))
    await _show_confirm(cb, state, cfg, len(data.get("account_ids") or []))
    await cb.answer()


@router.callback_query(ReactStates.waiting_confirm, F.data == keyboards.CB_REACT_LIVE)
async def react_live_toggle(cb: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    data = await state.get_data()
    await state.update_data(live=not data.get("live", False))
    await _show_confirm(cb, state, cfg, len(data.get("account_ids") or []))
    await cb.answer()


# ---------------------------------------------------------------------- запуск


def _make_runner(
    *,
    bot: Bot,
    client,
    account_name: str,
    refs: List[ChatRef],
    emojis: List[str],
    include_own: bool,
    live: bool,
    max_age_hours: int,
    limit: Optional[int],
    cfg: Config,
    stats: react_service.ReactStats,
    chat_id: int,
    message_id: int,
) -> Callable[[TaskInfo], "asyncio.Future"]:
    """Фабрика корутины: один аккаунт = одна задача (все чаты внутри)."""

    async def runner(info: TaskInfo) -> None:
        progress = Progress(
            bot,
            chat_id,
            message_id,
            react_service.render_react_progress,
            cfg.progress_interval,
            running_markup=lambda: keyboards.running_kb(info.task_id),
        )
        progress.bind(stats)
        progress.start()  # запускаем live-обновление сообщения
        with suppress(TelegramBadRequest):
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=texts.TASK_STARTED,
                reply_markup=keyboards.running_kb(info.task_id),
            )
        try:
            await react_service.run_reactions(
                client,
                refs,
                emojis,
                include_own=include_own,
                live=live,
                max_age_hours=max_age_hours,
                limit=limit,
                delay_range=cfg.reaction_delay,
                flood_cap=cfg.flood_wait_cap,
                progress=progress,
                stats=stats,
            )
            await progress.finish(react_service.render_react_summary(stats))
        except asyncio.CancelledError:
            with suppress(Exception):
                await progress.finish(
                    "🛑 <b>Остановлено.</b>\n\n" + react_service.render_react_summary(stats)
                )
            raise
        except TgError as e:
            info.state = "error"
            info.error = str(e)
            with suppress(Exception):
                await progress.finish(f"❌ {e}")
        except Exception as e:
            logger.exception("Задача реакций упала с неожиданной ошибкой")
            info.state = "error"
            info.error = f"{type(e).__name__}: {e}"[:300]
            with suppress(Exception):
                await progress.finish(texts.TASK_ERROR.format(err=texts.esc(e)))

    return runner


@router.callback_query(ReactStates.waiting_confirm, F.data == keyboards.CB_REACT_RUN)
async def react_run(
    cb: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    cfg: Config,
    manager: ClientManager,
    registry: TaskRegistry,
) -> None:
    uid = cb.from_user.id
    if not manager.is_logged_in(uid):
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return

    data = await state.get_data()
    await state.clear()
    infos = await fetch_logged_accounts(manager, uid)
    chosen = selected_order(infos, set(data.get("account_ids") or []))
    if not chosen:
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return
    chats: list[ChatRef] = data["chats"]
    emojis: List[str] = list(data["emojis"])
    include_own: bool = bool(data.get("include_own"))
    live: bool = bool(data.get("live"))
    max_age_hours: int = int(data.get("age_hours") or 0)
    raw_limit = int(data.get("limit") if data.get("limit") is not None else cfg.default_messages_limit)
    limit: Optional[int] = None if raw_limit == 0 else max(1, raw_limit)
    await cb.answer()

    # 1) Сообщение-прогресс для каждого аккаунта
    placeholders: list[tuple[int, int]] = []
    for i, info in enumerate(chosen):
        label = f"⏳ Готовлю задачу: <i>{texts.esc(info['name'])}</i>"
        if i == 0:
            msg = await safe_edit(cb, label)
            if msg is not None:
                placeholders.append((msg.chat.id, msg.message_id))
                continue
        await asyncio.sleep(_MESSAGE_GAP)
        msg = await bot.send_message(uid, label)
        placeholders.append((msg.chat.id, msg.message_id))

    # 2) По аккаунту — отдельная задача (внутри все чаты)
    launched = 0
    for info, (chat_id, message_id) in zip(chosen, placeholders):
        client = manager.get(uid, info["id"])
        if client is None:
            continue
        stats = react_service.ReactStats(
            emojis=list(emojis), limit=limit or 0, live=live, chats_total=len(chats)
        )
        task_info = registry.start(
            uid,
            _make_runner(
                bot=bot,
                client=client,
                account_name=info["name"],
                refs=list(chats),
                emojis=list(emojis),
                include_own=include_own,
                live=live,
                max_age_hours=max_age_hours,
                limit=limit,
                cfg=cfg,
                stats=stats,
                chat_id=chat_id,
                message_id=message_id,
            ),
            kind="react",
            chat=f"{len(chats)} чатов",
            detail=react_service.emojis_label(emojis) + (" ⚡️" if live else ""),
            account_name=info["name"],
            short_renderer=react_service.short_react,
            stats_obj=stats,
        )
        if task_info is None:
            with suppress(Exception):
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=texts.TASKS_TOO_MANY,
                    reply_markup=keyboards.back_to_menu_kb(),
                )
            break
        launched += 1

    if launched and len(chosen) > cfg.max_concurrent_tasks:
        with suppress(Exception):
            await bot.send_message(uid, texts.TASK_QUEUED_NOTE)


@router.message(ReactStates.waiting_link)
@router.message(ReactStates.waiting_custom)
@router.message(ReactStates.waiting_limit)
async def react_fallback(message: Message) -> None:
    await message.answer(texts.NEED_TEXT, reply_markup=keyboards.cancel_kb())
