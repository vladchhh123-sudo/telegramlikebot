"""Smoke-тест без сети: сборка, конфиг, БД (+миграция v1→v2), клавиатуры, парсеры, реестр задач."""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test-hash")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "smoke_bot.db"))

from bot.config import load_config
from bot.db import Database
from bot.registry import TaskRegistry
from bot.tg_client import ClientManager
from bot import keyboards, texts
from bot.services.base import (
    chat_ref_from_tg_id,
    normalize_emoji,
    parse_chat_input,
    parse_chat_input_list,
    parse_message_link,
)
from bot.services import invite_service, parse_service, react_service, send_service, stories_service
from bot.services.excel import build_report_xlsx, build_users_xlsx, extract_users_from_file
from bot.services.base import parse_invite_token, progress_line
from bot.handlers import invite, login, messages, parse, profile, reactions, start, stories as stories_h  # noqa: F401


def check_links() -> None:
    cases = {
        "@durov": ("username", "durov"),
        "https://t.me/telegram": ("username", "telegram"),
        "t.me/+AbCdEf12345": ("invite", "AbCdEf12345"),
        "-1001234567890": ("id", "-1001234567890"),
        "привет": None,
        "": None,
    }
    for raw, expected in cases.items():
        ref = parse_chat_input(raw)
        got = (ref.kind, ref.value) if ref else None
        assert got == expected, f"parse_chat_input({raw!r}) = {got}"

    multi = "@durov\nhttps://t.me/telegram\nt.me/+AbCdEf12345, -1001234567890\n@durov"
    kinds = [(r.kind, r.value) for r in parse_chat_input_list(multi)]
    assert kinds == [
        ("username", "durov"),
        ("username", "telegram"),
        ("invite", "AbCdEf12345"),
        ("id", "-1001234567890"),
    ], kinds
    assert parse_chat_input_list("мусор") == []

    ref, mid = parse_message_link("https://t.me/telegram/12345")
    assert (ref.kind, ref.value, mid) == ("username", "telegram", 12345)
    ref, mid = parse_message_link("t.me/c/2226319014/987")
    assert (ref.kind, ref.value, mid) == ("id", "2226319014", 987)
    assert parse_message_link("t.me/telegram") is None

    ref = chat_ref_from_tg_id(-1001234567890)
    assert (ref.kind, ref.value) == ("id", "1234567890")

    # токены инвайтинга
    assert parse_invite_token("@durov") == "@durov"
    assert parse_invite_token("https://t.me/durov") == "@durov"
    assert parse_invite_token("123456789") == "123456789"
    assert parse_invite_token("+79001234567") == "+79001234567"
    assert parse_invite_token("просто слово") is None
    assert parse_invite_token("") is None
    print("  parsers: OK")


def check_excel() -> None:
    xlsx = build_users_xlsx([
        {"id": 1, "name": "Иван", "username": "@ivan_t", "last_seen": "в сети", "photo": True, "source": "чят"},
        {"id": 2, "name": "Мария", "username": "—", "last_seen": "5 мин назад", "photo": False, "source": "чят"},
    ])
    assert xlsx.startswith(b"PK")
    users = extract_users_from_file("list.xlsx", xlsx)
    assert users == ["@ivan_t"], users

    csv_data = "@foo12\n123456789\nhttps://t.me/bar12\n".encode()
    users2 = extract_users_from_file("list.csv", csv_data)
    assert users2 == ["@foo12", "123456789", "@bar12"], users2

    report = build_report_xlsx([("@a", "✅ добавлен", ""), ("@b", "ошибка", "PeerFlood")])
    assert report.startswith(b"PK")

    assert "12/50" in progress_line(12, 50, 1.0)
    assert progress_line(0, 50, 1.0)
    print("  excel + progress_line: OK")


def check_emoji() -> None:
    assert normalize_emoji("  ❤️ ") == "❤"
    assert not normalize_emoji("   ")
    print("  normalize_emoji: OK")


