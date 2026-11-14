import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaPhoto, LinkPreviewOptions

from .constants import RETRY_ATTEMPTS, RETRY_BACKOFF_SEC
from .formatting import Publication

logger = logging.getLogger(__name__)
T = TypeVar("T")


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = RETRY_ATTEMPTS,
    backoff=RETRY_BACKOFF_SEC,
    sleep=asyncio.sleep,
) -> T:
    """До `attempts` попыток с паузами `backoff`. TelegramRetryAfter ждёт
    retry_after секунд и входит в число попыток."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except TelegramRetryAfter as e:
            last = e
            if i < attempts - 1:
                await sleep(e.retry_after)
        except Exception as e:
            last = e
            if i < attempts - 1:
                await sleep(backoff[min(i, len(backoff) - 1)])
    assert last is not None
    raise last


class TelegramSink:
    def __init__(self, bot, channel_id: int, owner_id: int, sleep=asyncio.sleep):
        self.bot = bot
        self.channel_id = channel_id
        self.owner_id = owner_id
        self._sleep = sleep

    async def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        return await with_retries(fn, sleep=self._sleep)

    async def _send_photos(self, photos: list[bytes], caption: str | None) -> None:
        files = [BufferedInputFile(data, filename=f"photo{i + 1}.jpg") for i, data in enumerate(photos)]
        if len(files) == 1:
            await self._call(
                lambda: self.bot.send_photo(
                    chat_id=self.channel_id,
                    photo=files[0],
                    caption=caption,
                    parse_mode=ParseMode.HTML if caption else None,
                )
            )
            return
        media = [
            InputMediaPhoto(
                media=f,
                caption=caption if i == 0 else None,
                parse_mode=ParseMode.HTML if (i == 0 and caption) else None,
            )
            for i, f in enumerate(files)
        ]
        await self._call(lambda: self.bot.send_media_group(chat_id=self.channel_id, media=media))

    async def send(self, photos: list[bytes], pub: Publication) -> None:
        if photos:
            await self._send_photos(photos, pub.caption)
        for text in pub.texts:
            await self._call(
                lambda text=text: self.bot.send_message(
                    chat_id=self.channel_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )

    async def notify_owner(self, text: str) -> None:
        """Личное сообщение владельцу простым текстом; ошибки только логируются."""
        try:
            await self._call(
                lambda: self.bot.send_message(chat_id=self.owner_id, text=text, parse_mode=None)
            )
        except Exception:
            logger.error("не удалось уведомить владельца", exc_info=True)
