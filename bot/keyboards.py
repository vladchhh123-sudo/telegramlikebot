"""Инлайн-клавиатуры и callback_data бота (v3: мульти-аккаунт)."""
from __future__ import annotations

from typing import Any, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# ---- callback_data ----
CB_MAIN = "menu:main"
CB_LOGIN_START = "menu:login"
CB_ACCOUNTS = "menu:accounts"           # 🧩 менеджмент аккаунтов
CB_ACCOUNT_VIEW = "acct:view:"          # + account_user_id
CB_ACCOUNT_LOGOUT = "acct:del:"         # + account_user_id (подтверждение)
CB_ACCOUNT_LOGOUT_YES = "acct:delyes:"  # + account_user_id
CB_LOGIN_CANCEL = "login:cancel"
CB_FLOW_REACT = "flow:react"
CB_FLOW_STORIES = "flow:stories"
CB_FLOW_SEND = "flow:send"
CB_FLOW_PROFILE = "flow:profile"
CB_TASKS = "menu:tasks"
CB_TASK_STOP_ID = "task:stopid:"     # + id задачи
CB_TASKS_STOP_ALL = "tasks:stopall"
CB_TASKS_REFRESH = "tasks:refresh"
CB_TASKS_CLEAR = "tasks:clear"
# выбор аккаунтов при создании задачи
CB_ACCSEL_TOGGLE = "acctsel:toggle:"   # + account_user_id
CB_ACCSEL_DONE = "acctsel:done"
CB_ACCSEL_ALL = "acctsel:all"
# реакции
CB_REACT_EMOJI = "react:emoji:"      # + эмодзи (переключатель)
CB_REACT_EMOJIS_DONE = "react:emojis_done"
CB_REACT_RANDOM_SET = "react:random"  # 🎲 выбрать случайный набор
CB_REACT_CUSTOM = "react:custom"
CB_REACT_RUN = "react:run"
CB_REACT_LIMIT = "react:limit:"      # + число (0 = ♾)
CB_REACT_LIMIT_CUSTOM = "react:limit:custom"
CB_REACT_OWN = "react:own"
CB_REACT_LIVE = "react:live"
CB_REACT_AGE = "react:age"
# стории
CB_STORY_PEERS = "stories:peers:"    # + число (0 = ♾)
CB_STORY_PEERS_CUSTOM = "stories:peers:custom"
CB_STORY_MODE = "stories:mode:"      # + view_like|view
CB_STORY_MODE_WITH_LIKE = "stories:mode:view_like"
CB_STORY_MODE_VIEW = "stories:mode:view"
# парсинг
CB_FLOW_PARSE = "flow:parse"
CB_PARSE_SEEN = "parse:seen:"        # + часы (0 = любой)
CB_PARSE_PHOTO = "parse:photo:"      # any|yes|no
CB_PARSE_USERNAME = "parse:username:"  # any|yes|no
CB_PARSE_PEERS = "parse:peers:"      # + число (0 = все)
CB_PARSE_PEERS_CUSTOM = "parse:peers:custom"
CB_PARSE_SCAN = "parse:scan:"        # + число сообщений для скрытых чатов
CB_PARSE_SCAN_CUSTOM = "parse:scan:custom"
CB_PARSE_RUN = "parse:run"
# инвайтинг
CB_FLOW_INVITE = "flow:invite"
CB_INV_DELAY = "inv:delay:"          # + индекс пресета
CB_INV_BATCH = "inv:batch:"          # + размер пачки (0 = без пачек)
CB_INV_DAILY = "inv:daily:"          # + дневной лимит (0 = ∞)
CB_INV_RUN = "inv:run"
# сообщения
CB_SEND_RUN = "send:run"
# профиль
CB_PROFILE_REFRESH = "prof:refresh"
CB_PROFILE_NAME = "prof:name"
CB_PROFILE_LAST = "prof:last"
CB_PROFILE_ABOUT = "prof:about"
CB_PROFILE_USERNAME = "prof:username"
CB_PROFILE_PHOTO = "prof:photo"
CB_PROFILE_DELPHOTO = "prof:delphoto"
CB_PROFILE_BACK = "prof:back"

