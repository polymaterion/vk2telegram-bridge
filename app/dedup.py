import hashlib

from .constants import DEDUP_TTL_SEC


def cursor_key(peer_id: int) -> str:
    return f"vk:cursor:{peer_id}"


def msg_key(peer_id: int, cmid: int) -> str:
    return f"vk:seen:msg:{peer_id}:{cmid}"


def content_key(content_hash: str) -> str:
    return f"vk:seen:content:{content_hash}"


def compute_content_hash(combined_text: str, photo_bytes: list[bytes]) -> str:
    norm = " ".join(combined_text.casefold().split())
    photo_part = b"\x00".join(hashlib.sha256(b).hexdigest().encode() for b in photo_bytes)
    return hashlib.sha256(norm.encode() + b"\x00" + photo_part).hexdigest()


class Dedup:
    """Ключи Redis: курсор, дедупликация «а» (сообщение) и «б» (содержимое).
    Клиент Redis должен быть создан с decode_responses=True."""

    def __init__(self, redis, ttl: int = DEDUP_TTL_SEC):
        self.r = redis
        self.ttl = ttl

    async def get_cursor(self, peer_id: int) -> int | None:
        value = await self.r.get(cursor_key(peer_id))
        return None if value is None else int(value)

    async def set_cursor(self, peer_id: int, cmid: int) -> None:
        await self.r.set(cursor_key(peer_id), cmid)

    async def advance_cursor(self, peer_id: int, cmid: int) -> None:
        current = await self.get_cursor(peer_id)
        if current is None or cmid > current:
            await self.set_cursor(peer_id, cmid)

    async def seen_msg(self, peer_id: int, cmid: int) -> bool:
        return bool(await self.r.exists(msg_key(peer_id, cmid)))

    async def mark_msg(self, peer_id: int, cmid: int) -> None:
        await self.r.set(msg_key(peer_id, cmid), "1", ex=self.ttl)

    async def content_owner(self, content_hash: str) -> str | None:
        return await self.r.get(content_key(content_hash))

    async def mark_sent(self, peer_id: int, cmid: int, content_hash: str) -> None:
        await self.mark_msg(peer_id, cmid)
        await self.r.set(content_key(content_hash), f"{peer_id}:{cmid}", ex=self.ttl)
