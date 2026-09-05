"""Конфигурация: чтение и валидация переменных окружения (.env)."""
from __future__ import annotations

import os
import random
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError as e:
        raise RuntimeError(f"Переменная {name} должна быть числом (получено: {raw!r}).") from e


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"Переменная {name} должна быть целым числом (получено: {raw!r}).") from e


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    admin_ids: tuple[int, ...]
    db_path: str

    reaction_delay: tuple[float, float]
    story_delay: tuple[float, float]
    peer_delay: tuple[float, float]
    send_delay: tuple[float, float]

    # Значения по умолчанию для лимитов (конкретные лимиты пользователь выбирает в боте)
    default_messages_limit: int
    default_peers_limit: int

    flood_wait_cap: int
    progress_interval: float
    max_concurrent_tasks: int


def rand_in_range(rng: tuple[float, float]) -> float:
    """Случайное значение из диапазона (с защитой от перепутанных границ)."""
    a, b = rng
    return random.uniform(min(a, b), max(a, b))


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    api_id_raw = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()

    missing = [
        name
        for name, value in (("BOT_TOKEN", bot_token), ("API_ID", api_id_raw), ("API_HASH", api_hash))
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Не заданы обязательные переменные окружения: "
            + ", ".join(missing)
            + ".\nСкопируйте .env.example в .env, заполните и перезапустите (см. README)."
        )

    try:
        api_id = int(api_id_raw)
    except ValueError as e:
        raise RuntimeError("API_ID должен быть целым числом (см. https://my.telegram.org).") from e

    admin_raw = os.getenv("ADMIN_IDS", "").replace(" ", "").strip()
    try:
        admin_ids = tuple(int(part) for part in admin_raw.split(",") if part)
    except ValueError as e:
        raise RuntimeError("ADMIN_IDS должен содержать числовые Telegram ID через запятую.") from e

    reaction_delay = (_env_float("REACTION_DELAY_MIN", 0.8), _env_float("REACTION_DELAY_MAX", 1.6))
    story_delay = (_env_float("STORY_REACTION_DELAY_MIN", 0.6), _env_float("STORY_REACTION_DELAY_MAX", 1.2))
    peer_delay = (_env_float("PEER_DELAY_MIN", 0.3), _env_float("PEER_DELAY_MAX", 0.8))
    send_delay = (_env_float("SEND_DELAY_MIN", 3.0), _env_float("SEND_DELAY_MAX", 7.0))

    return Config(
        bot_token=bot_token,
        api_id=api_id,
        api_hash=api_hash,
        admin_ids=admin_ids,
        db_path=os.getenv("DB_PATH", "data/bot.db").strip() or "data/bot.db",
        reaction_delay=reaction_delay,
        story_delay=story_delay,
        peer_delay=peer_delay,
        send_delay=send_delay,
        default_messages_limit=max(1, _env_int("DEFAULT_MESSAGES_LIMIT", 1000)),
        default_peers_limit=max(1, _env_int("DEFAULT_PEERS_LIMIT", 300)),
        flood_wait_cap=max(30, _env_int("FLOOD_WAIT_CAP", 300)),
        progress_interval=max(3.0, _env_float("PROGRESS_INTERVAL", 8.0)),
        max_concurrent_tasks=max(1, _env_int("MAX_CONCURRENT_TASKS", 5)),
    )
