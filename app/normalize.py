from dataclasses import dataclass, field

from .constants import ALBUM_LIMIT

TYPE_NAMES = {
    "video": "видео",
    "doc": "документ",
    "audio": "аудио",
    "audio_message": "голосовое",
    "sticker": "стикер",
    "link": "ссылка",
    "poll": "опрос",
    "wall": "запись",
    "market": "товар",
    "graffiti": "граффити",
}


@dataclass
class Parsed:
    texts: list[str] = field(default_factory=list)
    photos: list[dict] = field(default_factory=list)  # объекты photo (первые ALBUM_LIMIT)
    other_types: list[str] = field(default_factory=list)

    @property
    def combined_text(self) -> str:
        return "\n".join(self.texts)


def _walk(node: dict, texts: list, photos: list, other: list) -> None:
    text = node.get("text")
    if isinstance(text, str) and text:
        texts.append(text)
    for att in node.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        att_type = att.get("type")
        if not att_type:
            continue
        if att_type == "photo":
            photo = att.get("photo")
            if isinstance(photo, dict):
                photos.append(photo)
        else:
            name = TYPE_NAMES.get(att_type, str(att_type))
            if name not in other:
                other.append(name)
    for fwd in node.get("fwd_messages") or []:
        if isinstance(fwd, dict):
            _walk(fwd, texts, photos, other)


def parse_message(m) -> Parsed:
    """Обход сообщения и всех fwd_messages в глубину в порядке документа.
    reply_message не разбирается."""
    texts: list[str] = []
    photos: list[dict] = []
    other: list[str] = []
    root = {
        "text": m.text,
        "attachments": m.attachments,
        "fwd_messages": m.fwd_messages,
    }
    _walk(root, texts, photos, other)
    return Parsed(texts=texts, photos=photos[:ALBUM_LIMIT], other_types=other)
