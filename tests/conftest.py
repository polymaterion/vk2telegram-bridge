import asyncio
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from vkover import Message

from app.dedup import Dedup
from app.telegram_sink import TelegramSink
from app.worker import Worker

PEER = 2000000001
PEER2 = 2000000002
CHANNEL = -1001
OWNER = 777
ME = 1


def make_msg(cmid, *, peer=PEER, text="", from_id=100, out=False,
             attachments=None, fwd=None, **extra) -> Message:
    d = {
        "id": cmid + 5000,
        "peer_id": peer,
        "from_id": from_id,
        "date": 0,
        "text": text,
        "out": 1 if out else 0,
        "conversation_message_id": cmid,
        "attachments": attachments or [],
        "fwd_messages": fwd or [],
    }
    d.update(extra)
    return Message.from_api(d)


def photo_att(*sizes):
    """sizes: (url, width, height)"""
    return {
        "type": "photo",
        "photo": {"sizes": [{"url": u, "width": w, "height": h} for u, w, h in sizes]},
    }


def video_att():
    return {"type": "video", "video": {"id": 1}}


class FakeBot:
    def __init__(self, fail_channel_times=0):
        self.calls = []  # (method, kwargs)
        self.fail_channel_times = fail_channel_times
        self._failed = 0

    def _maybe_fail(self, chat_id):
        if chat_id == CHANNEL and self._failed < self.fail_channel_times:
            self._failed += 1
            raise RuntimeError("boom")

    async def send_message(self, chat_id, text, parse_mode=None, link_preview_options=None):
        self.calls.append(("send_message", dict(chat_id=chat_id, text=text, parse_mode=parse_mode,
                                                link_preview_options=link_preview_options)))
        self._maybe_fail(chat_id)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None):
        self.calls.append(("send_photo", dict(chat_id=chat_id, photo=photo, caption=caption,
                                              parse_mode=parse_mode)))
        self._maybe_fail(chat_id)

    async def send_media_group(self, chat_id, media):
        self.calls.append(("send_media_group", dict(chat_id=chat_id, media=media)))
        self._maybe_fail(chat_id)

    def channel_calls(self):
        return [c for c in self.calls if c[1]["chat_id"] == CHANNEL]

    def owner_calls(self):
        return [c for c in self.calls if c[1]["chat_id"] == OWNER]


class FakeVK:
    def __init__(self):
        self.me = {"id": ME}
        self.users_calls = 0
        self.client = SimpleNamespace(call=lambda method, **p: None)
        self.refresher = None

    def users(self, ids):
        self.users_calls += 1
        return [{"id": ids[0], "first_name": "Иван", "last_name": "Петров"}]

    def request(self, method, **params):
        assert method == "groups.getById"
        return {"groups": [{"id": params["group_id"], "name": "Сообщество"}]}


async def _no_sleep(_):
    return None


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class Env:
    def __init__(self, redis, bot, http_files):
        self.redis = redis
        self.bot = bot
        self.dedup = Dedup(redis)
        self.vk = FakeVK()
        self.http_files = http_files
        self.http_calls = []
        self.sink = TelegramSink(bot, CHANNEL, OWNER, sleep=_no_sleep)
        self.worker = Worker(
            vk=self.vk, me_id=ME, dedup=self.dedup, sink=self.sink,
            titles={PEER: "Чат 1", PEER2: "Чат 2"},
            http_get=self._http_get, sleep=_no_sleep,
        )

    async def _http_get(self, url):
        self.http_calls.append(url)
        return self.http_files[url]

    async def cursor(self, peer=PEER):
        return await self.dedup.get_cursor(peer)


@pytest.fixture
def make_env(redis):
    def _make(bot=None, files=None):
        return Env(redis, bot or FakeBot(), files or {})
    return _make
