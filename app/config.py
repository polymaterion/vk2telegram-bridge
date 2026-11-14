import os
from dataclasses import dataclass

import yaml


class ConfigError(Exception):
    """Ошибка конфигурации: сервис завершается с кодом 1."""


@dataclass(frozen=True)
class Chat:
    peer_id: int
    title: str


@dataclass(frozen=True)
class Settings:
    tg_bot_token: str
    tg_channel_id: int
    tg_owner_id: int
    redis_url: str
    vk_token_file: str
    vk_profile_dir: str
    config_path: str
    log_level: str


def load_chats(path: str) -> list[Chat]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"не удалось прочитать {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"некорректный YAML в {path}: {e}") from e
    return parse_chats(data)


def parse_chats(data) -> list[Chat]:
    if not isinstance(data, dict):
        raise ConfigError("корень конфигурации должен быть словарём с ключом chats")
    items = data.get("chats")
    if not isinstance(items, list) or not items:
        raise ConfigError("chats: нужен непустой список")
    chats: list[Chat] = []
    seen: set[int] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"chats[{i}]: ожидается словарь")
        peer_id = item.get("peer_id")
        if isinstance(peer_id, bool) or not isinstance(peer_id, int) or peer_id <= 2000000000:
            raise ConfigError(f"chats[{i}].peer_id: целое число > 2000000000")
        if peer_id in seen:
            raise ConfigError(f"chats[{i}].peer_id={peer_id}: повтор")
        seen.add(peer_id)
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ConfigError(f"chats[{i}].title: непустая строка")
        chats.append(Chat(peer_id=peer_id, title=title.strip()))
    return chats


def _required(env: dict, name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"переменная окружения {name} не задана")
    return value


def _required_int(env: dict, name: str) -> int:
    raw = _required(env, name)
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name}: ожидается целое число, получено {raw!r}") from e


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        tg_bot_token=_required(env, "TG_BOT_TOKEN"),
        tg_channel_id=_required_int(env, "TG_CHANNEL_ID"),
        tg_owner_id=_required_int(env, "TG_OWNER_ID"),
        redis_url=env.get("REDIS_URL") or "redis://redis:6379/0",
        vk_token_file=env.get("VK_TOKEN_FILE") or "/data/web_token.json",
        vk_profile_dir=env.get("VK_PROFILE_DIR") or "/data/vk_profile",
        config_path=env.get("CONFIG_PATH") or "/app/config.yaml",
        log_level=(env.get("LOG_LEVEL") or "INFO").upper(),
    )
