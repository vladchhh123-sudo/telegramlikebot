"""Менеджер Telethon-клиентов: МУЛЬТИ-АККАУНТ без лимитов.

На одного владельца панели (owner) — сколько угодно загруженных аккаунтов.
Вход: номер → код → облачный пароль (2FA). Сессии в SQLite, восстановление при старте.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.types.auth import (
    SentCodeTypeApp,
    SentCodeTypeCall,
    SentCodeTypeFlashCall,
    SentCodeTypeFragmentSms,
    SentCodeTypeMissedCall,
    SentCodeTypeSms,
)

from bot.db import Database

logger = logging.getLogger(__name__)


class LoginError(Exception):
    """Понятная пользователю ошибка входа (текст показывается в чате)."""


@dataclass
class _LoginFlow:
    client: TelegramClient
    phone: str
    phone_code_hash: Optional[str] = None


def code_type_hint(sent_code: Any) -> str:
    """Человекочитаемая подсказка, каким способом придёт код."""
    t = getattr(sent_code, "type", None)
    if isinstance(t, SentCodeTypeApp):
        return "Код придёт в приложение Telegram (сообщение от Telegram)."
    if isinstance(t, SentCodeTypeSms):
        return "Код придёт по SMS на указанный номер."
    if isinstance(t, SentCodeTypeCall):
        return "Вам позвонят и продиктуют код."
    if isinstance(t, SentCodeTypeFlashCall) or isinstance(t, SentCodeTypeMissedCall):
        return "Придёт пропущенный звонок: код — последние цифры номера звонившего."
    if isinstance(t, SentCodeTypeFragmentSms):
        return "Код придёт по SMS (анонимный номер Fragment)."
    return "Код придёт в приложение Telegram или по SMS."


class ClientManager:
    """owner_bot_user_id → {account_user_id → TelegramClient}."""

    def __init__(self, api_id: int, api_hash: str, db: Database) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._db = db
        self._clients: dict[int, dict[int, TelegramClient]] = {}
        self._pending: dict[int, _LoginFlow] = {}

    # ------------------------------------------------------------------ восстановление

    async def restore_all(self) -> int:
        """При старте восстанавливает все сохранённые сессии всех владельцев."""
        restored = 0
        for owner_id in {row["owner_bot_user_id"] for row in await self._db_all_rows()}:
            rows = await self._db.list_accounts(owner_id)
            for row in rows:
                try:
                    client = TelegramClient(StringSession(row["session"]), self._api_id, self._api_hash)
                    await client.connect()
                    if await client.is_user_authorized():
                        me = await client.get_me()
                        self._clients.setdefault(owner_id, {})[me.id] = client
                        restored += 1
                    else:
                        await client.disconnect()
                        logger.warning(
                            "Сессия аккаунта %s (owner=%s) недействительна — нужен повторный вход.",
                            row["account_user_id"], owner_id,
                        )
                except Exception:
                    logger.exception("Не удалось восстановить сессию owner=%s", owner_id)
        return restored

    async def _db_all_rows(self) -> list[dict[str, Any]]:
        async with self._db.conn.execute("SELECT owner_bot_user_id FROM accounts") as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------ статус

    def get(self, owner_id: int, account_user_id: int) -> Optional[TelegramClient]:
        return self._clients.get(owner_id, {}).get(account_user_id)

    def logged_ids(self, owner_id: int) -> set[int]:
        return set(self._clients.get(owner_id, {}).keys())

    def is_logged_in(self, owner_id: int) -> bool:
        """True, если загружен хотя бы один аккаунт."""
        return bool(self._clients.get(owner_id))

    def accounts_count(self, owner_id: int) -> int:
        return len(self._clients.get(owner_id, {}))

    async def list_account_infos(self, owner_id: int, logged_only: bool = False) -> list[dict[str, Any]]:
        """Список аккаунтов владельца: id, имя, телефон, username, флаг «в сети»."""
        live = self.logged_ids(owner_id)
        infos: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in await self._db.list_accounts(owner_id):
            acc_uid = row["account_user_id"]
            if acc_uid is None or acc_uid in seen:
                continue
            seen.add(acc_uid)
            if logged_only and acc_uid not in live:
                continue
            infos.append(
                {
                    "id": acc_uid,
                    "name": row["account_name"] or f"аккаунт {acc_uid}",
                    "phone": row["phone"],
                    "logged": acc_uid in live,
                }
            )
        # аккаунты в памяти, которых нет в БД (не должно случаться, но на всякий случай)
        for acc_uid in live - seen:
            info = await self.account_info(owner_id, acc_uid)
            if info:
                infos.append(
                    {"id": acc_uid, "name": info["name"], "phone": info["phone"], "logged": True}
                )
        return infos

    async def account_info(self, owner_id: int, account_user_id: int) -> Optional[dict[str, Any]]:
        client = self.get(owner_id, account_user_id)
        if client is None:
            return None
        me = await client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name])).strip()
        return {
            "id": me.id,
            "name": name or "—",
            "username": ("@" + me.username) if me.username else "—",
            "phone": me.phone or "—",
            "has_photo": me.photo is not None,
        }

    # ------------------------------------------------------------------ вход

    async def start_login(self, owner_id: int, phone: str) -> str:
        """Отправляет код на номер. Возвращает подсказку о способе доставки кода."""
        await self.cancel_login(owner_id)

        client = TelegramClient(
            StringSession(),
            self._api_id,
            self._api_hash,
            device_model="BoostUserbot",
            system_version="1.0.0",
            app_version="1.0.0",
            lang_code="ru",
            system_lang_code="ru",
        )
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except errors.PhoneNumberInvalidError as e:
            await client.disconnect()
            raise LoginError("Неверный формат номера. Пример: <code>+79001234567</code>") from e
        except errors.PhoneNumberBannedError as e:
            await client.disconnect()
            raise LoginError("Этот номер заблокирован в Telegram.") from e
        except errors.ApiIdInvalidError as e:
            await client.disconnect()
            raise LoginError("Неверные API_ID/API_HASH — проверьте .env.") from e
        except errors.FloodWaitError as e:
            await client.disconnect()
            raise LoginError(f"Слишком много попыток входа. Повторите через {e.seconds + 1} c.") from e
        except LoginError:
            await client.disconnect()
            raise
        except Exception as e:
            await client.disconnect()
            raise LoginError(f"Не удалось отправить код: {e}") from e

        self._pending[owner_id] = _LoginFlow(
            client=client, phone=phone, phone_code_hash=sent.phone_code_hash
        )
        return code_type_hint(sent)

    async def confirm_code(self, owner_id: int, code: str) -> str:
        """Проверяет код. Возвращает 'ok' или 'need_password' (2FA)."""
        flow = self._pending.get(owner_id)
        if flow is None:
            raise LoginError("Сессия входа не найдена. Начните заново: /start → «Загрузить аккаунт».")
        try:
            await flow.client.sign_in(phone=flow.phone, code=code, phone_code_hash=flow.phone_code_hash)
        except errors.SessionPasswordNeededError:
            return "need_password"
        except errors.PhoneCodeInvalidError as e:
            raise LoginError("Неверный код. Пришлите ещё раз (только цифры).") from e
        except errors.PhoneCodeExpiredError as e:
            await self.cancel_login(owner_id)
            raise LoginError("Код истёк. Начните вход заново (/cancel → «Загрузить аккаунт»).") from e
        except errors.PhoneNumberUnoccupiedError as e:
            await self.cancel_login(owner_id)
            raise LoginError("Этот номер не зарегистрирован в Telegram.") from e
        except errors.FloodWaitError as e:
            raise LoginError(f"Слишком много попыток. Повторите через {e.seconds + 1} c.") from e
        except LoginError:
            raise
        except Exception as e:
            raise LoginError(f"Ошибка входа: {e}") from e
        await self._finalize(owner_id)
        return "ok"

    async def confirm_password(self, owner_id: int, password: str) -> None:
        flow = self._pending.get(owner_id)
        if flow is None:
            raise LoginError("Сессия входа не найдена. Начните заново: /start → «Загрузить аккаунт».")
        try:
            await flow.client.sign_in(password=password)
        except errors.PasswordHashInvalidError as e:
            raise LoginError("Неверный облачный пароль. Попробуйте ещё раз или /cancel.") from e
        except errors.FloodWaitError as e:
            raise LoginError(f"Слишком много попыток. Повторите через {e.seconds + 1} c.") from e
        except LoginError:
            raise
        except Exception as e:
            raise LoginError(f"Ошибка входа: {e}") from e
        await self._finalize(owner_id)

    async def _finalize(self, owner_id: int) -> None:
        flow = self._pending.pop(owner_id)
        try:
            me = await flow.client.get_me()
            session_str = flow.client.session.save()
            name = " ".join(filter(None, [me.first_name, me.last_name])).strip()
            await self._db.save_account(
                owner_bot_user_id=owner_id,
                phone=flow.phone,
                session=session_str,
                account_user_id=me.id,
                account_name=name,
            )
            self._clients.setdefault(owner_id, {})[me.id] = flow.client
            logger.info("Аккаунт добавлен: owner=%s → tg_user=%s (%s)", owner_id, me.id, name)
        except Exception:
            await flow.client.disconnect()
            raise

    # ------------------------------------------------------------------ выход / отмена

    async def cancel_login(self, owner_id: int) -> None:
        flow = self._pending.pop(owner_id, None)
        if flow is not None:
            try:
                await flow.client.disconnect()
            except Exception:
                logger.debug("Не удалось отключить клиент при отмене входа", exc_info=True)

    async def logout_account(self, owner_id: int, account_user_id: int) -> bool:
        client = self._clients.get(owner_id, {}).pop(account_user_id, None)
        if client is not None:
            try:
                await client.log_out()
            except Exception:
                logger.exception("Ошибка log_out (owner=%s, acc=%s)", owner_id, account_user_id)
                try:
                    await client.disconnect()
                except Exception:
                    pass
        await self._db.delete_account(owner_id, account_user_id)
        return client is not None

    async def shutdown(self) -> None:
        for clients in list(self._clients.values()):
            for client in list(clients.values()):
                try:
                    await client.disconnect()
                except Exception:
                    pass
        for flow in list(self._pending.values()):
            try:
                await flow.client.disconnect()
            except Exception:
                pass
