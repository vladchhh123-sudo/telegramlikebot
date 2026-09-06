"""Функция 4: редактирование профиля аккаунта — имя, фамилия, био, юзернейм, фото."""
from __future__ import annotations

import io
import logging
import re
from contextlib import suppress
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from telethon import TelegramClient, errors
from telethon.tl.functions.account import CheckUsernameRequest, UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.photos import DeletePhotosRequest, UploadProfilePhotoRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPhoto, Photo

from bot import keyboards, texts
from bot.config import Config
from bot.handlers.common import fetch_logged_accounts
from bot.tg_client import ClientManager
from bot.utils import safe_delete, safe_edit

logger = logging.getLogger(__name__)
router = Router(name="profile")
router.message.filter(F.chat.type == "private")

# Префиксы кнопок наших сценариев. Роутер подключён ПОСЛЕДНИМ, поэтому сюда попадают
# только нажатия, которые не взял ни один живой обработчик = кнопки устаревших экранов.
_STALE_PREFIXES = (
    "parse:", "inv:", "acctsel:", "react:emoji:", "stories:peers",
    "msgs:", "prof:", "send:", "stories:own",
)


@router.callback_query(F.data.startswith(_STALE_PREFIXES))
async def _stale_buttons(cb: CallbackQuery) -> None:
    """Кнопки со старых экранов (состояние сброшено перезапуском) — не молчим."""
    await cb.answer(texts.STALE_SCREEN, show_alert=True)

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class ProfileStates(StatesGroup):
    waiting_pick = State()
    menu = State()
    waiting_name = State()
    waiting_last = State()
    waiting_about = State()
    waiting_username = State()
    waiting_photo = State()
    waiting_delphoto = State()


def _acc_pick_kb(infos: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"👤 {info['name']} ({info['phone']})",
            callback_data=f"prof:pick:{info['id']}",
        )]
        for info in infos
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=keyboards.CB_LOGIN_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def fetch_profile(client: TelegramClient) -> dict:
    me = await client.get_me()
    full = await client(GetFullUserRequest("me"))
    return {
        "name": (me.first_name or "—")[:64],
        "last": me.last_name or "—",
        "username": ("@" + me.username) if me.username else "—",
        "about": full.full_user.about or "—",
        "photo": "🖼 установлено" if me.photo is not None else "нет",
        "phone": me.phone or "—",
        "has_photo": me.photo is not None,
        "first_name": me.first_name or "",
        "last_name": me.last_name,
        "username_raw": me.username,
    }


def _profile_text(p: dict) -> str:
    return texts.PROFILE_VIEW.format(
        name=texts.esc(p["name"]),
        last=texts.esc(p["last"]),
        username=texts.esc(p["username"]),
        about=texts.esc(p["about"]),
        photo=p["photo"],
        phone=texts.esc(p["phone"]),
    )


async def _show_menu(cb_or_msg, state: FSMContext, manager: ClientManager, uid: int, acc_id: int) -> None:
    client = manager.get(uid, acc_id)
    if client is None:
        return
    p = await fetch_profile(client)
    await state.update_data(acc_id=acc_id)
    await state.set_state(ProfileStates.menu)
    kb = keyboards.profile_kb(p["has_photo"])
    if isinstance(cb_or_msg, CallbackQuery):
        await safe_edit(cb_or_msg, _profile_text(p), kb)
    else:
        await cb_or_msg.answer(_profile_text(p), reply_markup=kb)


# ---------------------------------------------------------------------- вход и выбор аккаунта


