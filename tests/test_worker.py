import asyncio
import html
from html.parser import HTMLParser

import pytest

from app.constants import CAPTION_LIMIT, PHOTO_MAX_BYTES, TEXT_LIMIT
from app.worker import consume
from tests.conftest import (CHANNEL, OWNER, PEER, PEER2, FakeBot, make_msg,
                            photo_att, video_att)


def texts_sent(bot):
    return [c[1]["text"] for c in bot.channel_calls() if c[0] == "send_message"]


# 1. Дубль «а»
async def test_dup_a(make_env):
    env = make_env()
    m = make_msg(10, text="привет")
    await env.worker.process(m)
    await env.worker.process(make_msg(10, text="привет"))
    assert len(env.bot.channel_calls()) == 1


# 2. Дубль «б»
async def test_dup_b_same_text_and_photo_across_chats(make_env):
    env = make_env(files={"u1": b"IMG", "u2": b"IMG"})
    await env.worker.process(make_msg(1, peer=PEER, text="Текст", attachments=[photo_att(("u1", 10, 10))]))
    await env.worker.process(make_msg(1, peer=PEER2, text="  текст ", attachments=[photo_att(("u2", 10, 10))]))
    assert len(env.bot.channel_calls()) == 1
    # дубль (б) ставит seen:msg и сдвигает курсор второго чата
    assert await env.dedup.seen_msg(PEER2, 1)
    assert await env.cursor(PEER2) == 1


async def test_different_photo_bytes_are_different(make_env):
    env = make_env(files={"u1": b"IMG1", "u2": b"IMG2"})
    await env.worker.process(make_msg(1, peer=PEER, text="Т", attachments=[photo_att(("u1", 10, 10))]))
    await env.worker.process(make_msg(1, peer=PEER2, text="Т", attachments=[photo_att(("u2", 10, 10))]))
    assert len(env.bot.channel_calls()) == 2


# 3. Вложенные пересылки, 3 уровня
async def test_nested_forwards(make_env):
    env = make_env(files={"a": b"A", "b": b"B", "c": b"C"})

    def build():
        lvl3 = {"text": "три", "attachments": [photo_att(("c", 5, 5))], "fwd_messages": []}
        lvl2 = {"text": "два", "attachments": [photo_att(("b", 5, 5))], "fwd_messages": [lvl3]}
        lvl1 = {"text": "один", "attachments": [], "fwd_messages": [lvl2]}
        return make_msg(7, text="верх", attachments=[photo_att(("a", 5, 5))], fwd=[lvl1])

    await env.worker.process(build())
    calls = env.bot.channel_calls()
    assert len(calls) == 1 and calls[0][0] == "send_media_group"
    media = calls[0][1]["media"]
    assert [m.media.data for m in media] == [b"A", b"B", b"C"]
    caption = media[0].caption
    assert caption.index("верх") < caption.index("один") < caption.index("два") < caption.index("три")
    # как одно сообщение проходит дедупликацию (б)
    other = build()
    other.peer_id, other.conversation_message_id = PEER2, 99
    await env.worker.process(other)
    assert len(env.bot.channel_calls()) == 1


# 4. Фильтр
@pytest.mark.parametrize("kwargs", [
    dict(out=True),
    dict(from_id=1),  # собственный аккаунт (ME)
    dict(action={"type": "chat_invite_user"}),
])
async def test_filter(make_env, kwargs):
    env = make_env()
    await env.worker.process(make_msg(5, text="текст", **kwargs))
    assert env.bot.calls == []
    assert await env.cursor() == 5


# 5. Пустое сообщение (только видео)
async def test_empty_message(make_env):
    env = make_env()
    await env.worker.process(make_msg(8, attachments=[video_att()]))
    assert env.bot.calls == []
    assert await env.cursor() == 8
    assert await env.dedup.seen_msg(PEER, 8)


