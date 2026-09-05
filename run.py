"""Точка входа: сборка и запуск бота."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from bot.config import Config, load_config
from bot.db import Database
from bot.handlers import invite, login, messages, parse, profile, reactions, start, stories
from bot.middlewares import AccessMiddleware
from bot.registry import TaskRegistry
from bot.tg_client import ClientManager
from bot.texts import VERSION

logger = logging.getLogger("bot")

BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="tasks", description="Статус всех задач"),
    BotCommand(command="my", description="Информация о загруженном аккаунте"),
    BotCommand(command="stop", description="Остановить все задачи"),
    BotCommand(command="cancel", description="Отменить текущий шаг / вход"),
    BotCommand(command="logout", description="Выйти из загруженного аккаунта"),
    BotCommand(command="help", description="Справка"),
]


def build_dispatcher(cfg: Config, db: Database, manager: ClientManager, registry: TaskRegistry) -> Dispatcher:
    """Собирает Dispatcher со всеми роутерами и зависимостями."""
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(AccessMiddleware(cfg.admin_ids))

    # Зависимости, доступные обработчикам по имени параметра
    dp["cfg"] = cfg
    dp["db"] = db
    dp["manager"] = manager
    dp["registry"] = registry

    # Порядок важен: сначала глобальные команды и меню, потом FSM-сценарии
    dp.include_router(start.router)
    dp.include_router(login.router)
    dp.include_router(reactions.router)
    dp.include_router(stories.router)
    dp.include_router(messages.router)
    dp.include_router(parse.router)
    dp.include_router(invite.router)
    dp.include_router(profile.router)  # последним: здесь перехват устаревших кнопок
    return dp


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    cfg = load_config()
    logger.info("Telegram Boost Bot v%s", VERSION)

    db = Database(cfg.db_path)
    await db.connect()

    manager = ClientManager(cfg.api_id, cfg.api_hash, db)
    restored = await manager.restore_all()
    logger.info("Восстановлено сессий загруженных аккаунтов: %d", restored)

    registry = TaskRegistry(max_concurrent=cfg.max_concurrent_tasks)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(cfg, db, manager, registry)

    try:
        await bot.set_my_commands(BOT_COMMANDS)
        logger.info("Бот запущен. Для остановки нажмите Ctrl+C.")
        await dp.start_polling(bot)
    finally:
        await manager.shutdown()
        await db.close()


if __name__ == "__main__":
    import sys

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except RuntimeError as e:
        print(f"\n❌ Ошибка конфигурации: {e}")
        sys.exit(1)
