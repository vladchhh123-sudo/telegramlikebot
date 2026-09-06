"""Добавление аккаунта: номер → код → облачный пароль (2FA). Аккаунтов — сколько нужно."""
from __future__ import annotations

import logging
import re
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import keyboards, texts
from bot.tg_client import ClientManager, LoginError
from bot.utils import safe_delete, safe_edit

logger = logging.getLogger(__name__)
router = Router(name="login")
router.message.filter(F.chat.type == "private")

_PHONE_RE = re.compile(r"^\+?\d{7,15}$")


class LoginStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_qr = State()


@router.callback_query(F.data == keyboards.CB_LOGIN_START)
async def cb_login_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(LoginStates.waiting_phone)
    await safe_edit(cb, texts.LOGIN_CHOICE, keyboards.login_choice_kb())
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_LOGIN_PHONE)
async def cb_login_phone(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(LoginStates.waiting_phone)
    await safe_edit(cb, texts.LOGIN_ASK_PHONE, keyboards.cancel_kb())
    await cb.answer()


@router.callback_query(F.data == keyboards.CB_LOGIN_QR)
async def cb_login_qr(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    try:
        url = await manager.start_qr_login(cb.from_user.id)
    except LoginError as e:
        await cb.answer(f"❌ {e}", show_alert=True)
        return
    await state.set_state(LoginStates.waiting_qr)
    msg = await safe_edit(cb, texts.LOGIN_QR_ASK, keyboards.login_qr_kb(url))
    if msg is None:
        msg = cb.message
    await cb.answer()

    # цикл: ждём подтверждение ~3 минуты, раз в ~25 с обновляем кнопку (токен живёт ~30 с)
    for _ in range(7):
        try:
            res = await manager.wait_qr_login(cb.from_user.id, timeout=25)
        except LoginError:
            # вход отменён пользователем (❌) — тишина
            with suppress(Exception):
                await state.clear()
            return
        if res == "timeout":
            try:
                url = await manager.refresh_qr_login(cb.from_user.id)
            except LoginError:
                break
            with suppress(Exception):
                await msg.edit_reply_markup(reply_markup=keyboards.login_qr_kb(url))
            continue
        if res == "need_password":
            await state.set_state(LoginStates.waiting_password)
            with suppress(Exception):
                await msg.edit_text(texts.ASK_PASSWORD, reply_markup=keyboards.cancel_kb())
            return
        break  # ok — вошли

    await manager.cancel_login(cb.from_user.id)
    await state.clear()
    with suppress(Exception):
        await msg.edit_text(texts.LOGIN_QR_EXPIRED, reply_markup=keyboards.login_choice_kb())


@router.callback_query(F.data == keyboards.CB_LOGIN_CANCEL)
async def cb_login_cancel(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    await manager.cancel_login(cb.from_user.id)
    await state.clear()
    has_account = manager.is_logged_in(cb.from_user.id)
    text = (
        texts.MENU.format(n=manager.accounts_count(cb.from_user.id))
        if has_account
        else texts.WELCOME
    )
    await safe_edit(cb, text, keyboards.main_menu(has_account))
    await cb.answer(texts.CANCELLED)


@router.message(LoginStates.waiting_phone, F.text)
async def login_phone(message: Message, state: FSMContext, manager: ClientManager, bot: Bot) -> None:
    phone = re.sub(r"[\s()\-.]", "", message.text.strip())
    if not _PHONE_RE.fullmatch(phone):
        await message.answer(texts.BAD_PHONE, reply_markup=keyboards.cancel_kb())
        return

    await safe_delete(bot, message.chat.id, message.message_id)
    try:
        hint = await manager.start_login(message.from_user.id, phone)
    except LoginError as e:
        await message.answer(f"❌ {e}", reply_markup=keyboards.cancel_kb())
        return
    except Exception as e:
        logger.exception("Ошибка отправки кода")
        await message.answer(
            texts.TASK_ERROR.format(err=texts.esc(e)), reply_markup=keyboards.cancel_kb()
        )
        return

    await state.update_data(phone=phone)
    await state.set_state(LoginStates.waiting_code)
    await message.answer(texts.CODE_SENT.format(hint=hint), reply_markup=keyboards.cancel_kb())


@router.message(LoginStates.waiting_code, F.text)
async def login_code(message: Message, state: FSMContext, manager: ClientManager, bot: Bot) -> None:
    code = re.sub(r"\D", "", message.text)
    if not code:
        await message.answer(texts.NEED_TEXT, reply_markup=keyboards.cancel_kb())
        return

    await safe_delete(bot, message.chat.id, message.message_id)
    try:
        result = await manager.confirm_code(message.from_user.id, code)
    except LoginError as e:
        await message.answer(f"❌ {e}", reply_markup=keyboards.cancel_kb())
        return
    except Exception as e:
        logger.exception("Ошибка проверки кода")
        await message.answer(
            texts.TASK_ERROR.format(err=texts.esc(e)), reply_markup=keyboards.cancel_kb()
        )
        return

    if result == "need_password":
        await state.set_state(LoginStates.waiting_password)
        await message.answer(texts.ASK_PASSWORD, reply_markup=keyboards.cancel_kb())
        return
    await _login_done(message, state, manager)


@router.message(LoginStates.waiting_password, F.text)
async def login_password(message: Message, state: FSMContext, manager: ClientManager, bot: Bot) -> None:
    password = message.text.strip()
    await safe_delete(bot, message.chat.id, message.message_id)  # пароль не оставляем в чате
    try:
        await manager.confirm_password(message.from_user.id, password)
    except LoginError as e:
        await message.answer(f"❌ {e}", reply_markup=keyboards.cancel_kb())
        return
    except Exception as e:
        logger.exception("Ошибка проверки пароля")
        await message.answer(
            texts.TASK_ERROR.format(err=texts.esc(e)), reply_markup=keyboards.cancel_kb()
        )
        return
    await _login_done(message, state, manager)


async def _login_done(message: Message, state: FSMContext, manager: ClientManager) -> None:
    await state.clear()
    infos = await manager.list_account_infos(message.from_user.id)
    name = texts.esc(infos[-1]["name"]) if infos else "аккаунт"
    await message.answer(
        texts.LOGIN_OK.format(name=name, total=len(infos)),
        reply_markup=keyboards.main_menu(True),
    )


@router.message(LoginStates.waiting_phone)
@router.message(LoginStates.waiting_code)
@router.message(LoginStates.waiting_password)
@router.message(LoginStates.waiting_qr)
async def login_fallback(message: Message) -> None:
    await message.answer(texts.NEED_TEXT, reply_markup=keyboards.cancel_kb())