# 6. Текст + видео; фото без текста
async def test_text_and_video(make_env):
    env = make_env()
    await env.worker.process(make_msg(1, text="смотри", attachments=[video_att()]))
    (text,) = texts_sent(env.bot)
    assert "[вложения: видео]" in text
    lines = text.split("\n")
    assert lines[0] == "Чат 1" and lines[1].startswith('<a href="https://vk.ru/id100">Иван Петров</a>')
    assert lines[2] == "смотри"
    assert lines[-1] == '<a href="https://vk.ru/im?sel=c1&amp;msgid=1">Ссылка на сообщение</a>'


async def test_photo_without_text_has_no_attachments_line(make_env):
    env = make_env(files={"u": b"X"})
    await env.worker.process(make_msg(1, attachments=[photo_att(("u", 5, 5)), video_att()]))
    (call,) = env.bot.channel_calls()
    assert call[0] == "send_photo"
    assert "[вложения" not in call[1]["caption"]


async def test_group_author(make_env):
    env = make_env()
    await env.worker.process(make_msg(1, text="x", from_id=-55))
    (text,) = texts_sent(env.bot)
    assert '<a href="https://vk.ru/club55">Сообщество</a>' in text


async def test_author_cached(make_env):
    env = make_env()
    await env.worker.process(make_msg(1, text="a"))
    await env.worker.process(make_msg(2, text="b"))
    assert env.vk.users_calls == 1


# 7. Экранирование
class _Check(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.data = [], ""

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, d):
        self.data += d


async def test_escaping(make_env):
    env = make_env()
    await env.worker.process(make_msg(3, text="a < b > c & d <b>x</b>"))
    (text,) = texts_sent(env.bot)
    p = _Check()
    p.feed(text)
    assert all(t == "a" for t, _ in p.tags)  # никаких чужих тегов
    assert "a < b > c & d <b>x</b>" in p.data
    msg_link = [a for t, a in p.tags if "msgid" in a.get("href", "")][0]["href"]
    assert msg_link == "https://vk.ru/im?sel=c1&msgid=3"
    assert "&amp;msgid=3" in text


# 8. Лимиты
async def test_caption_limit_boundary(make_env):
    env = make_env(files={"u": b"X"})
    # подбираем текст так, чтобы весь HTML был ровно CAPTION_LIMIT
    await env.worker.process(make_msg(1, text="x", attachments=[photo_att(("u", 5, 5))]))
    overhead = len(env.bot.channel_calls()[0][1]["caption"]) - 1
    env.bot.calls.clear()
    # ровно CAPTION_LIMIT -> подпись; на символ больше -> отдельное сообщение
    text = "я" * (CAPTION_LIMIT - overhead)
    await env.worker.process(make_msg(2, text=text, attachments=[photo_att(("u", 5, 5))]))
    (call,) = env.bot.channel_calls()
    assert call[0] == "send_photo" and len(call[1]["caption"]) == CAPTION_LIMIT
    env.bot.calls.clear()
    await env.worker.process(make_msg(3, text="ю" + text, attachments=[photo_att(("u", 5, 5))]))
    assert [c[0] for c in env.bot.channel_calls()] == ["send_photo", "send_message"]
    assert env.bot.channel_calls()[0][1]["caption"] is None


async def test_long_caption_goes_separately_after_album(make_env):
    env = make_env(files={"u1": b"1", "u2": b"2"})
    text = "слово " * 300
    await env.worker.process(make_msg(1, text=text,
                                      attachments=[photo_att(("u1", 5, 5)), photo_att(("u2", 5, 5))]))
    calls = env.bot.channel_calls()
    assert [c[0] for c in calls] == ["send_media_group", "send_message"]
    assert all(m.caption is None for m in calls[0][1]["media"])
    assert calls[1][1]["parse_mode"] == "HTML"
    assert calls[1][1]["link_preview_options"].is_disabled is True


