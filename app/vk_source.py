import asyncio
import logging
import queue
import threading
import time
from typing import Callable

from vkover import NewMessage, TokenExpired, VKError

from .config import Chat
from .constants import HISTORY_COUNT, POLL_INTERVAL_SEC

logger = logging.getLogger(__name__)


def install_lock(vk, lock=None):
    """FR-2: все вызовы VK API из любых потоков сериализуются одним RLock.
    Оборачиваются vk.client.call и vk.refresher.refresh (обновление токена
    запускает Chromium напрямую, мимо client.call)."""
    lock = lock or threading.RLock()

    original_call = vk.client.call

    def locked_call(method, **params):
        with lock:
            return original_call(method, **params)

    vk.client.call = locked_call

    refresher = getattr(vk, "refresher", None)
    if refresher is not None:
        original_refresh = refresher.refresh

        def locked_refresh(*args, **kwargs):
            with lock:
                return original_refresh(*args, **kwargs)

        refresher.refresh = locked_refresh
    return lock


class VKSource:
    def __init__(
        self,
        vk,
        chats: list[Chat],
        enqueue: Callable,
        on_session_expired: Callable[[], None],
        *,
        poll_interval: float = POLL_INTERVAL_SEC,
    ):
        self.vk = vk
        self.peers = [c.peer_id for c in chats]
        self._peer_set = set(self.peers)
        self.enqueue = enqueue
        self.on_session_expired = on_session_expired
        self.poll_interval = poll_interval
        self.signals: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.enq_cursor: dict[int, int] = {}
        self._expired_lock = threading.Lock()
        self._expired_reported = False
        self._register_handlers()

    # --- FR-1 ---
    def _register_handlers(self) -> None:
        @self.vk.on(NewMessage)
        def _on_new_message(event):  # синхронный; исключение убило бы vk.run()
            try:
                peer_id = event.peer_id
                if peer_id in self._peer_set:
                    self.signals.put(peer_id)
            except Exception:
                logger.error("ошибка в обработчике NewMessage", exc_info=True)

    def _report_expired(self) -> None:
        with self._expired_lock:
            if self._expired_reported:
                return
            self._expired_reported = True
        logger.error("сессия ВК недействительна (TokenExpired)")
        self.on_session_expired()

    # --- FR-5 ---
    async def init_cursors(self, dedup) -> None:
        for peer_id in self.peers:
            stored = await dedup.get_cursor(peer_id)
            if stored is None:
                msgs = await asyncio.to_thread(self.vk.history, peer_id, count=1)
                stored = msgs[0].conversation_message_id if msgs else 0
                await dedup.set_cursor(peer_id, stored)
                logger.info("первый запуск, чат %s: курсор=%s", peer_id, stored)
            self.enq_cursor[peer_id] = stored

    # --- FR-4 ---
    def fetch_peer(self, peer_id: int) -> int:
        msgs = self.vk.history(peer_id, count=HISTORY_COUNT)
        cursor = self.enq_cursor.get(peer_id, 0)
        new = sorted(
            (m for m in msgs if m.conversation_message_id > cursor),
            key=lambda m: m.conversation_message_id,
        )
        for m in new:
            m.peer_id = peer_id
            self.enqueue(m)
            self.enq_cursor[peer_id] = m.conversation_message_id
        return len(new)

    def fetch_all(self) -> None:
        for peer_id in self.peers:
            try:
                self.fetch_peer(peer_id)
            except TokenExpired:
                raise
            except VKError:
                logger.error("ошибка VK при чтении чата %s", peer_id, exc_info=True)
            except Exception:
                logger.error("сбой при чтении чата %s", peer_id, exc_info=True)

    # --- FR-3 ---
    def fetcher_loop(self) -> None:
        first = True
        last_full = 0.0
        while not self.stop.is_set():
            signaled = False
            if not first:
                try:
                    self.signals.get(timeout=self.poll_interval)
                    signaled = True
                except queue.Empty:
                    pass
                while True:  # сводим всплеск сигналов к одному опросу
                    try:
                        self.signals.get_nowait()
                        signaled = True
                    except queue.Empty:
                        break
            if first or signaled or time.monotonic() - last_full >= self.poll_interval:
                first = False
                try:
                    self.fetch_all()
                except TokenExpired:
                    self._report_expired()
                    return
                last_full = time.monotonic()

    # --- FR-1/FR-6: поток VK ---
    def vk_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.vk.run()
                return
            except TokenExpired:
                self._report_expired()
                return
            except Exception:
                logger.error("сбой vk.run(), повтор через 5 с", exc_info=True)
                self.stop.wait(5)

    def start(self) -> list[threading.Thread]:
        threads = [
            threading.Thread(target=self.vk_loop, name="vk-run", daemon=True),
            threading.Thread(target=self.fetcher_loop, name="vk-fetcher", daemon=True),
        ]
        for t in threads:
            t.start()
        return threads
