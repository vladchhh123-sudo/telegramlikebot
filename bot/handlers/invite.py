"""Функция: инвайтинг — добавление пользователей из Excel/CSV/текста в чат.

Поток: аккаунт(ы) → целевой чат → список (файлом ИЛИ текстом) → настройки
(паузы: пресеты или своя, пачки, дневной лимит) → запуск.
- список делится между выбранными аккаунтами по кругу;
- уже пробованных ранее (по истории чата в БД) скипаем автоматически;
- участников целевого чата скипаем (уже в чате);
- после запуска история сохраняется — следующий список чистить не нужно.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from contextlib import suppress
from typing import Callable, Dict, List, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from telethon import TelegramClient, errors
from telethon.tl.types import User

from bot import keyboards, texts
from bot.config import Config
from bot.db import Database
from bot.handlers.common import fetch_logged_accounts, selected_order
from bot.progress import Progress
from bot.registry import TaskInfo, TaskRegistry
from bot.services import invite_service
from bot.services.base import ChatRef, TgError, normalize_invite_token, parse_chat_input
from bot.services.excel import build_report_xlsx, extract_users_from_file, extract_users_from_text
from bot.tg_client import ClientManager
from bot.utils import safe_delete, safe_edit

logger = logging.getLogger(__name__)
router = Router(name="invite")
router.message.filter(F.chat.type == "private")

_MESSAGE_GAP = 1.05
MAX_USERS = 20_000
_SAMPLE = 8
_DELAY_RANGE_RE = re.compile(r"^(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?$")


class InviteStates(StatesGroup):
    waiting_accounts = State()
    waiting_target = State()
    waiting_file = State()
    waiting_settings = State()
    waiting_custom_delay = State()


def _split(items: list, parts: int) -> list[list]:
    buckets: list[list] = [[] for _ in range(parts)]
    for i, item in enumerate(items):
        buckets[i % parts].append(item)
    return buckets


def _delay_label(data: dict) -> tuple[tuple[float, float], str]:
    """Диапазон паузы + подпись для кнопки."""
    idx = int(data.get("delay_idx") or 0)
    custom = data.get("custom_delay")
    if idx < 0 and custom:
        lo, hi = custom
        return (lo, hi), f"{int(lo)}–{int(hi)} c (своя)"
    lo, hi = keyboards.INVITE_DELAY_PRESETS[max(0, idx)]
    return (lo, hi), f"{lo}–{hi} c"


# ---------------------------------------------------------------------- шаг 0: аккаунты


@router.callback_query(F.data == keyboards.CB_FLOW_INVITE)
async def cb_invite_start(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    if not infos:
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return
    await state.set_state(InviteStates.waiting_accounts)
    await state.update_data(account_ids=None, target=None, users=None,
                            delay_idx=1, custom_delay=None, batch=5, daily=50, hist=True)
    if len(infos) == 1:
        await state.update_data(account_ids=[infos[0]["id"]])
        await state.set_state(InviteStates.waiting_target)
        await safe_edit(cb, texts.INVITE_ASK_LINK, keyboards.cancel_kb())
    else:
        await safe_edit(cb, texts.ACCSEL_ASK, keyboards.accounts_multiselect_kb(infos, set()))
    await cb.answer()


@router.callback_query(InviteStates.waiting_accounts, F.data.startswith(keyboards.CB_ACCSEL_TOGGLE))
async def invite_acc_toggle(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    data = await state.get_data()
    selected: set[int] = set(data.get("account_ids") or [])
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


@router.callback_query(InviteStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_ALL)
async def invite_acc_all(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    await state.update_data(account_ids=[i["id"] for i in infos])
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_reply_markup"):
        with suppress(TelegramBadRequest):
            await msg.edit_reply_markup(
                reply_markup=keyboards.accounts_multiselect_kb(infos, {i["id"] for i in infos})
            )
    await cb.answer(texts.ACCSEL_SELECTED.format(n=len(infos)))


@router.callback_query(InviteStates.waiting_accounts, F.data == keyboards.CB_ACCSEL_DONE)
async def invite_acc_done(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("account_ids"):
        await cb.answer(texts.ACCSEL_NONE, show_alert=True)
        return
    await state.set_state(InviteStates.waiting_target)
    await safe_edit(cb, texts.INVITE_ASK_LINK, keyboards.cancel_kb())
    await cb.answer()


# ---------------------------------------------------------------------- шаг 1: целевой чат


@router.message(InviteStates.waiting_target, F.text)
async def invite_target(message: Message, state: FSMContext, bot: Bot) -> None:
    ref = parse_chat_input(message.text or "")
    if ref is None:
        await message.answer(texts.INVITE_BAD_LINK, reply_markup=keyboards.cancel_kb())
        return
    await safe_delete(bot, message.chat.id, message.message_id)
    await state.update_data(target=ref)
    await state.set_state(InviteStates.waiting_file)
    await message.answer(texts.INVITE_ASK_FILE, reply_markup=keyboards.cancel_kb())


# ---------------------------------------------------------------------- шаг 2: список (файл или текст)


@router.message(InviteStates.waiting_file, F.document)
async def invite_file(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    name = doc.file_name or ""
    if not name.lower().endswith((".xlsx", ".csv", ".txt")):
        await message.answer(texts.INVITE_FILE_BAD, reply_markup=keyboards.cancel_kb())
        return
    buffer = io.BytesIO()
    await bot.download(doc, destination=buffer)
    tokens = extract_users_from_file(name, buffer.getvalue())
    await _accept_users(message, state, bot, tokens, source=name)


@router.message(InviteStates.waiting_file, F.text)
async def invite_text_list(message: Message, state: FSMContext, bot: Bot) -> None:
    """Список можно прислать обычным текстом: по одному или через запятую."""
    tokens = extract_users_from_text(message.text or "")
    await _accept_users(message, state, bot, tokens, source="текст")


async def _accept_users(message: Message, state: FSMContext, bot: Bot, tokens: List[str], source: str) -> None:
    if not tokens or len(tokens) > MAX_USERS:
        await message.answer(texts.INVITE_FILE_BAD, reply_markup=keyboards.cancel_kb())
        return
    await safe_delete(bot, message.chat.id, message.message_id)
    await state.update_data(users=tokens)
    await state.set_state(InviteStates.waiting_settings)
    sample = ", ".join(tokens[:_SAMPLE]) + (f" … +{len(tokens) - _SAMPLE}" if len(tokens) > _SAMPLE else "")
    await message.answer(texts.INVITE_USERS_LINE.format(n=len(tokens), sample=texts.esc(sample)))
    await _send_settings(message, state)


# ---------------------------------------------------------------------- шаг 3: настройки


def _settings_text(data: dict) -> str:
    (lo, hi), label = _delay_label(data)
    accounts_n = len(data.get("account_ids") or [])
    mode = "делится" if accounts_n > 1 else "полный у одного"
    batch = int(data.get("batch") or 0)
    daily = int(data.get("daily") or 0)
    return texts.INVITE_CONFIRM.format(
        accounts=accounts_n,
        mode=mode,
        chat=texts.esc(data.get("target").value),
        users=len(data.get("users") or []),
        delay_lo=int(lo),
        delay_hi=int(hi),
        delay_label=label,
        batch=str(batch) if batch else "выкл",
        daily="♾" if daily == 0 else str(daily),
        hist="вкл" if bool(data.get("hist", True)) else "выкл",
    )


async def _send_settings(target: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(data.get("delay_idx") or 0)
    custom = data.get("custom_delay")
    await target.answer(
        _settings_text(data),
        reply_markup=keyboards.invite_settings_kb(
            idx if idx >= 0 else 1, int(data.get("batch") or 0),
            int(data.get("daily") or 0), custom_label=custom,
            skip_history=bool(data.get("hist", True)),
        ),
    )


async def _refresh_settings(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(data.get("delay_idx") or 0)
    custom = data.get("custom_delay")
    msg = cb.message
    if msg is not None and hasattr(msg, "edit_text"):
        with suppress(TelegramBadRequest):
            await msg.edit_text(
                _settings_text(data),
                reply_markup=keyboards.invite_settings_kb(
                    idx if idx >= 0 else 1,
                    int(data.get("batch") or 0),
                    int(data.get("daily") or 0), custom_label=custom,
                    skip_history=bool(data.get("hist", True)),
                ),
            )


@router.callback_query(InviteStates.waiting_settings, F.data.startswith(keyboards.CB_INV_DELAY))
async def invite_delay(cb: CallbackQuery, state: FSMContext) -> None:
    idx = int((cb.data or "").removeprefix(keyboards.CB_INV_DELAY))
    idx = max(0, min(idx, len(keyboards.INVITE_DELAY_PRESETS) - 1))
    await state.update_data(delay_idx=idx, custom_delay=None)
    await _refresh_settings(cb, state)
    lo, hi = keyboards.INVITE_DELAY_PRESETS[idx]
    await cb.answer(f"⏱ Пауза: {lo}–{hi} c")


@router.callback_query(InviteStates.waiting_settings, F.data == keyboards.CB_INV_DELAY_CUSTOM)
async def invite_delay_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InviteStates.waiting_custom_delay)
    await safe_edit(cb, texts.INVITE_DELAY_ASK, keyboards.cancel_kb())
    await cb.answer()


@router.message(InviteStates.waiting_custom_delay, F.text)
async def invite_delay_custom_text(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    await safe_delete(bot, message.chat.id, message.message_id)
    m = _DELAY_RANGE_RE.match(raw)
    if not m:
        await message.answer(texts.INVITE_DELAY_BAD, reply_markup=keyboards.cancel_kb())
        return
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    lo, hi = sorted((lo, hi))
    if lo < 1 or hi > 3600:
        await message.answer(texts.INVITE_DELAY_BAD, reply_markup=keyboards.cancel_kb())
        return
    await state.update_data(delay_idx=-1, custom_delay=(float(lo), float(hi)))
    await state.set_state(InviteStates.waiting_settings)
    data = await state.get_data()
    await message.answer(_settings_text(data), reply_markup=keyboards.invite_settings_kb(
        1, int(data.get("batch") or 0), int(data.get("daily") or 0), custom_label=(lo, hi)))


@router.callback_query(InviteStates.waiting_settings, F.data.startswith(keyboards.CB_INV_BATCH))
async def invite_batch(cb: CallbackQuery, state: FSMContext) -> None:
    current = int((cb.data or "").removeprefix(keyboards.CB_INV_BATCH))
    try:
        nxt = keyboards.INVITE_BATCH_CHOICES[
            (keyboards.INVITE_BATCH_CHOICES.index(current) + 1) % len(keyboards.INVITE_BATCH_CHOICES)
        ]
    except ValueError:
        nxt = 5
    await state.update_data(batch=nxt)
    await _refresh_settings(cb, state)
    await cb.answer("📦 Пачка: " + ("выкл" if nxt == 0 else str(nxt)))


@router.callback_query(InviteStates.waiting_settings, F.data.startswith(keyboards.CB_INV_DAILY))
async def invite_daily(cb: CallbackQuery, state: FSMContext) -> None:
    current = int((cb.data or "").removeprefix(keyboards.CB_INV_DAILY))
    try:
        nxt = keyboards.INVITE_DAILY_CHOICES[
            (keyboards.INVITE_DAILY_CHOICES.index(current) + 1) % len(keyboards.INVITE_DAILY_CHOICES)
        ]
    except ValueError:
        nxt = 50
    await state.update_data(daily=nxt)
    await _refresh_settings(cb, state)
    await cb.answer("🚦 Лимит/день: " + ("♾" if nxt == 0 else str(nxt)))


@router.callback_query(InviteStates.waiting_settings, F.data == keyboards.CB_INV_HIST)
async def invite_hist_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cur = bool(data.get("hist", True))
    await state.update_data(hist=not cur)
    await _refresh_settings(cb, state)
    await cb.answer("⏭ Скип пробованных: " + ("выкл" if cur else "вкл"))


# ---------------------------------------------------------------------- запуск


def _make_runner(
    *,
    bot: Bot,
    db: Database,
    owner_id: int,
    client: TelegramClient,
    account_name: str,
    ref: ChatRef,
    tokens: List[str],
    opts: invite_service.InviteOptions,
    cfg: Config,
    stats: invite_service.InviteStats,
    chat_id: int,
    message_id: int,
    skip_history: bool = True,
) -> Callable[[TaskInfo], "asyncio.Future"]:
    async def runner(info: TaskInfo) -> None:
        progress = Progress(
            bot,
            chat_id,
            message_id,
            invite_service.render_invite_progress,
            cfg.progress_interval,
            running_markup=lambda: keyboards.running_kb(info.task_id),
        )
        progress.bind(stats)
        progress.start()
        entity = None
        chat_key = ref.value
        with suppress(TelegramBadRequest):
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=texts.TASK_STARTED,
                reply_markup=keyboards.running_kb(info.task_id),
            )
        try:
            from bot.services import base as base_svc

            entity = await base_svc.resolve_chat(client, ref)
            chat_key = str(getattr(entity, "id", ref.value))

            # 1) история прошлых запусков по этому чату → скип (отключается кнопкой)
            if skip_history:
                history = await db.get_invite_history(owner_id, chat_key)
                attempted = {normalize_invite_token(k) for k in history}
            else:
                attempted = set()

            # 2) участники целевого чата → скип «уже в чате» + резолв ID
            known_users: Dict[int, User] = {}
            try:
                async for u in client.iter_participants(entity, limit=10000):
                    if isinstance(u, User):
                        known_users[u.id] = u
            except errors.RPCError:
                progress.note("🙈 Не удалось получить участников чата — скип «уже в чате» недоступен.")
            if known_users:
                progress.note(f"В чате уже {len(known_users)} участников — их скипаем.")

            # 3) общий кэш профилей панели (из парсинга) → инвайт по голому ID
            numeric_ids = [t for t in tokens if t.isdigit()]
            extra_hashes = await db.get_entity_hashes(owner_id, numeric_ids) if numeric_ids else {}
            names_n = sum(1 for t in tokens if t.startswith("@"))
            phones_n = sum(1 for t in tokens if t.startswith("+"))
            logger.info(
                "📨 список: ID %d (с кэшем %d) · @юзернеймы %d · телефоны %d",
                len(numeric_ids), len(extra_hashes), names_n, phones_n,
            )
            if numeric_ids and not extra_hashes:
                progress.note(
                    f"⚠️ В списке {len(numeric_ids)} ID, но в кэше панели 0 профилей. "
                    "Сначала прогони 🕵️ Парсинг чата-источника на v3.24+ — иначе все получат «ID не найти»."
                )
            elif extra_hashes:
                progress.note(f"🧠 В кэше панели профили для {len(extra_hashes)} ID из списка.")

            await invite_service.run_invites(
                client, entity, tokens, opts, progress, stats,
                attempted=attempted, known_users=known_users, extra_hashes=extra_hashes,
            )
            summary = invite_service.render_invite_summary(stats)
            if stats.results:
                report = build_report_xlsx(stats.results)
                await bot.send_document(
                    chat_id,
                    BufferedInputFile(report, filename=f"invite_{account_name.replace(' ', '_')}.xlsx"),
                    caption="📎 Отчёт по каждому пользователю",
                )
            await progress.finish(summary)
        except asyncio.CancelledError:
            with suppress(Exception):
                await progress.finish("🛑 <b>Остановлено.</b>\n\n" + invite_service.render_invite_summary(stats))
            raise
        except TgError as e:
            info.state = "error"
            info.error = str(e)
            with suppress(Exception):
                await progress.finish(f"❌ {e}")
        except Exception as e:
            logger.exception("Задача инвайтинга упала")
            info.state = "error"
            info.error = f"{type(e).__name__}: {e}"[:300]
            with suppress(Exception):
                await progress.finish(texts.TASK_ERROR.format(err=texts.esc(e)))
        finally:
            logger.info(
                "📨 финиш: ✅%d ⛔️%d 🚫%d · скип(история %d, чат %d, прочее %d) из %d",
                stats.invited, stats.declined, stats.failed,
                stats.skipped_history, stats.skipped_member, stats.skipped, stats.total,
            )
            # историю сохраняем всегда (даже при остановке/ошибке)
            if stats.results:
                try:
                    await db.save_invite_history(owner_id, chat_key, stats.results)
                except Exception:
                    logger.exception("Не удалось сохранить историю инвайтов")
            if stats.resolved:
                with suppress(Exception):
                    await db.save_entities(owner_id, stats.resolved)  # профили — в общий кэш

    return runner


@router.callback_query(InviteStates.waiting_settings, F.data == keyboards.CB_INV_RUN)
async def invite_run(
    cb: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    cfg: Config,
    db: Database,
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
    ref: ChatRef = data["target"]
    users: List[str] = list(data.get("users") or [])
    delay_range, _label = _delay_label(data)
    batch = int(data.get("batch") or 0)
    daily = int(data.get("daily") or 0)
    hist = bool(data.get("hist", True))
    await cb.answer()

    per_account_users = _split(users, len(chosen))

    placeholders: list[tuple[int, int]] = []
    for i, info in enumerate(chosen):
        label = f"⏳ Готовлю инвайтинг: <i>{texts.esc(info['name'])}</i> ({len(per_account_users[i])} пользователей)"
        if i == 0:
            msg = await safe_edit(cb, label)
            if msg is not None:
                placeholders.append((msg.chat.id, msg.message_id))
                continue
        await asyncio.sleep(_MESSAGE_GAP)
        msg = await bot.send_message(uid, label)
        placeholders.append((msg.chat.id, msg.message_id))

    launched = 0
    for info, tokens, (chat_id, message_id) in zip(chosen, per_account_users, placeholders):
        client = manager.get(uid, info["id"])
        if client is None or not tokens:
            continue
        opts = invite_service.InviteOptions(
            delay_range=delay_range,
            batch_size=batch,
            batch_pause_range=(120.0, 240.0),
            daily_limit=daily,
            flood_cap=cfg.flood_wait_cap,
            max_consecutive_errors=8,
        )
        stats = invite_service.InviteStats(total=len(tokens), total_units=len(tokens))
        task_info = registry.start(
            uid,
            _make_runner(
                bot=bot,
                db=db,
                owner_id=uid,
                client=client,
                account_name=info["name"],
                ref=ref,
                tokens=tokens,
                opts=opts,
                cfg=cfg,
                stats=stats,
                chat_id=chat_id,
                message_id=message_id,
                skip_history=hist,
            ),
            kind="invite",
            chat=ref.value,
            detail=f"📨 {len(tokens)} польз.",
            account_name=info["name"],
            short_renderer=invite_service.short_invite,
            stats_obj=stats,
        )
        if task_info is None:
            break
        launched += 1

    if launched and len(chosen) > cfg.max_concurrent_tasks:
        with suppress(Exception):
            await bot.send_message(uid, texts.TASK_QUEUED_NOTE)


@router.message(InviteStates.waiting_target)
@router.message(InviteStates.waiting_file)
@router.message(InviteStates.waiting_custom_delay)
async def invite_fallback(message: Message) -> None:
    await message.answer(texts.NEED_TEXT, reply_markup=keyboards.cancel_kb())


@router.callback_query(
    F.data.startswith(keyboards.CB_INV_DELAY)
    | (F.data == keyboards.CB_INV_DELAY_CUSTOM)
    | F.data.startswith(keyboards.CB_INV_BATCH)
    | F.data.startswith(keyboards.CB_INV_DAILY)
    | (F.data == keyboards.CB_INV_HIST)
    | (F.data == keyboards.CB_INV_RUN)
)
async def invite_stale_screen(cb: CallbackQuery, state: FSMContext) -> None:
    """Кнопки со старого экрана настроек (после перезапуска) больше не молчат."""
    await cb.answer(texts.STALE_SCREEN, show_alert=True)