@router.callback_query(F.data == keyboards.CB_FLOW_PROFILE)
async def cb_profile_start(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    infos = await fetch_logged_accounts(manager, cb.from_user.id)
    if not infos:
        await cb.answer(texts.NO_ACCOUNT, show_alert=True)
        return
    if len(infos) == 1:
        await _show_menu(cb, state, manager, cb.from_user.id, infos[0]["id"])
    else:
        await state.set_state(ProfileStates.waiting_pick)
        await safe_edit(cb, texts.PROFILE_PICK, _acc_pick_kb(infos))
    await cb.answer()


@router.callback_query(ProfileStates.waiting_pick, F.data.startswith("prof:pick:"))
async def cb_profile_pick(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    try:
        acc_id = int((cb.data or "").removeprefix("prof:pick:"))
    except ValueError:
        await cb.answer(texts.CANCELLED)
        return
    await _show_menu(cb, state, manager, cb.from_user.id, acc_id)
    await cb.answer()


@router.callback_query(ProfileStates.menu, F.data == keyboards.CB_PROFILE_REFRESH)
async def cb_profile_refresh(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    data = await state.get_data()
    acc_id: Optional[int] = data.get("acc_id")
    if acc_id is None:
        await cb.answer(texts.PROFILE_NO_ACCOUNT_SEL, show_alert=True)
        return
    await _show_menu(cb, state, manager, cb.from_user.id, acc_id)
    await cb.answer("🔄 Обновлено")


# ---------------------------------------------------------------------- имя / фамилия / био


@router.callback_query(ProfileStates.menu, F.data == keyboards.CB_PROFILE_NAME)
async def cb_prof_name(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileStates.waiting_name)
    await safe_edit(cb, texts.PROFILE_ASK_NAME, keyboards.cancel_kb())
    await cb.answer()


@router.message(ProfileStates.waiting_name, F.text)
async def prof_name(message: Message, state: FSMContext, manager: ClientManager, bot: Bot) -> None:
    value = message.text.strip()
    await safe_delete(bot, message.chat.id, message.message_id)
    if not value or len(value) > 64:
        await message.answer(texts.PROFILE_NAME_BAD, reply_markup=keyboards.cancel_kb())
        return
    data = await state.get_data()
    acc_id: Optional[int] = data.get("acc_id")
    client = manager.get(message.from_user.id, acc_id or 0)
    if client is None:
        await message.answer(texts.PROFILE_NO_ACCOUNT_SEL, reply_markup=keyboards.back_to_menu_kb())
        return
    try:
        await client(UpdateProfileRequest(first_name=value))
    except errors.FirstNameInvalidError:
        await message.answer(texts.PROFILE_NAME_BAD, reply_markup=keyboards.cancel_kb())
        return
    except errors.FloodWaitError as e:
        await message.answer(f"⏳ Слишком часто. Повтори через {e.seconds + 1} c.")
        return
    except errors.RPCError as e:
        await message.answer(f"❌ Telegram: <b>{type(e).__name__}</b>", reply_markup=keyboards.back_to_menu_kb())
        return
    await _after_save(message, state, manager)


@router.callback_query(ProfileStates.menu, F.data == keyboards.CB_PROFILE_LAST)
async def cb_prof_last(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileStates.waiting_last)
    await safe_edit(cb, texts.PROFILE_ASK_LAST, keyboards.cancel_kb())
    await cb.answer()


@router.message(ProfileStates.waiting_last, F.text)
async def prof_last(message: Message, state: FSMContext, manager: ClientManager, bot: Bot) -> None:
    value = message.text.strip()
    await safe_delete(bot, message.chat.id, message.message_id)
    if len(value) > 64:
        await message.answer(texts.PROFILE_LAST_BAD, reply_markup=keyboards.cancel_kb())
        return
    data = await state.get_data()
    client = manager.get(message.from_user.id, data.get("acc_id") or 0)
    if client is None:
        await message.answer(texts.PROFILE_NO_ACCOUNT_SEL, reply_markup=keyboards.back_to_menu_kb())
        return
    try:
        await client(UpdateProfileRequest(last_name=None if value == "-" else value))
    except errors.FloodWaitError as e:
        await message.answer(f"⏳ Слишком часто. Повтори через {e.seconds + 1} c.")
        return
    except errors.RPCError as e:
        await message.answer(f"❌ Telegram: <b>{type(e).__name__}</b>", reply_markup=keyboards.back_to_menu_kb())
        return
    await _after_save(message, state, manager)


@router.callback_query(ProfileStates.menu, F.data == keyboards.CB_PROFILE_ABOUT)
async def cb_prof_about(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileStates.waiting_about)
    await safe_edit(cb, texts.PROFILE_ASK_ABOUT, keyboards.cancel_kb())
    await cb.answer()


@router.message(ProfileStates.waiting_about, F.text)
async def prof_about(message: Message, state: FSMContext, manager: ClientManager, bot: Bot) -> None:
    value = message.text.strip()
    await safe_delete(bot, message.chat.id, message.message_id)
    if len(value) > 70:
        await message.answer(texts.PROFILE_ABOUT_BAD, reply_markup=keyboards.cancel_kb())
        return
    data = await state.get_data()
    client = manager.get(message.from_user.id, data.get("acc_id") or 0)
    if client is None:
        await message.answer(texts.PROFILE_NO_ACCOUNT_SEL, reply_markup=keyboards.back_to_menu_kb())
        return
    try:
        await client(UpdateProfileRequest(about=None if value == "-" else value))
    except errors.AboutTooLongError:
        await message.answer(texts.PROFILE_ABOUT_BAD, reply_markup=keyboards.cancel_kb())
        return
    except errors.FloodWaitError as e:
        await message.answer(f"⏳ Слишком часто. Повтори через {e.seconds + 1} c.")
        return
    except errors.RPCError as e:
        await message.answer(f"❌ Telegram: <b>{type(e).__name__}</b>", reply_markup=keyboards.back_to_menu_kb())
        return
    await _after_save(message, state, manager)


# ---------------------------------------------------------------------- юзернейм


@router.callback_query(ProfileStates.menu, F.data == keyboards.CB_PROFILE_USERNAME)
async def cb_prof_username(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileStates.waiting_username)
    await safe_edit(cb, texts.PROFILE_ASK_USERNAME, keyboards.cancel_kb())
    await cb.answer()


@router.message(ProfileStates.waiting_username, F.text)
async def prof_username(message: Message, state: FSMContext, manager: ClientManager, bot: Bot) -> None:
    value = message.text.strip().lstrip("@")
    await safe_delete(bot, message.chat.id, message.message_id)
    if not _USERNAME_RE.fullmatch(value):
        await message.answer(texts.PROFILE_USERNAME_BAD, reply_markup=keyboards.cancel_kb())
        return
    data = await state.get_data()
    client = manager.get(message.from_user.id, data.get("acc_id") or 0)
    if client is None:
        await message.answer(texts.PROFILE_NO_ACCOUNT_SEL, reply_markup=keyboards.back_to_menu_kb())
        return
    try:
        if data.get("username_raw") == value:
            await message.answer(texts.PROFILE_USERNAME_SAME, reply_markup=keyboards.cancel_kb())
            return
        await client(CheckUsernameRequest(value))
        await client(UpdateUsernameRequest(value))
    except (errors.UsernameOccupiedError, errors.UsernameInvalidError):
        await message.answer(texts.PROFILE_USERNAME_TAKEN, reply_markup=keyboards.cancel_kb())
        return
    except errors.UsernameNotModifiedError:
        pass  # уже установлен — считаем успехом
    except errors.FloodWaitError as e:
        await message.answer(f"⏳ Слишком часто. Повтори через {e.seconds + 1} c.")
        return
    except errors.RPCError as e:
        await message.answer(
            f"❌ Telegram: <b>{type(e).__name__}</b> (юзернейм мог не смениться)",
            reply_markup=keyboards.back_to_menu_kb(),
        )
        return
    await _after_save(message, state, manager)


# ---------------------------------------------------------------------- фото


@router.callback_query(ProfileStates.menu, F.data == keyboards.CB_PROFILE_PHOTO)
async def cb_prof_photo(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileStates.waiting_photo)
    await safe_edit(cb, texts.PROFILE_ASK_PHOTO, keyboards.cancel_kb())
    await cb.answer()


@router.message(ProfileStates.waiting_photo, F.photo)
async def prof_photo(message: Message, state: FSMContext, manager: ClientManager, bot: Bot) -> None:
    await safe_delete(bot, message.chat.id, message.message_id)
    data = await state.get_data()
    client = manager.get(message.from_user.id, data.get("acc_id") or 0)
    if client is None:
        await message.answer(texts.PROFILE_NO_ACCOUNT_SEL, reply_markup=keyboards.back_to_menu_kb())
        return
    buffer = io.BytesIO()
    await bot.download(message.photo[-1], destination=buffer)
    buffer.seek(0)
    buffer.name = "photo.jpg"
    try:
        uploaded = await client.upload_file(buffer)
        await client(UploadProfilePhotoRequest(file=uploaded))
    except errors.PhotoInvalidError:
        await message.answer("❌ Telegram не принял фото. Пришли другое или /cancel.",
                             reply_markup=keyboards.cancel_kb())
        return
    except errors.FloodWaitError as e:
        await message.answer(f"⏳ Слишком часто. Повтори через {e.seconds + 1} c.")
        return
    except errors.RPCError as e:
        await message.answer(f"❌ Telegram: <b>{type(e).__name__}</b>", reply_markup=keyboards.back_to_menu_kb())
        return
    await _after_save(message, state, manager)


@router.callback_query(ProfileStates.menu, F.data == keyboards.CB_PROFILE_DELPHOTO)
async def cb_prof_delphoto(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("has_photo", True):
        await cb.answer(texts.PROFILE_NO_PHOTO, show_alert=True)
        return
    await state.set_state(ProfileStates.waiting_delphoto)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data="prof:delphoto:yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data=keyboards.CB_PROFILE_BACK),
            ]
        ]
    )
    await safe_edit(cb, texts.PROFILE_ASK_DELPHOTO, kb)
    await cb.answer()


@router.callback_query(ProfileStates.waiting_delphoto, F.data == "prof:delphoto:yes")
async def cb_prof_delphoto_yes(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    data = await state.get_data()
    client = manager.get(cb.from_user.id, data.get("acc_id") or 0)
    if client is None:
        await cb.answer(texts.PROFILE_NO_ACCOUNT_SEL, show_alert=True)
        return
    photos = await client.get_profile_photos("me", limit=1)
    if not photos or not isinstance(photos[0], Photo):
        await cb.answer(texts.PROFILE_NO_PHOTO, show_alert=True)
        return
    photo: Photo = photos[0]
    try:
        await client(DeletePhotosRequest(id=[InputPhoto(photo.id, photo.access_hash, photo.file_reference)]))
    except errors.FloodWaitError as e:
        await cb.answer(f"⏳ Слишком часто. Повтори через {e.seconds + 1} c.", show_alert=True)
        return
    await _show_menu(cb, state, manager, cb.from_user.id, data.get("acc_id"))
    await cb.answer("🗑 Фото удалено")


@router.callback_query(ProfileStates.waiting_delphoto, F.data == keyboards.CB_PROFILE_BACK)
async def cb_prof_delphoto_no(cb: CallbackQuery, state: FSMContext, manager: ClientManager) -> None:
    data = await state.get_data()
    await _show_menu(cb, state, manager, cb.from_user.id, data.get("acc_id"))
    await cb.answer()


# ---------------------------------------------------------------------- общее


async def _after_save(message: Message, state: FSMContext, manager: ClientManager) -> None:
    data = await state.get_data()
    acc_id: Optional[int] = data.get("acc_id")
    client = manager.get(message.from_user.id, acc_id or 0)
    if client is None:
        await message.answer(texts.PROFILE_NO_ACCOUNT_SEL, reply_markup=keyboards.back_to_menu_kb())
        return
    p = await fetch_profile(client)
    await state.update_data(has_photo=p["has_photo"], username_raw=p["username_raw"] if p["username_raw"] else None)
    await state.set_state(ProfileStates.menu)
    await message.answer(
        texts.PROFILE_SAVED.format(info=_profile_text(p)),
        reply_markup=keyboards.profile_kb(p["has_photo"]),
    )


@router.callback_query(ProfileStates.menu, F.data.startswith("prof:"))
async def cb_profile_stale(cb: CallbackQuery) -> None:
    await cb.answer("Этот экран устарел — начни заново 🙂", show_alert=True)


@router.callback_query(F.data.startswith(keyboards.CB_ACCSEL_TOGGLE))
@router.callback_query(F.data == keyboards.CB_ACCSEL_DONE)
@router.callback_query(F.data == keyboards.CB_ACCSEL_ALL)
@router.callback_query(F.data == keyboards.CB_SEND_RUN)
async def stale_callbacks(cb: CallbackQuery) -> None:
    """Нажатие на кнопки устаревших экранов (роутер профиля подключается последним)."""
    await cb.answer("Этот экран устарел — начни заново 🙂", show_alert=True)


@router.message(ProfileStates.waiting_name)
@router.message(ProfileStates.waiting_last)
@router.message(ProfileStates.waiting_about)
@router.message(ProfileStates.waiting_username)
@router.message(ProfileStates.waiting_photo)
async def profile_fallback(message: Message) -> None:
    await message.answer(texts.NEED_TEXT, reply_markup=keyboards.cancel_kb())
