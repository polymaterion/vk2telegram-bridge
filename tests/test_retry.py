import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from app.telegram_sink import with_retries


async def test_retry_after_waits_and_counts_as_attempt():
    sleeps, calls = [], []

    async def sleep(s):
        sleeps.append(s)

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="flood", retry_after=7)
        return "ok"

    assert await with_retries(fn, sleep=sleep) == "ok"
    assert sleeps == [7, 7] and len(calls) == 3


async def test_backoff_and_exhaustion():
    sleeps, calls = [], []

    async def sleep(s):
        sleeps.append(s)

    async def fn():
        calls.append(1)
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await with_retries(fn, sleep=sleep)
    assert len(calls) == 3 and sleeps == [1, 2]