def check_keyboards() -> None:
    infos = [
        {"id": 111, "name": "Акк Один", "phone": "+79001112233", "logged": True},
        {"id": 222, "name": "Акк Два", "phone": "+79004445566", "logged": True},
    ]
    for kb in (
        keyboards.main_menu(True),
        keyboards.main_menu(False),
        keyboards.cancel_kb(),
        keyboards.accounts_multiselect_kb(infos, {111}),
        keyboards.accounts_manage_kb(infos),
        keyboards.account_detail_kb(111),
        keyboards.account_logout_confirm_kb(111),
        keyboards.emoji_multiselect_kb(["👍", "🔥"]),
        keyboards.react_confirm_kb(["👍", "❤"], include_own=False, live=True, limit=0, age_hours=24),
        keyboards.stories_mode_kb(0),
        keyboards.send_confirm_kb(),
        keyboards.profile_kb(True),
        keyboards.profile_kb(False),
        keyboards.running_kb("r1"),
        keyboards.tasks_kb(["r1", "s2", "s3"]),
        keyboards.back_to_menu_kb(),
    ):
        assert kb.inline_keyboard
    # в пикере эмодзи есть кнопка «🎲 Случайный набор» и полный набор реакций
    random_btn = [
        b
        for row in keyboards.emoji_multiselect_kb([]).inline_keyboard
        for b in row
        if b.callback_data == keyboards.CB_REACT_RANDOM_SET
    ]
    assert random_btn, "нет кнопки случайного набора"
    assert len(keyboards.PRESET_REACTIONS) >= 30
    for kb in (
        keyboards.emoji_multiselect_kb(list(keyboards.PRESET_REACTIONS)),
        keyboards.accounts_multiselect_kb(infos, {111, 222}),
        keyboards.react_confirm_kb(["👍"], include_own=False, live=False, limit=1000, age_hours=0),
        keyboards.running_kb("r999"),
    ):
        for row in kb.inline_keyboard:
            for btn in row:
                assert len((btn.callback_data or "").encode()) <= 64, btn.callback_data
    print("  keyboards: OK")


def check_renders() -> None:
    s = react_service.ReactStats(
        emojis=["👍", "🔥"], seen=10, limit=100, reacted=7, live=True,
        chats_total=3, chats_done=2, chats_failed=1,
    )
    assert react_service.render_react_progress(s)
    assert react_service.render_react_summary(s)
    assert react_service.short_react(s)
    s0 = react_service.ReactStats(emojis=["👍"], seen=10, limit=0, skipped_old=3, chats_total=1, chats_done=1)
    assert "∞" in react_service.render_react_progress(s0)
    assert "старые 3" in react_service.render_react_progress(s0)

    st = stories_service.StoriesStats(
        peers_seen=5, peers_with_stories=2, stories_found=3, liked=3,
        chats_total=2, chats_done=1, hidden_chats=1,
    )
    assert "🙈" in stories_service.render_stories_summary(st)
    assert stories_service.render_stories_progress(st)
    assert stories_service.short_stories(st)

    sd = send_service.SendStats(chats_total=3, chats_done=2, sent=2)
    assert send_service.render_send_progress(sd)
    assert send_service.render_send_summary(sd)
    assert send_service.short_send(sd)

    ps = parse_service.ParseStats(chats_total=2, chats_done=1, users_found=42, raw_found=100)
    assert parse_service.render_parse_progress(ps)
    assert parse_service.render_parse_summary(ps)
    assert parse_service.short_parse(ps)

    iv = invite_service.InviteStats(total=10, processed=4, invited=3, failed=1, total_units=10)
    assert invite_service.render_invite_progress(iv)
    assert invite_service.render_invite_summary(iv)
    assert invite_service.short_invite(iv)

    assert texts.REACT_CONFIRM.format(
        accounts=2, chat_list="• x\n", count=2, emojis="👍🔥", limit="♾ без лимита",
        age="последние 24 часа", own="✅ лайкать", live="⚡️ ВКЛ", delay_lo=0.8, delay_hi=1.6,
    )
    assert texts.SEND_CONFIRM.format(
        accounts=1, count=2, chat_list="• x\n", preview="привет", photo="нет", delay_lo=3.0, delay_hi=7.0,
    )
    assert texts.PROFILE_VIEW.format(
        name="A", last="B", username="@c", about="d", photo="нет", phone="+7",
    )
    assert texts.HELP.format(max_concurrent=5)
    assert texts.MENU.format(n=3)
    assert texts.LOGOUT_DONE.format(name="x")
    assert texts.VERSION
    print("  renders: OK")