# Каноничные эмодзи-реакции Telegram (полный официальный набор)
PRESET_REACTIONS = [
    "👍", "👎", "❤️", "🔥",
    "🥰", "👏", "😁", "🤔",
    "🤯", "😱", "🤬", "😢",
    "🎉", "🤩", "🤮", "💩",
    "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳",
    "❤‍🔥", "🌚", "🌭", "💯",
    "🤣", "⚡", "🍌", "🤨",
    "😐", "🫡", "🍓", "🍾",
    "🗿",
]

# Возможные фильтры свежести сообщений (часы; 0 = без ограничения)
AGE_CHOICES = [0, 24, 72, 168]


def _age_label(hours: int) -> str:
    return {0: "♾ всё время", 24: "🕐 24 часа", 72: "🕐 3 дня", 168: "🕐 7 дней"}.get(hours, f"🕐 {hours} ч")


def age_label(hours: int) -> str:
    return _age_label(hours)


def main_menu(has_account: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_account:
        rows.append([InlineKeyboardButton(text="💬 Реакции в чате", callback_data=CB_FLOW_REACT)])
        rows.append([InlineKeyboardButton(text="📸 Стории: смотреть + лайкать", callback_data=CB_FLOW_STORIES)])
        rows.append([InlineKeyboardButton(text="✉️ Сообщения в чаты", callback_data=CB_FLOW_SEND)])
        rows.append([
            InlineKeyboardButton(text="🕵️ Парсинг участников", callback_data=CB_FLOW_PARSE),
            InlineKeyboardButton(text="📨 Инвайтинг", callback_data=CB_FLOW_INVITE),
        ])
        rows.append([
            InlineKeyboardButton(text="👤 Профиль аккаунта", callback_data=CB_FLOW_PROFILE),
            InlineKeyboardButton(text="🧩 Аккаунты", callback_data=CB_ACCOUNTS),
        ])
        rows.append([InlineKeyboardButton(text="📋 Мои задачи", callback_data=CB_TASKS)])
    else:
        rows.append([InlineKeyboardButton(text="➕ Загрузить аккаунт", callback_data=CB_LOGIN_START)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=CB_LOGIN_CANCEL)]]
    )


# ------------------------------------------------------------------ выбор аккаунтов


def _acc_label(info: dict[str, Any], selected: bool) -> str:
    mark = "✔️ " if selected else "▫️ "
    return f"{mark}{info['name']} ({info['phone']})"


def accounts_multiselect_kb(infos: Sequence[dict[str, Any]], selected_ids: set[int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_acc_label(info, info["id"] in selected_ids),
                              callback_data=CB_ACCSEL_TOGGLE + str(info["id"]))]
        for info in infos
    ]
    rows.append(
        [
            InlineKeyboardButton(text=f"✅ Готово ({len(selected_ids)})", callback_data=CB_ACCSEL_DONE),
            InlineKeyboardButton(text="🎵 Выбрать все", callback_data=CB_ACCSEL_ALL),
        ]
    )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=CB_LOGIN_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------------ эмодзи-реакции


def emoji_multiselect_kb(selected: Sequence[str]) -> InlineKeyboardMarkup:
    """Мультивыбор эмодзи: тап по кнопке переключает выбор (✔️), потом «Готово».

    На каждое сообщение будет ставиться случайная из выбранных (без повторов подряд).
    """
    chosen = set(selected)
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(PRESET_REACTIONS), 6):
        row = []
        for emoji in PRESET_REACTIONS[i : i + 6]:
            label = f"✔️{emoji}" if emoji in chosen else emoji
            row.append(InlineKeyboardButton(text=label, callback_data=CB_REACT_EMOJI + emoji))
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="🎲 Случайный набор", callback_data=CB_REACT_RANDOM_SET),
            InlineKeyboardButton(text=f"✅ Готово ({len(chosen)})", callback_data=CB_REACT_EMOJIS_DONE),
        ]
    )
    rows.append([InlineKeyboardButton(text="✍️ Своя эмодзи", callback_data=CB_REACT_CUSTOM)])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=CB_LOGIN_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def react_confirm_kb(
    emojis: Sequence[str],
    *,
    include_own: bool,
    live: bool,
    limit: int,          # 0 = ♾
    age_hours: int,
) -> InlineKeyboardMarkup:
    own_label = "👤 Свои: ✅" if include_own else "👤 Свои: ❌"
    live_label = "⚡️ Авто-режим: ВКЛ" if live else "⚡️ Авто-режим: выкл"
    limit_label = "♾ без лимита" if limit == 0 else str(limit)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"▶️ Запустить ({''.join(emojis)})", callback_data=CB_REACT_RUN)],
            [
                InlineKeyboardButton(text="100", callback_data=CB_REACT_LIMIT + "100"),
                InlineKeyboardButton(text="500", callback_data=CB_REACT_LIMIT + "500"),
                InlineKeyboardButton(text="1000", callback_data=CB_REACT_LIMIT + "1000"),
                InlineKeyboardButton(text="♾", callback_data=CB_REACT_LIMIT + "0"),
                InlineKeyboardButton(text="✍️ свой", callback_data=CB_REACT_LIMIT_CUSTOM),
            ],
            [
                InlineKeyboardButton(text=f"🔢 Лимит: {limit_label}", callback_data=CB_REACT_LIMIT_CUSTOM),
                InlineKeyboardButton(text=_age_label(age_hours), callback_data=CB_REACT_AGE),
            ],
            [
                InlineKeyboardButton(text=own_label, callback_data=CB_REACT_OWN),
                InlineKeyboardButton(text=live_label, callback_data=CB_REACT_LIVE),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CB_LOGIN_CANCEL)],
        ]
    )


