import asyncio
import logging
from dataclasses import dataclass

from .constants import PHOTO_MAX_BYTES
from .dedup import Dedup, compute_content_hash
from .formatting import build_message_url, build_publication
from .normalize import parse_message
from .telegram_sink import TelegramSink, with_retries

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Author:
    name: str
    url: str


class Worker:
    """Обработка одного сообщения от начала до конца (FR-7 … FR-17)."""

    def __init__(self, *, vk, me_id: int, dedup: Dedup, sink: TelegramSink,
                 titles: dict[int, str], http_get, sleep=asyncio.sleep):
        self.vk = vk
        self.me_id = me_id
        self.dedup = dedup
        self.sink = sink
        self.titles = titles
        self._http_get = http_get  # async (url) -> bytes
        self._sleep = sleep
        self._authors: dict[int, Author] = {}

    # --- автор (FR-13) ---
    async def _author(self, from_id: int) -> Author:
        cached = self._authors.get(from_id)
        if cached:
            return cached
        if from_id > 0:
            url = f"https://vk.ru/id{from_id}"
            fallback = f"id{from_id}"
        else:
            url = f"https://vk.ru/club{abs(from_id)}"
            fallback = f"id{from_id}"
        try:
            if from_id > 0:
                users = await asyncio.to_thread(self.vk.users, [from_id])
                u = users[0]
                name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
            else:
                resp = await asyncio.to_thread(
                    self.vk.request, "groups.getById", group_id=abs(from_id)
                )
                if isinstance(resp, dict):
                    resp = resp.get("groups") or []
                name = (resp[0].get("name") or "").strip()
            if not name:
                raise ValueError("пустое имя автора")
        except Exception:
            logger.warning("не удалось получить автора %s", from_id, exc_info=True)
            return Author(fallback, url)
        author = Author(name, url)
        self._authors[from_id] = author
        return author

    # --- фото (FR-11) ---
    async def _fetch_photo(self, photo: dict) -> bytes:
        sizes = [s for s in (photo.get("sizes") or []) if isinstance(s, dict) and s.get("url")]
        if not sizes:
            raise ValueError("у фото нет вариантов sizes")
        sizes.sort(key=lambda s: (s.get("width") or 0) * (s.get("height") or 0), reverse=True)
        for s in sizes:
            data = await with_retries(
                lambda url=s["url"]: self._http_get(url), sleep=self._sleep
            )
            if len(data) <= PHOTO_MAX_BYTES:
                return data
        raise ValueError("все варианты фото больше лимита")

    # --- основной сценарий ---
    def _skip_reason(self, m) -> str | None:
        if m.out:
            return "исходящее"
        if m.from_id == self.me_id:
            return "от собственного аккаунта"
        if "action" in m.raw:
            return "служебное событие беседы"
        return None

    async def _fail(self, m, title: str, exc: Exception) -> None:
        peer, cmid = m.peer_id, m.conversation_message_id
        logger.error("[%s] cmid=%s ошибка обработки", title, cmid, exc_info=exc)
        err = f"{type(exc).__name__}: {exc}"[:200]
        await self.sink.notify_owner(
            f"Не удалось переслать сообщение\n"
            f"Чат: {title}\n"
            f"{build_message_url(peer, m)}\n"
            f"Ошибка: {err}"
        )
        await self.dedup.advance_cursor(peer, cmid)

    async def process(self, m) -> None:
        peer, cmid = m.peer_id, m.conversation_message_id
        title = self.titles.get(peer, str(peer))

        def log(msg: str) -> None:
            logger.info("[%s] cmid=%s %s", title, cmid, msg)

        reason = self._skip_reason(m)
        if reason:
            await self.dedup.advance_cursor(peer, cmid)
            log(f"пропущено: {reason}")
            return

        if await self.dedup.seen_msg(peer, cmid):
            await self.dedup.advance_cursor(peer, cmid)
            log("дубль (а)")
            return

        parsed = parse_message(m)
        text = parsed.combined_text
        if not text and not parsed.photos:
            await self.dedup.mark_msg(peer, cmid)
            await self.dedup.advance_cursor(peer, cmid)
            log("пропущено: нет текста и фото")
            return

        try:
            photo_bytes = [await self._fetch_photo(p) for p in parsed.photos]
        except Exception as e:
            await self._fail(m, title, e)
            return

        content_hash = compute_content_hash(text, photo_bytes)
        owner = await self.dedup.content_owner(content_hash)
        if owner:
            await self.dedup.mark_msg(peer, cmid)
            await self.dedup.advance_cursor(peer, cmid)
            log(f"дубль (б) of {owner}")
            return

        try:
            author = await self._author(m.from_id)
            pub = build_publication(
                title=title,
                author_name=author.name,
                author_url=author.url,
                text=text,
                other_types=parsed.other_types,
                message_url=build_message_url(peer, m),
                has_photos=bool(photo_bytes),
            )
            await self.sink.send(photo_bytes, pub)
        except Exception as e:
            await self._fail(m, title, e)
            return

        await self.dedup.mark_sent(peer, cmid, content_hash)
        await self.dedup.advance_cursor(peer, cmid)
        log("отправлено")


async def consume(queue: asyncio.Queue, worker: Worker) -> None:
    """Берёт сообщения из out_queue строго по одному в порядке получения."""
    while True:
        m = await queue.get()
        try:
            await worker.process(m)
        except Exception:
            logger.error(
                "необработанная ошибка, cmid=%s", getattr(m, "conversation_message_id", "?"),
                exc_info=True,
            )
        finally:
            queue.task_done()
