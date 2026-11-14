import asyncio
import threading
import time
from types import SimpleNamespace

from vkover import NewMessage, TokenExpired, VKError

from app.config import Chat
from app.main import run_service
from app.vk_source import VKSource, install_lock
from tests.conftest import CHANNEL, OWNER, PEER, PEER2, FakeBot, FakeVK, make_msg

CHATS = [Chat(PEER, "Чат 1"), Chat(PEER2, "Чат 2")]


class ScriptedVK(FakeVK):
    """history отдаёт сообщения из словаря peer -> список (новые первыми)."""

    def __init__(self, store=None):
        super().__init__()
        self.store = store or {}
        self.handlers = {}
        self.history_calls = []
        self.history_error = None
        self.run_error = None
        self._block = threading.Event()

    def on(self, cls):
        def deco(fn):
            self.handlers[cls] = fn
            return fn
        return deco

    def history(self, peer_id, count=50, offset=0, rev=False):
        self.history_calls.append((peer_id, count))
        if self.history_error:
            raise self.history_error
        return list(self.store.get(peer_id, []))[:count]

    def run(self):
        if self.run_error:
            raise self.run_error
        self._block.wait()


# 10. Первый запуск и перезапуск
async def test_first_run_sets_cursor_and_publishes_nothing_then_only_new(redis):
    from app.dedup import Dedup
    dedup = Dedup(redis)
    vk = ScriptedVK({PEER: [make_msg(5), make_msg(4)], PEER2: []})
    sent = []
    src = VKSource(vk, CHATS, sent.append, lambda: None)

    await src.init_cursors(dedup)
    assert await dedup.get_cursor(PEER) == 5 and await dedup.get_cursor(PEER2) == 0
    assert vk.history_calls == [(PEER, 1), (PEER2, 1)]
    src.fetch_all()
    assert sent == []  # ничего не публикуется

    # перезапуск: ключ уже есть, history(count=1) не вызывается, забираются только новые
    vk2 = ScriptedVK({PEER: [make_msg(7), make_msg(6), make_msg(5), make_msg(4)], PEER2: []})
    sent2 = []
    src2 = VKSource(vk2, CHATS, sent2.append, lambda: None)
    await src2.init_cursors(dedup)
    assert all(c[1] != 1 for c in vk2.history_calls)
    src2.fetch_all()
    assert [m.conversation_message_id for m in sent2] == [6, 7]  # по возрастанию
    assert all(m.peer_id == PEER for m in sent2)
    sent2.clear()
    src2.fetch_all()
    assert sent2 == []  # повторный опрос не дублирует


def test_fetch_more_than_history_count_takes_latest_only():
    msgs = [make_msg(i) for i in range(300, 250, -1)]  # 50 новейших: 300..251
    vk = ScriptedVK({PEER: msgs})
    sent = []
    src = VKSource(vk, [Chat(PEER, "x")], sent.append, lambda: None)
    src.enq_cursor[PEER] = 100  # пропущено > 50
    src.fetch_peer(PEER)
    assert len(sent) == 50 and sent[0].conversation_message_id == 251
    assert src.enq_cursor[PEER] == 300


def test_event_handler_filters_by_config():
    vk = ScriptedVK()
    src = VKSource(vk, CHATS, lambda m: None, lambda: None)
    handler = vk.handlers[NewMessage]
    ev = lambda peer: SimpleNamespace(peer_id=peer)
    handler(ev(PEER))
    handler(ev(2000000099))  # не из конфига
    handler(SimpleNamespace())  # исключение внутри не должно вылетать
    assert src.signals.qsize() == 1 and src.signals.get() == PEER


def test_other_vk_error_does_not_stop_fetch_all():
    class V(ScriptedVK):
        def history(self, peer_id, count=50, offset=0, rev=False):
            if peer_id == PEER:
                raise VKError(9, "flood")
            return [make_msg(3, peer=peer_id)]

    sent = []
    src = VKSource(V(), CHATS, sent.append, lambda: None)
    src.fetch_all()
    assert [m.peer_id for m in sent] == [PEER2]


