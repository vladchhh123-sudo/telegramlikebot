"""Старт, меню, менеджмент аккаунтов, экран задач, остановка задач."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import keyboards, texts
from bot.config import Config
from bot.registry import TaskRegistry
from bot.tg_client import ClientManager
from bot.utils import safe_edit

logger = logging.getLogger(__name__)
router = Router(name="start")
router.message.filter(F.chat.type == "private")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, manager: ClientManager) -> None:
    await state.clear()
    has_account = manager.is_logged_in(message.from_user.id)
    text = (
        texts.MENU.format(n=manager.accounts_count(message.from_user.id))
        if has_account
        else texts.WELCOME
    )
    await message.answer(text, reply_markup=keyboards.main_menu(has_account))


@router.message(Command("help"))
async def cmd_help(message: Message, cfg: Config) -> None:
    await message.answer(
        texts.HELP.format(max_concurrent=cfg.max_concurrent_tasks),
        reply_markup=keyboards.back_to_menu_kb(),
    )


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext, manager: ClientManager) -> None:
    await manager.cancel_login(message.from_user.id)
    await state.clear()
    await message.answer(texts.CANCELLED, reply_markup=keyboards.back_to_menu_kb())


@router.message(Command("stop"), StateFilter("*"))
async def cmd_stop(message: Message, registry: TaskRegistry) -> None:
    stopped = registry.stop_all(message.from_user.id)
    if stopped:
        await message.answer(texts.TASKS_STOPPING_ALL.format(n=stopped))
    else:
        await message.answer(texts.TASKS_NOT_RUNNING)


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, registry: TaskRegistry) -> None:
    text, kb = _render_tasks(message.from_user.id, registry)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("accounts"))
async def cmd_accounts(message: Message, manager: ClientManager) -> None:
    infos = await manager.list_account_infos(message.from_user.id)
    if not infos:
        await message.answer(texts.ACCOUNTS_EMPTY, reply_markup=keyboards.accounts_manage_kb([]))
        return
    await message.answer(
        _render_accounts(infos), reply_markup=keyboards.accounts_manage_kb(infos)
    )


@router.message(Command("my"))
async def cmd_my(message: Message, manager: ClientManager) -> None:
    infos = await manager.list_account_infos(message.from_user.id)
    if not infos:
        await message.answer(texts.NO_ACCOUNT_INFO, reply_markup=keyboards.main_menu(False))
        return
    await message.answer(
        _render_accounts(infos), reply_markup=keyboards.accounts_manage_kb(infos)
    )


@router.message(Command("logout"))
async def cmd_logout(message: Message, manager: ClientManager) -> None:
    infos = await manager.list_account_infos(message.from_user.id)
    if not infos:
        await message.answer(texts.NO_ACCOUNT_INFO, reply_markup=keyboards.main_menu(False))
        return
    if len(infos) == 1:
        info = infos[0]
        await message.answer(
            texts.LOGOUT_CONFIRM.format(name=texts.esc(info["name"])),
            reply_markup=keyboards.account_logout_confirm_kb(info["id"]),
        )
    else:
        await message.answer(
            _render_accounts(infos), reply_markup=keyboards.accounts_manage_kb(infos)
        )


# ---------------------------------------------------------------------- экран задач


def _render_tasks(user_id: int, registry: TaskRegistry) -> tuple[str, InlineKeyboardMarkup]:
    tasks = registry.list_tasks(user_id)
    if not tasks:
        return texts.TASKS_EMPTY, keyboards.tasks_kb([])
    active = [t for t in tasks if t.is_active]
    finished = [t for t in tasks if not t.is_active]
    header = texts.TASKS_HEADER.format(active=len(active), total=len(tasks))
    lines = [header, ""]
    for t in active:
        lines.append(t.status_line())
        lines.append("")
    shown_finished = finished[:5]
    for t in shown_finished:
        lines.append(t.status_line())
        lines.append("")
    if len(finished) > len(shown_finished):
        lines.append(f"… и ещё {len(finished) - len(shown_finished)} завершённых («🗑 Очистить историю»)")
    lines.append(texts.TASKS_HINT)
    active_ids = [t.task_id for t in active]
    return "\n".join(lines), keyboards.tasks_kb(active_ids)


async def _show_tasks(cb: CallbackQuery, registry: TaskRegistry) -> None:
    text, kb = _render_tasks(cb.from_user.id, registry)
    await safe_edit(cb, text, kb)


@router.callback_query(F.data == keyboards.CB_TASKS)
async def cb_tasks(cb: CallbackQuery, registry: TaskRegistry) -> None:
    await _show_tasks(cb, registry)
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_TASKS_REFRESH)
async def cb_tasks_refresh(cb: CallbackQuery, registry: TaskRegistry) -> None:
    await _show_tasks(cb, registry)
    await cb.answer("🔄 Обновлено")


@router.callback_query(F.data == keyboards.CB_TASKS_CLEAR)
async def cb_tasks_clear(cb: CallbackQuery, registry: TaskRegistry) -> None:
    removed = registry.clear_finished(cb.from_user.id)
    await _show_tasks(cb, registry)
    await cb.answer(texts.TASKS_CLEARED.format(n=removed))


@router.callback_query(F.data == keyboards.CB_TASKS_STOP_ALL)
async def cb_tasks_stop_all(cb: CallbackQuery, registry: TaskRegistry) -> None:
    stopped = registry.stop_all(cb.from_user.id)
    await _show_tasks(cb, registry)
    if stopped:
        await cb.answer(texts.TASKS_STOPPING_ALL.format(n=stopped))
    else:
        await cb.answer(texts.TASKS_NOT_RUNNING, show_alert=True)


@router.callback_query(F.data.startswith(keyboards.CB_TASK_STOP_ID))
async def cb_task_stop_one(cb: CallbackQuery, registry: TaskRegistry) -> None:
    task_id = (cb.data or "").removeprefix(keyboards.CB_TASK_STOP_ID)
    if registry.stop(cb.from_user.id, task_id):
        await cb.answer(texts.TASK_STOPPED_ONE.format(id=task_id))
    else:
        await cb.answer(texts.TASK_STOP_MISSED.format(id=task_id), show_alert=True)


# ---------------------------------------------------------------------- меню / аккаунты


def _render_accounts(infos: list[dict]) -> str:
    lines = [texts.ACCOUNTS_HEADER.format(n=len(infos))]
    for i, info in enumerate(infos, 1):
        status = "🟢 в сети" if info["logged"] else "🔴 сессия недействительна"
        lines.append(f"{i}. <b>{texts.esc(info['name'])}</b> — <code>{texts.esc(info['phone'])}</code> · {status}")
    return "\n".join(lines)


@router.callback_query(F.data == keyboards.CB_MAIN)
async def cb_main(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    await state.clear()
    has_account = manager.is_logged_in(cb.from_user.id)
    text = (
        texts.MENU.format(n=manager.accounts_count(cb.from_user.id))
        if has_account
        else texts.WELCOME
    )
    await safe_edit(cb, text, keyboards.main_menu(has_account))
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_ACCOUNTS)
async def cb_accounts(cb: CallbackQuery, manager: ClientManager) -> None:
    infos = await manager.list_account_infos(cb.from_user.id)
    if not infos:
        await safe_edit(cb, texts.ACCOUNTS_EMPTY, keyboards.accounts_manage_kb([]))
    else:
        await safe_edit(cb, _render_accounts(infos), keyboards.accounts_manage_kb(infos))
    await cb.answer()


@router.callback_query(F.data.startswith(keyboards.CB_ACCOUNT_VIEW))
async def cb_account_view(cb: CallbackQuery, manager: ClientManager) -> None:
    try:
        acc_id = int((cb.data or "").removeprefix(keyboards.CB_ACCOUNT_VIEW))
    except ValueError:
        await cb.answer(texts.CANCELLED)
        return
    info = await manager.account_info(cb.from_user.id, acc_id)
    if info is None:
        db_info = next(
            (i for i in await manager.list_account_infos(cb.from_user.id) if i["id"] == acc_id), None
        )
        name = texts.esc(db_info["name"]) if db_info else "аккаунт"
        await safe_edit(
            cb,
            f"⚠️ Аккаунт <b>{name}</b>: сессия недействительна.\nУдали его и добавь заново.",
            keyboards.account_detail_kb(acc_id),
        )
    else:
        await safe_edit(
            cb,
            texts.ACCOUNT_INFO.format(
                name=texts.esc(info["name"]),
                username=texts.esc(info["username"]),
                uid=info["id"],
                phone=texts.esc(info["phone"]),
                photo="🖼 установлено" if info["has_photo"] else "нет",
            ),
            keyboards.account_detail_kb(acc_id),
        )
    await cb.answer()


@router.callback_query(F.data.startswith(keyboards.CB_ACCOUNT_LOGOUT_YES))
async def cb_account_logout_yes(cb: CallbackQuery, manager: ClientManager) -> None:
    try:
        acc_id = int((cb.data or "").removeprefix(keyboards.CB_ACCOUNT_LOGOUT_YES))
    except ValueError:
        await cb.answer(texts.CANCELLED)
        return
    info = await manager.account_info(cb.from_user.id, acc_id)
    name = info["name"] if info else "аккаунт"
    await manager.logout_account(cb.from_user.id, acc_id)
    logger.info("Аккаунт %s (owner=%s) удалён", acc_id, cb.from_user.id)
    has_account = manager.is_logged_in(cb.from_user.id)
    text = (
        texts.MENU.format(n=manager.accounts_count(cb.from_user.id))
        if has_account
        else texts.WELCOME
    )
    await safe_edit(cb, texts.LOGOUT_DONE.format(name=texts.esc(name)) + "\n\n" + text,
                    keyboards.main_menu(has_account))
    await cb.answer()


@router.callback_query(F.data.startswith(keyboards.CB_ACCOUNT_LOGOUT))
async def cb_account_logout(cb: CallbackQuery, manager: ClientManager) -> None:
    try:
        acc_id = int((cb.data or "").removeprefix(keyboards.CB_ACCOUNT_LOGOUT))
    except ValueError:
        await cb.answer(texts.CANCELLED)
        return
    info = await manager.account_info(cb.from_user.id, acc_id)
    if info is None:
        await safe_edit(cb, "⚠️ Сессия аккаунта недействительна.", keyboards.account_detail_kb(acc_id))
    else:
        await safe_edit(
            cb,
            texts.LOGOUT_CONFIRM.format(name=texts.esc(info["name"])),
            keyboards.account_logout_confirm_kb(acc_id),
        )
    await cb.answer()
