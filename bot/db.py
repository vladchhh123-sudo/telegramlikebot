"""SQLite-хранилище: загруженные аккаунты (мульти-аккаунт, без лимитов).

Схема v2: одна строка на загруженный аккаунт, привязка к владельцу панели
(owner_bot_user_id). Автоматическая миграция со старой схемы v1
(один аккаунт на пользователя, PK bot_user_id).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_bot_user_id INTEGER NOT NULL,
    phone            TEXT    NOT NULL,
    session          TEXT    NOT NULL,
    account_user_id  INTEGER,
    account_name     TEXT,
    created_at       TEXT    DEFAULT (datetime('now')),
    updated_at       TEXT    DEFAULT (datetime('now'))
);
"""
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_bot_user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_owner_acc ON accounts(owner_bot_user_id, account_user_id)",
]

# История инвайтов: чтобы повторные списки скипали уже попробованных
_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS invite_history (
    owner_bot_user_id INTEGER NOT NULL,
    chat_key          TEXT    NOT NULL,
    token             TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    detail            TEXT    DEFAULT '',
    created_at        TEXT    DEFAULT (datetime('now')),
    updated_at        TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (owner_bot_user_id, chat_key, token)
);
"""

# KV-настройки (врата подписки и прочие флаги)
_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Пропуска «врат подписки»: кто подавал заявку в канал
_GATE_TABLE = """
CREATE TABLE IF NOT EXISTS gate_pass (
    channel_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (channel_id, user_id)
);
"""

# Общий кэш профилей: (владелец, user_id) → access_hash.
# Парсинг запоминает профили, инвайтинг использует их для инвайта по голому ID —
# работает между разными аккаунтами одной панели.
_ENTITY_TABLE = """
CREATE TABLE IF NOT EXISTS entity_cache (
    owner_bot_user_id INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    access_hash       INTEGER NOT NULL,
    updated_at        TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (owner_bot_user_id, user_id)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._migrate_if_needed()
        await self._conn.execute(_SCHEMA)
        for idx in _INDEXES:
            await self._conn.execute(idx)
        await self._conn.execute(_HISTORY_TABLE)
        await self._conn.execute(_SETTINGS_TABLE)
        await self._conn.execute(_GATE_TABLE)
        await self._conn.execute(_ENTITY_TABLE)
        await self._conn.commit()

    async def _migrate_if_needed(self) -> None:
        """v1 → v2: таблица с PK bot_user_id переезжает на owner_bot_user_id."""
        async with self._conn.execute("PRAGMA table_info(accounts)") as cur:
            columns = [row["name"] for row in await cur.fetchall()]
        if not columns or "bot_user_id" not in columns or "owner_bot_user_id" in columns:
            return
        await self._conn.execute("ALTER TABLE accounts RENAME TO accounts_v1_old")
        await self._conn.execute(_SCHEMA)
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO accounts
                (owner_bot_user_id, phone, session, account_user_id, account_name, created_at, updated_at)
            SELECT bot_user_id, phone, session, account_user_id, account_name, created_at, updated_at
            FROM accounts_v1_old
            """
        )
        await self._conn.execute("DROP TABLE accounts_v1_old")
        for idx in _INDEXES:
            await self._conn.execute(idx)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не открыта: сначала вызовите Database.connect().")
        return self._conn

    # ------------------------------------------------------------------ аккаунты

    async def save_account(
        self,
        owner_bot_user_id: int,
        phone: str,
        session: str,
        account_user_id: Optional[int],
        account_name: Optional[str],
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO accounts (owner_bot_user_id, phone, session, account_user_id, account_name, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(owner_bot_user_id, account_user_id) DO UPDATE SET
                phone = excluded.phone,
                session = excluded.session,
                account_name = excluded.account_name,
                updated_at = datetime('now')
            """,
            (owner_bot_user_id, phone, session, account_user_id, account_name),
        )
        await self.conn.commit()

    async def list_accounts(self, owner_bot_user_id: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM accounts WHERE owner_bot_user_id = ? ORDER BY id",
            (owner_bot_user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_account(self, owner_bot_user_id: int, account_user_id: int) -> Optional[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM accounts WHERE owner_bot_user_id = ? AND account_user_id = ?",
            (owner_bot_user_id, account_user_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_account_by_phone(self, owner_bot_user_id: int, phone: str) -> int:
        """Удаляет ВСЕ строки аккаунта владельца с этим телефоном (дубели, строки без id)."""
        cur = await self.conn.execute(
            "DELETE FROM accounts WHERE owner_bot_user_id = ? AND phone = ?",
            (owner_bot_user_id, phone),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def delete_account(self, owner_bot_user_id: int, account_user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM accounts WHERE owner_bot_user_id = ? AND account_user_id = ?",
            (owner_bot_user_id, account_user_id),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ история инвайтов

    async def save_invite_history(
        self, owner_bot_user_id: int, chat_key: str, results: list[tuple[str, str, str]]
    ) -> int:
        """Сохраняет результаты запуска: (токен, статус, причина). Возвращает кол-во записей."""
        saved = 0
        for token, status, detail in results:
            key = (token or "").strip().lower()
            if not key:
                continue
            await self.conn.execute(
                """
                INSERT INTO invite_history (owner_bot_user_id, chat_key, token, status, detail, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(owner_bot_user_id, chat_key, token) DO UPDATE SET
                    status = excluded.status,
                    detail = excluded.detail,
                    updated_at = datetime('now')
                """,
                (owner_bot_user_id, chat_key, key, status, detail),
            )
            saved += 1
        await self.conn.commit()
        return saved

    async def get_invite_history(self, owner_bot_user_id: int, chat_key: str) -> dict[str, str]:
        """{токен: статус последней попытки} по чату."""
        async with self.conn.execute(
            "SELECT token, status FROM invite_history WHERE owner_bot_user_id = ? AND chat_key = ?",
            (owner_bot_user_id, chat_key),
        ) as cur:
            rows = await cur.fetchall()
        return {row["token"]: row["status"] for row in rows}

    # ------------------------------------------------------------------ настройки (KV)

    async def get_setting(self, key: str) -> Optional[str]:
        async with self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ врата подписки

    async def add_gate_pass(self, channel_id: int, user_id: int) -> bool:
        """Фиксирует заявку. True — записан впервые, False — уже был."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO gate_pass (channel_id, user_id) VALUES (?, ?)",
            (channel_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def has_gate_pass(self, channel_id: int, user_id: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM gate_pass WHERE channel_id = ? AND user_id = ?",
            (channel_id, user_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def remove_gate_pass(self, channel_id: int, user_id: int) -> None:
        """Убирает пропуск (человек отписался от канала — снова через врата)."""
        await self.conn.execute(
            "DELETE FROM gate_pass WHERE channel_id = ? AND user_id = ?",
            (channel_id, user_id),
        )
        await self.conn.commit()

    async def clear_gate_pass(self, channel_id: int) -> int:
        """Удаляет все пропуска канала (для повторного теста). Возвращает сколько стёрли."""
        cur = await self.conn.execute("DELETE FROM gate_pass WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()
        return cur.rowcount or 0

    async def gate_pass_count(self, channel_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM gate_pass WHERE channel_id = ?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ кэш профилей (access_hash)

    async def save_entities(self, owner_bot_user_id: int, pairs) -> int:
        """Сохраняет (user_id, access_hash). Возвращает кол-во записанных."""
        clean = [(int(u), int(h)) for u, h in pairs if u and h is not None]
        if not clean:
            return 0
        await self.conn.executemany(
            "INSERT INTO entity_cache (owner_bot_user_id, user_id, access_hash) VALUES (?, ?, ?) "
            "ON CONFLICT(owner_bot_user_id, user_id) DO UPDATE SET "
            "access_hash = excluded.access_hash, updated_at = datetime('now')",
            [(owner_bot_user_id, u, h) for u, h in clean],
        )
        await self.conn.commit()
        return len(clean)

    async def get_entity_hashes(self, owner_bot_user_id: int, user_ids) -> dict[int, int]:
        """{user_id: access_hash} из общего кэша панели."""
        ids = [int(u) for u in user_ids if u]
        out: dict[int, int] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            async with self.conn.execute(
                f"SELECT user_id, access_hash FROM entity_cache "
                f"WHERE owner_bot_user_id = ? AND user_id IN ({marks})",
                [owner_bot_user_id, *chunk],
            ) as cur:
                for row in await cur.fetchall():
                    out[int(row["user_id"])] = int(row["access_hash"])
        return out