# ------------------------------------------------------------------ стории


def stories_mode_kb(peer_limit: int) -> InlineKeyboardMarkup:
    peers_label = "♾ все" if peer_limit == 0 else str(peer_limit)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 300", callback_data=CB_STORY_PEERS + "300"),
                InlineKeyboardButton(text="👥 1000", callback_data=CB_STORY_PEERS + "1000"),
                InlineKeyboardButton(text="♾ все", callback_data=CB_STORY_PEERS + "0"),
                InlineKeyboardButton(text="✍️ свой", callback_data=CB_STORY_PEERS_CUSTOM),
            ],
            [
                InlineKeyboardButton(
                    text=f"👀❤️ Смотреть + лайкать ({peers_label})", callback_data=CB_STORY_MODE_WITH_LIKE
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"👀 Только посмотреть ({peers_label})", callback_data=CB_STORY_MODE_VIEW
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CB_LOGIN_CANCEL)],
        ]
    )


# ------------------------------------------------------------------ сообщения


def send_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Запустить рассылку", callback_data=CB_SEND_RUN)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CB_LOGIN_CANCEL)],
        ]
    )


# ------------------------------------------------------------------ парсинг

SEEN_CHOICES = [0, 24, 72, 168, 720]
PEER_CHOICES = [0, 200, 500, 1000, 5000]
SCAN_CHOICES = [100, 200, 500, 1000, 2000, 5000]


def _seen_label(hours: int) -> str:
    return {0: "♾ любой", 24: "🕐 24 ч", 72: "🕐 3 дня", 168: "🕐 7 дней", 720: "🕐 30 дней"}.get(hours, f"🕐 {hours} ч")


def parse_criteria_kb(seen_hours: int, photo: str, username: str, peer_limit: int, scan_limit: int) -> InlineKeyboardMarkup:
    photo_label = {"any": "🖼 Аватар: все", "yes": "🖼 Аватар: только ЕСТЬ", "no": "🖼 Аватар: только НЕТ"}[photo]
    username_label = {"any": "@ Юзернейм: все", "yes": "@ Юзернейм: только ЕСТЬ", "no": "@ Юзернейм: только НЕТ"}[username]
    peers_label = "♾ все" if peer_limit == 0 else str(peer_limit)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"👤 Последний визит: {_seen_label(seen_hours)}", callback_data=CB_PARSE_SEEN + str(seen_hours))],
            [InlineKeyboardButton(text=photo_label, callback_data=CB_PARSE_PHOTO + photo)],
            [InlineKeyboardButton(text=username_label, callback_data=CB_PARSE_USERNAME + username)],
            [
                InlineKeyboardButton(text=f"👥 Лимит/чат: {peers_label}", callback_data=CB_PARSE_PEERS + str(peer_limit)),
                InlineKeyboardButton(text="✍️", callback_data=CB_PARSE_PEERS_CUSTOM),
            ],
            [
                InlineKeyboardButton(text=f"💬 Сообщений (скрытые): {scan_limit}", callback_data=CB_PARSE_SCAN + str(scan_limit)),
                InlineKeyboardButton(text="✍️", callback_data=CB_PARSE_SCAN_CUSTOM),
            ],
            [InlineKeyboardButton(text=f"▶️ Запустить парсинг", callback_data=CB_PARSE_RUN)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CB_LOGIN_CANCEL)],
        ]
    )