async def test_long_text_split(make_env):
    env = make_env()
    text = ("абв " * 3000).strip() + " & <конец>"
    await env.worker.process(make_msg(1, text=text, attachments=[video_att()]))
    parts = texts_sent(env.bot)
    assert len(parts) >= 3
    assert all(len(p) <= TEXT_LIMIT for p in parts)
    assert parts[0].startswith("Чат 1\n<a href=")
    assert all("Чат 1" not in p for p in parts[1:])
    assert parts[-1].endswith("Ссылка на сообщение</a>")
    assert "[вложения: видео]" in parts[-1]
    assert not any("Ссылка на сообщение" in p for p in parts[:-1])
    body = "".join(html.unescape(p) for p in parts)
    for word in ("&", "<конец>"):
        assert word in body
    # слова не разорваны: количество «абв» сохранено
    assert body.count("абв") == 3000


async def test_split_without_whitespace_cuts_by_chars(make_env):
    env = make_env()
    await env.worker.process(make_msg(1, text="&" * 10000))
    parts = texts_sent(env.bot)
    assert all(len(p) <= TEXT_LIMIT for p in parts)
    # ни одна сущность &amp; не разорвана: после unescape ровно 10000 амперсандов в тексте
    body = "".join(html.unescape(p) for p in parts)
    assert body.count("&") == 10000 + body.count("&msgid")  # + амперсанд из ссылки


# 9. Фото: лимит размера
async def test_photo_size_fallback(make_env):
    big = b"0" * (PHOTO_MAX_BYTES + 1)
    env = make_env(files={"big": big, "small": b"ok"})
    await env.worker.process(make_msg(1, attachments=[photo_att(("small", 100, 100), ("big", 4000, 3000))]))
    assert env.http_calls == ["big", "small"]
    (call,) = env.bot.channel_calls()
    assert call[1]["photo"].data == b"ok"


# 11. Ошибка Telegram
async def test_telegram_error(make_env):
    bot = FakeBot(fail_channel_times=99)
    env = make_env(bot=bot)
    await env.worker.process(make_msg(4, text="текст"))
    assert len(bot.channel_calls()) == 3
    (owner,) = bot.owner_calls()
    assert owner[1]["parse_mode"] is None
    assert "Чат 1" in owner[1]["text"] and "msgid=4" in owner[1]["text"] and "boom" in owner[1]["text"]
    assert await env.cursor() == 4
    assert not await env.dedup.seen_msg(PEER, 4)
    assert await env.redis.keys("vk:seen:content:*") == []


async def test_telegram_recovers_on_retry(make_env):
    bot = FakeBot(fail_channel_times=2)
    env = make_env(bot=bot)
    await env.worker.process(make_msg(4, text="текст"))
    assert len(bot.channel_calls()) == 3 and bot.owner_calls() == []
    assert await env.dedup.seen_msg(PEER, 4)


async def test_photo_download_failure_notifies_owner(make_env):
    env = make_env(files={})  # KeyError при скачивании
    await env.worker.process(make_msg(2, text="т", attachments=[photo_att(("nope", 5, 5))]))
    assert len(env.bot.owner_calls()) == 1 and env.bot.channel_calls() == []
    assert await env.cursor() == 2 and not await env.dedup.seen_msg(PEER, 2)


# 13. Порядок
async def test_order(make_env):
    env = make_env()
    q = asyncio.Queue()
    for i, t in enumerate(["один", "два", "три", "четыре"], start=1):
        q.put_nowait(make_msg(i, text=t))
    task = asyncio.create_task(consume(q, env.worker))
    await asyncio.wait_for(q.join(), 5)
    task.cancel()
    sent = texts_sent(env.bot)
    assert [next(w for w in ("один", "два", "три", "четыре") if w in s) for s in sent] == \
        ["один", "два", "три", "четыре"]
    assert await env.cursor() == 4


async def test_cursor_never_moves_back(make_env):
    env = make_env()
    await env.worker.process(make_msg(9, text="a"))
    await env.worker.process(make_msg(3, text="b"))
    assert await env.cursor() == 9
