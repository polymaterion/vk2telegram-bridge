import asyncio
import logging
import signal
import sys

import httpx
from aiogram import Bot
from redis.asyncio import Redis
from vkover import TokenExpired, VKover

from .config import ConfigError, load_chats, load_settings
from .constants import POLL_INTERVAL_SEC, SESSION_EXPIRED_TEXT
from .dedup import Dedup
from .telegram_sink import TelegramSink
from .vk_source import VKSource, install_lock
from .worker import Worker, consume

logger = logging.getLogger("app")


async def run_service(
    *,
    chats,
    vk,
    redis,
    bot,
    owner_id: int,
    channel_id: int,
    http_get,
    sleep=asyncio.sleep,
    poll_interval: float = POLL_INTERVAL_SEC,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Запуск потоков, очередей и worker. Возвращает код завершения процесса."""
    loop = asyncio.get_running_loop()
    stop_event = stop_event or asyncio.Event()
    dedup = Dedup(redis)
    sink = TelegramSink(bot, channel_id, owner_id, sleep=sleep)
    out_queue: asyncio.Queue = asyncio.Queue()
    expired = asyncio.Event()

    install_lock(vk)
    source = VKSource(
        vk,
        chats,
        enqueue=lambda m: loop.call_soon_threadsafe(out_queue.put_nowait, m),
        on_session_expired=lambda: loop.call_soon_threadsafe(expired.set),
        poll_interval=poll_interval,
    )

    try:
        me_id = await asyncio.to_thread(lambda: vk.me["id"])
        await source.init_cursors(dedup)
    except TokenExpired:
        logger.error("сессия ВК недействительна при старте")
        await sink.notify_owner(SESSION_EXPIRED_TEXT)
        return 0

    worker = Worker(
        vk=vk,
        me_id=me_id,
        dedup=dedup,
        sink=sink,
        titles={c.peer_id: c.title for c in chats},
        http_get=http_get,
        sleep=sleep,
    )
    worker_task = asyncio.create_task(consume(out_queue, worker))
    source.start()
    logger.info("сервис запущен, чатов: %d", len(chats))

    waiters = [
        asyncio.create_task(expired.wait()),
        asyncio.create_task(stop_event.wait()),
    ]
    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    source.stop.set()
    worker_task.cancel()
    for t in waiters:
        t.cancel()
    if expired.is_set():
        await sink.notify_owner(SESSION_EXPIRED_TEXT)
    return 0


async def amain() -> int:
    try:
        settings = load_settings()
        chats = load_chats(settings.config_path)
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    bot = Bot(token=settings.tg_bot_token)
    http = httpx.AsyncClient(timeout=30, follow_redirects=True)

    async def http_get(url: str) -> bytes:
        r = await http.get(url)
        r.raise_for_status()
        return r.content

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        try:
            vk = VKover.from_files(settings.vk_token_file, profile_dir=settings.vk_profile_dir)
        except (OSError, KeyError, ValueError):
            # нет/повреждён файл токена — та же ситуация, что и недействительная сессия
            logger.error("не удалось прочитать токен ВК (%s)", settings.vk_token_file, exc_info=True)
            await TelegramSink(bot, settings.tg_channel_id, settings.tg_owner_id).notify_owner(
                SESSION_EXPIRED_TEXT
            )
            return 0

        return await run_service(
            chats=chats,
            vk=vk,
            redis=redis,
            bot=bot,
            owner_id=settings.tg_owner_id,
            channel_id=settings.tg_channel_id,
            http_get=http_get,
            stop_event=stop_event,
        )
    finally:
        await http.aclose()
        await redis.aclose()
        await bot.session.close()


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