# ------------------------------------------------------------------ инвайтинг

INVITE_DELAY_PRESETS = [(15, 30), (30, 60), (60, 120), (120, 240)]
INVITE_BATCH_CHOICES = [3, 5, 10, 20, 0]
INVITE_DAILY_CHOICES = [20, 50, 100, 200, 0]


def invite_settings_kb(delay_idx: int, batch: int, daily: int) -> InlineKeyboardMarkup:
    lo, hi = INVITE_DELAY_PRESETS[delay_idx]
    batch_label = "📦 Пачка: " + ("выкл" if batch == 0 else str(batch))
    daily_label = "🚦 Лимит/день: " + ("♾" if daily == 0 else str(daily))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⏱ Пауза: {lo}–{hi} c", callback_data=CB_INV_DELAY + str(delay_idx))],
            [
                InlineKeyboardButton(text=batch_label, callback_data=CB_INV_BATCH + str(batch)),
                InlineKeyboardButton(text=daily_label, callback_data=CB_INV_DAILY + str(daily)),
            ],
            [InlineKeyboardButton(text="▶️ Запустить инвайтинг", callback_data=CB_INV_RUN)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=CB_LOGIN_CANCEL)],
        ]
    )


# ------------------------------------------------------------------ профиль


def profile_kb(has_photo: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✏️ Имя", callback_data=CB_PROFILE_NAME),
            InlineKeyboardButton(text="✏️ Фамилия", callback_data=CB_PROFILE_LAST),
        ],
        [InlineKeyboardButton(text="📝 Описание (био)", callback_data=CB_PROFILE_ABOUT)],
        [InlineKeyboardButton(text="@ Юзернейм", callback_data=CB_PROFILE_USERNAME)],
        [
            InlineKeyboardButton(text="🖼 Сменить фото", callback_data=CB_PROFILE_PHOTO),
            InlineKeyboardButton(
                text="🗑 Удалить фото" if has_photo else "🗑 Фото нет", callback_data=CB_PROFILE_DELPHOTO
            ),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=CB_PROFILE_REFRESH),
            InlineKeyboardButton(text="⬅️ В меню", callback_data=CB_MAIN),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------------ задачи / аккаунты / прочее


def running_kb(task_id: str) -> InlineKeyboardMarkup:
    """Кнопка остановки конкретной задачи (в её сообщении-прогрессе)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⏹ Остановить ({task_id})", callback_data=CB_TASK_STOP_ID + task_id)]
        ]
    )


def tasks_kb(active_task_ids: list[str]) -> InlineKeyboardMarkup:
    """Экран «Мои задачи»: кнопки остановки активных + управление списком."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(active_task_ids), 3):
        rows.append(
            [
                InlineKeyboardButton(text=f"⏹ {task_id}", callback_data=CB_TASK_STOP_ID + task_id)
                for task_id in active_task_ids[i : i + 3]
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="⏹ Остановить все", callback_data=CB_TASKS_STOP_ALL),
            InlineKeyboardButton(text="🗑 Очистить историю", callback_data=CB_TASKS_CLEAR),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=CB_TASKS_REFRESH),
            InlineKeyboardButton(text="⬅️ В меню", callback_data=CB_MAIN),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def accounts_manage_kb(infos: Sequence[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Экран «🧩 Аккаунты»: список + добавление."""
    rows = [
        [InlineKeyboardButton(text=f"👤 {info['name']} ({info['phone']})",
                              callback_data=CB_ACCOUNT_VIEW + str(info["id"]))]
        for info in infos
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data=CB_LOGIN_START)])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data=CB_MAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_detail_kb(account_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚪 Выйти из этого аккаунта", callback_data=CB_ACCOUNT_LOGOUT + str(account_user_id))],
            [InlineKeyboardButton(text="⬅️ К списку аккаунтов", callback_data=CB_ACCOUNTS)],
        ]
    )


def account_logout_confirm_kb(account_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, выйти", callback_data=CB_ACCOUNT_LOGOUT_YES + str(account_user_id)),
                InlineKeyboardButton(text="❌ Нет", callback_data=CB_ACCOUNTS),
            ]
        ]
    )


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data=CB_MAIN)]]
    )
