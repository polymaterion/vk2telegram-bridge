import html
from dataclasses import dataclass

from .constants import CAPTION_LIMIT, MSG_LINK_ID_FIELD, TEXT_LIMIT


def esc(s: str) -> str:
    return html.escape(s)


def build_message_url(peer_id: int, message) -> str:
    return (
        f"https://vk.ru/im?sel=c{peer_id - 2000000000}"
        f"&msgid={getattr(message, MSG_LINK_ID_FIELD)}"
    )


def build_head(title: str, author_name: str, author_url: str) -> str:
    return f'{esc(title)}\n<a href="{html.escape(author_url, quote=True)}">{esc(author_name)}</a>'


def build_tail(text: str, other_types: list[str], message_url: str) -> str:
    lines = []
    if text and other_types:
        lines.append(esc(f"[вложения: {', '.join(other_types)}]"))
    lines.append(f'<a href="{html.escape(message_url, quote=True)}">Ссылка на сообщение</a>')
    return "\n".join(lines)


def _fit_prefix(text: str, budget: int) -> int:
    """Наибольшее n, при котором len(esc(text[:n])) <= budget."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(esc(text[:mid])) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _cut_point(text: str, n: int) -> int:
    """Граница пробела/перевода строки не правее n; без границы режем по символам."""
    p = max(text.rfind(" ", 0, n + 1), text.rfind("\n", 0, n + 1), text.rfind("\t", 0, n + 1))
    return p if p > 0 else max(n, 1)


def split_html(head: str, text: str, tail: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Делит публикацию на части с длиной HTML <= limit. Шапка — в первой части,
    вложения и ссылка — в последней. Режется исходный текст, не HTML, поэтому
    сущности (&amp;) и теги не разрываются."""
    body = f"\n{esc(text)}" if text else ""
    full = f"{head}{body}\n{tail}"
    if len(full) <= limit or not text:
        return [full]

    parts: list[str] = []
    rest = text
    first = True
    while True:
        rest = rest.lstrip()
        prefix = head + "\n" if first else ""
        base = limit - len(prefix)
        rest_html = esc(rest)
        if len(rest_html) <= base - 1 - len(tail):
            parts.append(f"{prefix}{rest_html}\n{tail}")
            break
        if len(rest_html) <= base:
            parts.append(f"{prefix}{rest_html}")
            parts.append(tail)
            break
        n = _fit_prefix(rest, base)
        cut = _cut_point(rest, n)
        parts.append(prefix + esc(rest[:cut].rstrip()))
        rest = rest[cut:]
        first = False
        if not rest.strip():
            parts.append(tail)
            break
    return parts


@dataclass
class Publication:
    caption: str | None  # подпись к фото (если помещается в CAPTION_LIMIT)
    texts: list[str]  # текстовые части, отправляемые отдельными сообщениями


def build_publication(
    *,
    title: str,
    author_name: str,
    author_url: str,
    text: str,
    other_types: list[str],
    message_url: str,
    has_photos: bool,
) -> Publication:
    head = build_head(title, author_name, author_url)
    tail = build_tail(text, other_types, message_url)
    body = f"\n{esc(text)}" if text else ""
    full = f"{head}{body}\n{tail}"
    if has_photos and len(full) <= CAPTION_LIMIT:
        return Publication(caption=full, texts=[])
    return Publication(caption=None, texts=split_html(head, text, tail))
