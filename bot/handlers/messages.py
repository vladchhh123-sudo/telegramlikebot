"""Функция 3: рассылка сообщений — аккаунты → текст/фото → чаты → подтверждение → запуск.

По каждому аккаунту — отдельная задача, внутри — все чаты.
"""
from __future__ import annotations

import asyncio
import io
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
from bot.services import send_service
from bot.services.base import ChatRef, TgError, parse_chat_input_list
from bot.tg_client import ClientManager
from bot.utils import safe_delete, safe_edit

logger = logging.getLogger(__name__)
router = Router(name="messages")
router.message.filter(F.chat.type == "private")

_MESSAGE_GAP = 1.05


class SendStates(StatesGroup):
    waiting_accounts = State()
    waiting_content = State()
    waiting_links = State()
    waiting_confirm = State()


def _chat_list_block(refs: list[ChatRef], max_lines: int = 6) -> str:
    lines = [f"  • <code>{texts.esc(ref.value)}</code>" for ref in refs[:max_lines]]
    if len(refs) > max_lines:
        lines.append(f"  … и ещё {len(refs) - max_lines}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- шаг 0: аккаунты


@router.callback_query(F.data == keyboards.CB_FLOW_SEND)
async def cb_send_start(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    if not infos:
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return
    await state.set_state(SendStates.waiting_accounts)
    await state.update_data(account_ids=None, text=None, photo_file_id=None, chats=None)
    if len(infos) == 1:
        await state.update_data(account_ids=[infos[0]["id"]])
        await state.set_state(SendStates.waiting_content)
        await safe_edit(cb, texts.SEND_ASK_CONTENT, keyboards.cancel_kb())
    else:
        await safe_edit(cb, texts.ACCSEL_ASK, keyboards.accounts_multiselect_kb(infos, set()))
    await cb.answer()


@router.callback_query(SendStates.waiting_accounts, F.data.startswith(keyboards.CB_ACCSEL_TOGGLE))
async def send_acc_toggle(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
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


@router.callback_query(SendStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_ALL)
async def send_acc_all(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    await state.update_data(account_ids=[i["id"] for i in infos])
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_reply_markup"):
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(
                reply_markup=keyboards.accounts_multiselect_kb(infos, {i["id"] for i in infos})
            )
    await cb.answer(texts.ACCSEL_SELECTED.format(n=len(infos)))


@router.callback_query(SendStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_DONE)
async def send_acc_done(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("account_ids"):
        await cb.answer(texts.ACCSEL_NONE, show_alert=True)
        return
    await state.set_state(SendStates.waiting_content)
    await safe_edit(cb, texts.SEND_ASK_CONTENT, keyboards.cancel_kb())
    await cb.answer()


# ---------------------------------------------------------------------- шаг 1: содержимое


@router.message(SendStates.waiting_content, F.photo)
async def send_content_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(bot, message.chat.id, message.message_id)
    await state.update_data(
        photo_file_id=message.photo[-1].file_id, text=(message.caption or "")
    )
    await state.set_state(SendStates.waiting_links)
    await message.answer(texts.SEND_ASK_LINK, reply_markup=keyboards.cancel_kb())


@router.message(SendStates.waiting_content, F.text)
async def send_content_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if not (message.text or "").strip():
        await message.answer(texts.SEND_BAD_CONTENT, reply_markup=keyboards.cancel_kb())
        return
    await safe_delete(bot, message.chat.id, message.message_id)
    await state.update_data(photo_file_id=None, text=message.text)
    await state.set_state(SendStates.waiting_links)
    await message.answer(texts.SEND_ASK_LINK, reply_markup=keyboards.cancel_kb())


# ---------------------------------------------------------------------- шаг 2: чаты


@router.message(SendStates.waiting_links, F.text)
async def send_links(message: Message, state: FSMContext, cfg: Config, bot: Bot) -> None:
    refs = parse_chat_input_list(message.text)
    if not refs:
        await message.answer(texts.REACT_BAD_LINK, reply_markup=keyboards.cancel_kb())
        return
    await safe_delete(bot, message.chat.id, message.message_id)
    data = await state.get_data()
    await state.update_data(chats=refs)
    await state.set_state(SendStates.waiting_confirm)

    text_body: str = data.get("text") or ""
    preview = texts.esc(text_body[:150] + ("…" if len(text_body) > 150 else "")) or texts.SEND_PREVIEW_EMPTY
    accounts_n = len(data.get("account_ids") or [])
    confirm = texts.SEND_CONFIRM.format(
        accounts=accounts_n,
        count=len(refs),
        chat_list=_chat_list_block(refs) + "\n",
        preview=preview,
        photo="✅ да" if data.get("photo_file_id") else "нет",
        delay_lo=cfg.send_delay[0],
        delay_hi=cfg.send_delay[1],
    )
    await message.answer(confirm, reply_markup=keyboards.send_confirm_kb())


# ---------------------------------------------------------------------- запуск


def _make_runner(
    *,
    bot: Bot,
    client,
    account_name: str,
    refs: List[ChatRef],
    text: str,
    photo_bytes: Optional[bytes],
    cfg: Config,
    stats: send_service.SendStats,
    chat_id: int,
    message_id: int,
) -> Callable[[TaskInfo], "asyncio.Future"]:
    async def runner(info: TaskInfo) -> None:
        progress = Progress(
            bot,
            chat_id,
            message_id,
            send_service.render_send_progress,
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
            await send_service.run_sends(
                client,
                refs,
                text=text,
                photo_bytes=photo_bytes,
                delay_range=cfg.send_delay,
                flood_cap=cfg.flood_wait_cap,
                progress=progress,
                stats=stats,
            )
            await progress.finish(send_service.render_send_summary(stats))
        except asyncio.CancelledError:
            with suppress(Exception):
                await progress.finish(
                    "🛑 <b>Остановлено.</b>\n\n" + send_service.render_send_summary(stats)
                )
            raise
        except TgError as e:
            info.state = "error"
            info.error = str(e)
            with suppress(Exception):
                await progress.finish(f"❌ {e}")
        except Exception as e:
            logger.exception("Задача рассылки упала с неожиданной ошибкой")
            info.state = "error"
            info.error = f"{type(e).__name__}: {e}"[:300]
            with suppress(Exception):
                await progress.finish(texts.TASK_ERROR.format(err=texts.esc(e)))

    return runner


@router.callback_query(SendStates.waiting_confirm, F.data == keyboards.CB_SEND_RUN)
async def send_run(
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
    text: str = data.get("text") or ""
    photo_file_id: Optional[str] = data.get("photo_file_id")
    await cb.answer()

    photo_bytes: Optional[bytes] = None
    if photo_file_id:
        buffer = io.BytesIO()
        await bot.download(photo_file_id, destination=buffer)
        photo_bytes = buffer.getvalue()

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
        stats = send_service.SendStats(chats_total=len(chats))
        task_info = registry.start(
            uid,
            _make_runner(
                bot=bot,
                client=client,
                account_name=info["name"],
                refs=list(chats),
                text=text,
                photo_bytes=photo_bytes,
                cfg=cfg,
                stats=stats,
                chat_id=chat_id,
                message_id=message_id,
            ),
            kind="send",
            chat=f"{len(chats)} чатов",
            detail="✉️ рассылка",
            account_name=info["name"],
            short_renderer=send_service.short_send,
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


@router.message(SendStates.waiting_content)
@router.message(SendStates.waiting_links)
async def send_fallback(message: Message) -> None:
    await message.answer(texts.NEED_TEXT, reply_markup=keyboards.cancel_kb())