async def check_db_migration() -> None:
    import aiosqlite

    path = os.path.join(tempfile.gettempdir(), "migrate_bot.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    # старая схема v1
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "CREATE TABLE accounts (bot_user_id INTEGER PRIMARY KEY, phone TEXT, session TEXT, "
        "account_user_id INTEGER, account_name TEXT, created_at TEXT, updated_at TEXT)"
    )
    await conn.execute(
        "INSERT INTO accounts (bot_user_id, phone, session, account_user_id, account_name) "
        "VALUES (7, '+7900', 'sess', 777, 'Old')"
    )
    await conn.commit()
    await conn.close()

    db = Database(path)
    await db.connect()
    rows = await db.list_accounts(7)
    assert len(rows) == 1 and rows[0]["account_user_id"] == 777 and rows[0]["owner_bot_user_id"] == 7
    await db.save_account(7, "+7900", "sess2", 777, "Old2")   # upsert
    await db.save_account(7, "+7901", "sess3", 888, "New")    # второй аккаунт — лимитов нет
    assert len(await db.list_accounts(7)) == 2
    assert (await db.get_account(7, 888))["account_name"] == "New"
    await db.delete_account(7, 777)
    assert len(await db.list_accounts(7)) == 1
    await db.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
    print("  db migration v1→v2: OK")


async def check_registry() -> None:
    reg = TaskRegistry(max_concurrent=1)

    async def job_a(info):
        await asyncio.sleep(0.05)
        info.state = "done"

    async def job_b(info):
        info.state = "error"
        info.error = "тестовая ошибка"

    async def job_stuck(info):
        await asyncio.sleep(10)

    info_a = reg.start(1, job_a, kind="react", chat="2 чата", detail="👍", account_name="Акк1")
    info_b = reg.start(1, job_b, kind="stories", chat="1 чат", detail="стории 👀", account_name="Акк2")
    info_stuck = reg.start(1, job_stuck, kind="send", chat="3 чата", detail="✉️", account_name="Акк3")

    assert info_a and info_b and info_stuck
    assert reg.active_count(1) == 3
    await asyncio.sleep(0.15)
    assert info_a.state == "done"
    assert info_b.state == "error" and info_b.error
    assert info_stuck.state in ("running", "done")
    assert "Акк1" in info_a.status_line()

    assert reg.stop(1, info_stuck.task_id) is True
    await asyncio.sleep(0.05)
    assert info_stuck.state == "stopped"
    assert reg.stop(1, "нет-такого") is False
    assert reg.clear_finished(1) >= 2
    print("  registry: OK")


async def check_manager() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    await db.connect()
    manager = ClientManager(cfg.api_id, cfg.api_hash, db)
    assert await manager.restore_all() == 0
    assert manager.is_logged_in(42) is False
    assert manager.accounts_count(42) == 0
    assert manager.get(42, 1) is None
    assert await manager.account_info(42, 1) is None
    assert await manager.list_account_infos(42) == []
    await db.close()
    print("  manager: OK")


def check_dispatcher() -> None:
    cfg = load_config()
    from run import build_dispatcher

    db = Database(cfg.db_path)
    manager = ClientManager(cfg.api_id, cfg.api_hash, db)
    dp = build_dispatcher(cfg, db, manager, TaskRegistry(max_concurrent=cfg.max_concurrent_tasks))
    assert dp is not None
    print("  dispatcher: OK")


def main() -> None:
    print("SMOKE TEST")
    check_links()
    check_emoji()
    check_keyboards()
    check_renders()
    check_excel()
    asyncio.run(check_db_migration())
    asyncio.run(check_registry())
    asyncio.run(check_manager())
    check_dispatcher()
    print("ALL OK")


if __name__ == "__main__":
    main()
