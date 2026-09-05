"""Общие помощники обработчиков: выбор аккаунтов при создании задач."""
from __future__ import annotations

from typing import Any, Optional, Set

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import keyboards, texts
from bot.tg_client import ClientManager
from bot.utils import safe_edit


async def fetch_logged_accounts(manager: ClientManager, owner_id: int) -> list[dict[str, Any]]:
    """Список живых аккаунтов владельца (для выбора в задачах)."""
    return await manager.list_account_infos(owner_id, logged_only=True)


def single_or_none(infos: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    return infos[0] if len(infos) == 1 else None


def selected_order(infos: list[dict[str, Any]], selected: Set[int]) -> list[dict[str, Any]]:
    """Выбранные аккаунты в порядке списка."""
    return [info for info in infos if info["id"] in selected]


async def ask_accounts(
    cb: CallbackQuery,
    state_data_setter,
    infos: list[dict[str, Any]],
    selected: Optional[Set[int]] = None,
) -> None:
    """Показать экран выбора аккаунтов (selected=None = ничего не выбрано)."""
    sel: Set[int] = selected or set()
    await state_data_setter(sel)
    text = texts.ACCSEL_ASK
    await safe_edit(cb, text, keyboards.accounts_multiselect_kb(infos, sel))


def accounts_line(infos: list[dict[str, Any]]) -> str:
    return ", ".join(info["name"] for info in infos) or "—"
