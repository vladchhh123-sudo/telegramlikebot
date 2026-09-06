"""Функция 2: истории участников — выбор аккаунтов → чаты → лимит/режим → запуск.

Работает даже в чатах со скрытыми участниками: фолбэк на авторов последних сообщений.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Callable, List, Optional, Set

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts
from bot.config import Config
from bot.handlers.common import fetch_logged_accounts, selected_order
from bot.progress import Progress
from bot.registry import TaskInfo, TaskRegistry
from bot.services import stories_service
from bot.services.base import ChatRef, TgError, parse_chat_input_list
from bot.tg_client import ClientManager
from bot.utils import safe_delete, safe_edit

logger = logging.getLogger(__name__)
router = Router(name="stories")
router.message.filter(F.chat.type == "private")

_MESSAGE_GAP = 1.05
MAX_PEERS = 1_000_000


class StoriesStates(StatesGroup):
    waiting_accounts = State()
    waiting_link = State()
    waiting_mode = State()
    waiting_custom_peers = State()


def _chat_list_block(refs: list[ChatRef], max_lines: int = 6) -> str:
    lines = [f"  • <code>{texts.esc(ref.value)}</code>" for ref in refs[:max_lines]]
    if len(refs) > max_lines:
        lines.append(f"  … и ещё {len(refs) - max_lines}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- шаг 0: аккаунты


@router.callback_query(F.data == keyboards.CB_FLOW_STORIES)
async def cb_stories_start(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    if not infos:
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return
    await state.set_state(StoriesStates.waiting_accounts)
    await state.update_data(account_ids=None, chats=None, peer_limit=None)
    if len(infos) == 1:
        await state.update_data(account_ids=[infos[0]["id"]])
        await state.set_state(StoriesStates.waiting_link)
        await safe_edit(cb, texts.STORIES_ASK_LINK, keyboards.cancel_kb())
    else:
        await safe_edit(cb, texts.ACCSEL_ASK, keyboards.accounts_multiselect_kb(infos, set()))
    await cb.answer()


@router.callback_query(StoriesStates.waiting_accounts, F.data.startswith(keyboards.CB_ACCSEL_TOGGLE))
async def stories_acc_toggle(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
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


@router.callback_query(StoriesStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_ALL)
async def stories_acc_all(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    await state.update_data(account_ids=[i["id"] for i in infos])
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_reply_markup"):
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(
                reply_markup=keyboards.accounts_multiselect_kb(infos, {i["id"] for i in infos})
            )
    await cb.answer(texts.ACCSEL_SELECTED.format(n=len(infos)))


@router.callback_query(StoriesStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_DONE)
async def stories_acc_done(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    data = await state.get_data()
    if not data.get("account_ids"):
        await cb.answer(texts.ACCSEL_NONE, show_alert=True)
        return
    await state.set_state(StoriesStates.waiting_link)
    await safe_edit(cb, texts.STORIES_ASK_LINK, keyboards.cancel_kb())
    await cb.answer()


# ---------------------------------------------------------------------- шаг 1: ссылки


@router.message(StoriesStates.waiting_link, F.text)
async def stories_link(message: Message, state: FSMContext, cfg: Config, bot: Bot) -> None:
    refs = parse_chat_input_list(message.text)
    if not refs:
        await message.answer(texts.STORIES_BAD_LINK, reply_markup=keyboards.cancel_kb())
        return
    await safe_delete(bot, message.chat.id, message.message_id)
    await state.update_data(chats=refs, peer_limit=cfg.default_peers_limit)
    await state.set_state(StoriesStates.waiting_mode)
    await message.answer(
        texts.STORIES_CHOOSE_MODE.format(count=len(refs), chat_list=_chat_list_block(refs)),
        reply_markup=keyboards.stories_mode_kb(cfg.default_peers_limit),
    )


# ---------------------------------------------------------------------- лимит участников


@router.callback_query(StoriesStates.waiting_mode, F.data.startswith(keyboards.CB_STORY_PEERS))
async def stories_peers(cb: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    raw = (cb.data or "").removeprefix(keyboards.CB_STORY_PEERS)
    if raw == "custom":
        await state.set_state(StoriesStates.waiting_custom_peers)
        await safe_edit(cb, texts.STORIES_PEERS_ASK, keyboards.cancel_kb())
        await cb.answer()
        return
    try:
        peer_limit = max(0, int(raw))
    except ValueError:
        peer_limit = cfg.default_peers_limit
    await state.update_data(peer_limit=peer_limit)
    data = await state.get_data()
    chats: list[ChatRef] = data["chats"]
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_text"):
        with suppress(TelegramBadRequest):
            await msg.edit_text(
                texts.STORIES_CHOOSE_MODE.format(count=len(chats), chat_list=_chat_list_block(chats)),
                reply_markup=keyboards.stories_mode_kb(peer_limit),
            )
    await cb.answer(texts.STORIES_PEERS_SET.format(limit="♾ без лимита" if peer_limit == 0 else peer_limit))


@router.message(StoriesStates.waiting_custom_peers, F.text)
async def stories_peers_text(message: Message, state: FSMContext, cfg: Config, bot: Bot) -> None:
    raw = (message.text or "").strip().replace(" ", "")
    await safe_delete(bot, message.chat.id, message.message_id)
    try:
        peer_limit = int(raw)
    except ValueError:
        await message.answer(texts.STORIES_PEERS_ASK, reply_markup=keyboards.cancel_kb())
        return
    if peer_limit < 0 or peer_limit > MAX_PEERS:
        await message.answer(texts.STORIES_PEERS_ASK, reply_markup=keyboards.cancel_kb())
        return
    await state.update_data(peer_limit=peer_limit)
    await state.set_state(StoriesStates.waiting_mode)
    data = await state.get_data()
    chats: list[ChatRef] = data["chats"]
    await message.answer(
        texts.STORIES_CHOOSE_MODE.format(count=len(chats), chat_list=_chat_list_block(chats)),
        reply_markup=keyboards.stories_mode_kb(peer_limit),
    )


# ---------------------------------------------------------------------- запуск


def _make_runner(
    *,
    bot: Bot,
    client,
    account_name: str,
    refs: List[ChatRef],
    like: bool,
    peer_limit: Optional[int],
    cfg: Config,
    stats: stories_service.StoriesStats,
    chat_id: int,
    message_id: int,
) -> Callable[[TaskInfo], "asyncio.Future"]:
    async def runner(info: TaskInfo) -> None:
        progress = Progress(
            bot,
            chat_id,
            message_id,
            stories_service.render_stories_progress,
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
            await stories_service.run_stories(
                client,
                refs,
                like=like,
                emoji="❤️",
                peer_limit=peer_limit,
                story_delay=cfg.story_delay,
                peer_delay=cfg.peer_delay,
                flood_cap=cfg.flood_wait_cap,
                progress=progress,
                stats=stats,
            )
            await progress.finish(stories_service.render_stories_summary(stats))
        except asyncio.CancelledError:
            with suppress(Exception):
                await progress.finish(
                    "🛑 <b>Остановлено.</b>\n\n" + stories_service.render_stories_summary(stats)
                )
            raise
        except TgError as e:
            info.state = "error"
            info.error = str(e)
            with suppress(Exception):
                await progress.finish(f"❌ {e}")
        except Exception as e:
            logger.exception("Задача сторий упала с неожиданной ошибкой")
            info.state = "error"
            info.error = f"{type(e).__name__}: {e}"[:300]
            with suppress(Exception):
                await progress.finish(texts.TASK_ERROR.format(err=texts.esc(e)))

    return runner


@router.callback_query(StoriesStates.waiting_mode, F.data.startswith(keyboards.CB_STORY_MODE))
async def stories_run(
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

    like = (cb.data or "").removeprefix(keyboards.CB_STORY_MODE) != "view"
    data = await state.get_data()
    await state.clear()
    infos = await fetch_logged_accounts(manager, uid)
    chosen = selected_order(infos, set(data.get("account_ids") or []))
    if not chosen:
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return
    chats: list[ChatRef] = data["chats"]
    raw_limit = int(data.get("peer_limit") or 0)
    peer_limit: Optional[int] = None if raw_limit == 0 else max(1, raw_limit)
    await cb.answer()

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

    launched = 0
    for info, (chat_id, message_id) in zip(chosen, placeholders):
        client = manager.get(uid, info["id"])
        if client is None:
            continue
        stats = stories_service.StoriesStats(
            like=like, peer_limit=peer_limit or 0, chats_total=len(chats)
        )
        task_info = registry.start(
            uid,
            _make_runner(
                bot=bot,
                client=client,
                account_name=info["name"],
                refs=list(chats),
                like=like,
                peer_limit=peer_limit,
                cfg=cfg,
                stats=stats,
                chat_id=chat_id,
                message_id=message_id,
            ),
            kind="stories",
            chat=f"{len(chats)} чатов",
            detail="стории 👀❤️" if like else "стории 👀",
            account_name=info["name"],
            short_renderer=stories_service.short_stories,
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


@router.message(StoriesStates.waiting_link)
@router.message(StoriesStates.waiting_custom_peers)
async def stories_fallback(message: Message) -> None:
    await message.answer(texts.NEED_TEXT, reply_markup=keyboards.cancel_kb())
