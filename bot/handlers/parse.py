"""Функция: парсинг участников чатов с фильтрами → выгрузка в Excel.

Поток: аккаунты → ссылки на чаты → фильтры → запуск.
По аккаунтам чаты распределяются поровну; каждый аккаунт — своя задача.
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
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot import keyboards, texts
from bot.config import Config
from bot.handlers.common import fetch_logged_accounts, selected_order
from bot.progress import Progress
from bot.registry import TaskInfo, TaskRegistry
from bot.services import parse_service
from bot.services.base import ChatRef, TgError, parse_chat_input_list
from bot.services.excel import build_users_xlsx
from bot.tg_client import ClientManager
from bot.utils import safe_delete, safe_edit

logger = logging.getLogger(__name__)
router = Router(name="parse")
router.message.filter(F.chat.type == "private")

_MESSAGE_GAP = 1.05
MAX_LIMIT = 20_000


class ParseStates(StatesGroup):
    waiting_accounts = State()
    waiting_links = State()
    waiting_criteria = State()
    waiting_custom = State()   # ввод числа: peers | scan


def _chat_list_block(refs: list[ChatRef], max_lines: int = 6) -> str:
    lines = [f"  • <code>{texts.esc(ref.value)}</code>" for ref in refs[:max_lines]]
    if len(refs) > max_lines:
        lines.append(f"  … и ещё {len(refs) - max_lines}")
    return "\n".join(lines)


def _split(items: list, parts: int) -> list[list]:
    """Распределяет элементы по частям по кругу (round-robin)."""
    buckets: list[list] = [[] for _ in range(parts)]
    for i, item in enumerate(items):
        buckets[i % parts].append(item)
    return buckets


# ---------------------------------------------------------------------- шаг 0: аккаунты


@router.callback_query(F.data == keyboards.CB_FLOW_PARSE)
async def cb_parse_start(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    if not infos:
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return
    await state.set_state(ParseStates.waiting_accounts)
    await state.update_data(account_ids=None, chats=None, seen_hours=0, photo="any",
                            username="any", peer_limit=0, scan_limit=500)
    if len(infos) == 1:
        await state.update_data(account_ids=[infos[0]["id"]])
        await state.set_state(ParseStates.waiting_links)
        await safe_edit(cb, texts.PARSE_ASK_LINK, keyboards.cancel_kb())
    else:
        await safe_edit(cb, texts.ACCSEL_ASK, keyboards.accounts_multiselect_kb(infos, set()))
    await cb.answer()


@router.callback_query(ParseStates.waiting_accounts, F.data.startswith(keyboards.CB_ACCSEL_TOGGLE))
async def parse_acc_toggle(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
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


@router.callback_query(ParseStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_ALL)
async def parse_acc_all(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    await state.update_data(account_ids=[i["id"] for i in infos])
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_reply_markup"):
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(
                reply_markup=keyboards.accounts_multiselect_kb(infos, {i["id"] for i in infos})
            )
    await cb.answer(texts.ACCSEL_SELECTED.format(n=len(infos)))


@router.callback_query(ParseStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_DONE)
async def parse_acc_done(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("account_ids"):
        await cb.answer(texts.ACCSEL_NONE, show_alert=True)
        return
    await state.set_state(ParseStates.waiting_links)
    await safe_edit(cb, texts.PARSE_ASK_LINK, keyboards.cancel_kb())
    await cb.answer()


# ---------------------------------------------------------------------- шаг 1: ссылки


@router.message(ParseStates.waiting_links, F.text)
async def parse_links(message: Message, state: FSMContext, bot: Bot) -> None:
    refs = parse_chat_input_list(message.text)
    if not refs:
        await message.answer(texts.PARSE_BAD_LINK, reply_markup=keyboards.cancel_kb())
        return
    await safe_delete(bot, message.chat.id, message.message_id)
    await state.update_data(chats=refs)
    await state.set_state(ParseStates.waiting_criteria)
    data = await state.get_data()
    await message.answer(
        texts.PARSE_CRITERIA.format(count=len(refs), chat_list=_chat_list_block(refs)),
        reply_markup=keyboards.parse_criteria_kb(
            int(data.get("seen_hours") or 0),
            data.get("photo") or "any",
            data.get("username") or "any",
            int(data.get("peer_limit") or 0),
            int(data.get("scan_limit") or 500),
        ),
    )


# ---------------------------------------------------------------------- шаг 2: фильтры


def _criteria_kb_from(state: FSMContext):
    data = state  # хелпер не нужен — оставлено для симметрии
    raise NotImplementedError


@router.callback_query(ParseStates.waiting_criteria, F.data.startswith(keyboards.CB_PARSE_SEEN))
async def parse_seen(cb: CallbackQuery, state: FSMContext) -> None:
    current = int((cb.data or "").removeprefix(keyboards.CB_PARSE_SEEN))
    try:
        nxt = keyboards.SEEN_CHOICES[(keyboards.SEEN_CHOICES.index(current) + 1) % len(keyboards.SEEN_CHOICES)]
    except ValueError:
        nxt = 0
    await state.update_data(seen_hours=nxt)
    await _refresh_criteria(cb, state)
    await cb.answer()


@router.callback_query(ParseStates.waiting_criteria, F.data.startswith(keyboards.CB_PARSE_PHOTO))
async def parse_photo(cb: CallbackQuery, state: FSMContext) -> None:
    current = (cb.data or "").removeprefix(keyboards.CB_PARSE_PHOTO)
    order = ["any", "yes", "no"]
    nxt = order[(order.index(current) + 1) % len(order)] if current in order else "any"
    await state.update_data(photo=nxt)
    await _refresh_criteria(cb, state)
    await cb.answer()


@router.callback_query(ParseStates.waiting_criteria, F.data.startswith(keyboards.CB_PARSE_USERNAME))
async def parse_username(cb: CallbackQuery, state: FSMContext) -> None:
    current = (cb.data or "").removeprefix(keyboards.CB_PARSE_USERNAME)
    order = ["any", "yes", "no"]
    nxt = order[(order.index(current) + 1) % len(order)] if current in order else "any"
    await state.update_data(username=nxt)
    await _refresh_criteria(cb, state)
    await cb.answer()


@router.callback_query(ParseStates.waiting_criteria, F.data.startswith(keyboards.CB_PARSE_PEERS))
async def parse_peers(cb: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    raw = (cb.data or "").removeprefix(keyboards.CB_PARSE_PEERS)
    if raw == "custom":
        await state.set_state(ParseStates.waiting_custom)
        await state.update_data(custom_field="peers")
        await safe_edit(cb, texts.PARSE_LIMIT_ASK, keyboards.cancel_kb())
        await cb.answer()
        return
    await state.update_data(peer_limit=max(0, int(raw) if raw.isdigit() else 0))
    await _refresh_criteria(cb, state)
    await cb.answer()


@router.callback_query(ParseStates.waiting_criteria, F.data.startswith(keyboards.CB_PARSE_SCAN))
async def parse_scan(cb: CallbackQuery, state: FSMContext) -> None:
    raw = (cb.data or "").removeprefix(keyboards.CB_PARSE_SCAN)
    if raw == "custom":
        await state.set_state(ParseStates.waiting_custom)
        await state.update_data(custom_field="scan")
        await safe_edit(cb, texts.PARSE_SCAN_ASK, keyboards.cancel_kb())
        await cb.answer()
        return
    await state.update_data(scan_limit=max(0, int(raw) if raw.isdigit() else 500))
    await _refresh_criteria(cb, state)
    await cb.answer()


@router.message(ParseStates.waiting_custom, F.text)
async def parse_custom_value(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip().replace(" ", "")
    await safe_delete(bot, message.chat.id, message.message_id)
    data = await state.get_data()
    field_name = data.get("custom_field") or "peers"
    try:
        value = int(raw)
    except ValueError:
        await message.answer(texts.PARSE_LIMIT_BAD, reply_markup=keyboards.cancel_kb())
        return
    if value < 0 or value > MAX_LIMIT:
        await message.answer(texts.PARSE_LIMIT_BAD, reply_markup=keyboards.cancel_kb())
        return
    await state.update_data(custom_field=None)
    if field_name == "peers":
        await state.update_data(peer_limit=value)
    else:
        await state.update_data(scan_limit=value)
    await state.set_state(ParseStates.waiting_criteria)
    refs: list[ChatRef] = data["chats"]
    await message.answer(
        texts.PARSE_CRITERIA.format(count=len(refs), chat_list=_chat_list_block(refs)),
        reply_markup=keyboards.parse_criteria_kb(
            int(data.get("seen_hours") or 0),
            data.get("photo") or "any",
            data.get("username") or "any",
            int(data.get("peer_limit") or 0),
            int(data.get("scan_limit") or 500),
        ),
    )


async def _refresh_criteria(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    refs: list[ChatRef] = data["chats"]
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_text"):
        with suppress(TelegramBadRequest):
            await msg.edit_text(
                texts.PARSE_CRITERIA.format(count=len(refs), chat_list=_chat_list_block(refs)),
                reply_markup=keyboards.parse_criteria_kb(
                    int(data.get("seen_hours") or 0),
                    data.get("photo") or "any",
                    data.get("username") or "any",
                    int(data.get("peer_limit") or 0),
                    int(data.get("scan_limit") or 500),
                ),
            )


# ---------------------------------------------------------------------- запуск


def _make_runner(
    *,
    bot: Bot,
    client,
    account_name: str,
    refs: List[ChatRef],
    filters: parse_service.ParseFilters,
    peer_limit: Optional[int],
    scan_limit: int,
    cfg: Config,
    stats: parse_service.ParseStats,
    chat_id: int,
    message_id: int,
) -> Callable[[TaskInfo], "asyncio.Future"]:
    async def runner(info: TaskInfo) -> None:
        progress = Progress(
            bot,
            chat_id,
            message_id,
            parse_service.render_parse_progress,
            cfg.progress_interval,
            running_markup=lambda: keyboards.running_kb(info.task_id),
        )
        progress.bind(stats)
        progress.start()
        with suppress(TelegramBadRequest):
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=texts.TASK_STARTED,
                reply_markup=keyboards.running_kb(info.task_id),
            )
        try:
            import time as _time

            stats.started_at = _time.time()
            await parse_service.run_parse(
                client,
                refs,
                filters,
                peer_limit=peer_limit,
                scan_limit=scan_limit,
                flood_cap=cfg.flood_wait_cap,
                progress=progress,
                stats=stats,
            )
            summary = parse_service.render_parse_summary(stats)
            if stats.users:
                excel = build_users_xlsx(stats.users)
                await bot.send_document(
                    chat_id,
                    BufferedInputFile(excel, filename=f"parse_{account_name.replace(' ', '_')}_{len(stats.users)}users.xlsx"),
                    caption=f"📎 {summary}",
                )
                with suppress(Exception):
                    await progress.finish("🏁 <b>Готово!</b> Excel-файл отправлен выше ⬆️\n\n" + summary)
            else:
                await progress.finish(summary + "\n\nℹ️ Под фильтры никто не подошёл — файл не создан.")
        except asyncio.CancelledError:
            with suppress(Exception):
                await progress.finish("🛑 <b>Остановлено.</b>")
            raise
        except TgError as e:
            info.state = "error"
            info.error = str(e)
            with suppress(Exception):
                await progress.finish(f"❌ {e}")
        except Exception as e:
            logger.exception("Задача парсинга упала")
            info.state = "error"
            info.error = f"{type(e).__name__}: {e}"[:300]
            with suppress(Exception):
                await progress.finish(texts.TASK_ERROR.format(err=texts.esc(e)))

    return runner


@router.callback_query(ParseStates.waiting_criteria, F.data == keyboards.CB_PARSE_RUN)
async def parse_run(
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
    filters = parse_service.ParseFilters(
        seen_hours=int(data.get("seen_hours") or 0),
        photo=data.get("photo") or "any",
        username=data.get("username") or "any",
    )
    raw_peers = int(data.get("peer_limit") or 0)
    peer_limit: Optional[int] = None if raw_peers == 0 else raw_peers
    scan_limit = int(data.get("scan_limit") or 500)
    await cb.answer()

    # чаты делим между аккаунтами по кругу
    per_account_refs = _split(chats, len(chosen))

    placeholders: list[tuple[int, int]] = []
    for i, info in enumerate(chosen):
        label = f"⏳ Готовлю парсинг: <i>{texts.esc(info['name'])}</i>"
        if i == 0:
            msg = await safe_edit(cb, label)
            if msg is not None:
                placeholders.append((msg.chat.id, msg.message_id))
                continue
        await asyncio.sleep(_MESSAGE_GAP)
        msg = await bot.send_message(uid, label)
        placeholders.append((msg.chat.id, msg.message_id))

    launched = 0
    for info, refs, (chat_id, message_id) in zip(chosen, per_account_refs, placeholders):
        client = manager.get(uid, info["id"])
        if client is None or not refs:
            continue
        stats = parse_service.ParseStats(chats_total=len(refs))
        task_info = registry.start(
            uid,
            _make_runner(
                bot=bot,
                client=client,
                account_name=info["name"],
                refs=refs,
                filters=filters,
                peer_limit=peer_limit,
                scan_limit=scan_limit,
                cfg=cfg,
                stats=stats,
                chat_id=chat_id,
                message_id=message_id,
            ),
            kind="parse",
            chat=f"{len(refs)} чатов",
            detail="🕵️ парсинг",
            account_name=info["name"],
            short_renderer=parse_service.short_parse,
            stats_obj=stats,
        )
        if task_info is None:
            break
        launched += 1

    if launched and len(chosen) > cfg.max_concurrent_tasks:
        with suppress(Exception):
            await bot.send_message(uid, texts.TASK_QUEUED_NOTE)


@router.message(ParseStates.waiting_links)
@router.message(ParseStates.waiting_custom)
async def parse_fallback(message: Message) -> None:
    await message.answer(texts.NEED_TEXT, reply_markup=keyboards.cancel_kb())
