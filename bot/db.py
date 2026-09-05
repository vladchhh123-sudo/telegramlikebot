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

    async def delete_account(self, owner_bot_user_id: int, account_user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM accounts WHERE owner_bot_user_id = ? AND account_user_id = ?",
            (owner_bot_user_id, account_user_id),
        )
        await self.conn.commit()