def test_lock_serializes_calls_and_refresh():
    active, overlaps = 0, 0
    guard = threading.Lock()

    def slow(method, **p):
        nonlocal active, overlaps
        with guard:
            active += 1
            overlaps += active > 1
        time.sleep(0.02)
        with guard:
            active -= 1
        return method

    refreshed = []
    vk = SimpleNamespace(client=SimpleNamespace(call=slow),
                         refresher=SimpleNamespace(refresh=lambda: refreshed.append(1) or "tok"))
    lock = install_lock(vk)
    threads = [threading.Thread(target=vk.client.call, args=("m",)) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert overlaps == 0
    # refresh берёт тот же замок
    with lock:
        t = threading.Thread(target=vk.refresher.refresh)
        t.start(); t.join(0.05)
        assert t.is_alive() and not refreshed
    t.join(1)
    assert refreshed == [1]


# 12. TokenExpired
async def test_token_expired_in_fetcher_notifies_once_and_exits_0(redis):
    vk = ScriptedVK({PEER: [make_msg(1)], PEER2: []})
    bot = FakeBot()
    task = asyncio.create_task(run_service(
        chats=CHATS, vk=vk, redis=redis, bot=bot, owner_id=OWNER, channel_id=CHANNEL,
        http_get=None, poll_interval=0.05))
    await asyncio.sleep(0.2)  # старт прошёл
    vk.history_error = TokenExpired(5, "access_token has expired")
    vk.run_error = TokenExpired(5, "access_token has expired")
    vk._block.set()  # vk.run() в потоке VK тоже падает с TokenExpired
    code = await asyncio.wait_for(task, 5)
    assert code == 0
    owner = bot.owner_calls()
    assert len(owner) == 1 and "Сессия ВК недействительна" in owner[0][1]["text"]
    assert bot.channel_calls() == []


async def test_token_expired_in_run_notifies_once_and_exits_0(redis):
    vk = ScriptedVK({PEER: [], PEER2: []})
    vk.run_error = TokenExpired(5, "x")
    bot = FakeBot()
    code = await asyncio.wait_for(run_service(
        chats=CHATS, vk=vk, redis=redis, bot=bot, owner_id=OWNER, channel_id=CHANNEL,
        http_get=None, poll_interval=0.05), 5)
    assert code == 0 and len(bot.owner_calls()) == 1


async def test_token_expired_at_startup(redis):
    vk = ScriptedVK()
    vk.history_error = TokenExpired(5, "x")
    bot = FakeBot()
    code = await asyncio.wait_for(run_service(
        chats=CHATS, vk=vk, redis=redis, bot=bot, owner_id=OWNER, channel_id=CHANNEL,
        http_get=None, poll_interval=0.05), 5)
    assert code == 0 and len(bot.owner_calls()) == 1


async def test_end_to_end_message_is_published(redis):
    vk = ScriptedVK({PEER: [make_msg(1, text="старое")], PEER2: []})
    bot = FakeBot()
    stop = asyncio.Event()
    task = asyncio.create_task(run_service(
        chats=CHATS, vk=vk, redis=redis, bot=bot, owner_id=OWNER, channel_id=CHANNEL,
        http_get=None, poll_interval=0.05, stop_event=stop))
    await asyncio.sleep(0.2)
    assert bot.calls == []  # первый запуск: старое не пересылается
    vk.store[PEER] = [make_msg(2, text="новое"), make_msg(1, text="старое")]
    vk.handlers[NewMessage](SimpleNamespace(peer_id=PEER))
    for _ in range(50):
        if bot.channel_calls():
            break
        await asyncio.sleep(0.05)
    stop.set()
    assert await asyncio.wait_for(task, 5) == 0
    (call,) = bot.channel_calls()
    assert "новое" in call[1]["text"] and "Чат 1" in call[1]["text"]
