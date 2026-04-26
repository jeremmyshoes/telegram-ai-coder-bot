"""Конвертер Markdown → Telegram HTML.

LLM-провайдеры (OpenAI, Anthropic и пр.) почти всегда отвечают в Markdown:
``**bold**``, ``*italic*``, ```` `code` ````, ``[ссылка](url)``, ``# заголовок``,
тройные бэктики для код-блоков. Telegram таких разметок не понимает в режиме
parse_mode=None — пользователь видит «голые» звёздочки и решётки.

Эта утилита превращает Markdown в **whitelist HTML**, который понимает
Telegram (см. https://core.telegram.org/bots/api#html-style):

- `**жирный**` / `__жирный__` → `<b>жирный</b>`
- `*курсив*` / `_курсив_`     → `<i>курсив</i>`
- `~~зачёркнутый~~`           → `<s>зачёркнутый</s>`
- `` `код` ``                 → `<code>код</code>`
- ```` ```...``` ````         → `<pre>...</pre>` (с опц. `<code class="language-…">`)
- `[текст](url)`              → `<a href="url">текст</a>`
- `# / ## / ###`              → `<b>...</b>` (Telegram не умеет heading'и)
- `> цитата`                  → `<blockquote>...</blockquote>`

Всё остальное остаётся обычным текстом. Спец-символы `<`, `>`, `&` HTML-экранируются.
"""

from __future__ import annotations

import re

# Допустимые в Telegram URL-схемы для <a href="...">.
# Всё прочее (data:, javascript:, file:…) игнорируется — ссылка превращается
# в обычный текст.
_SAFE_LINK_SCHEMES = ("http://", "https://", "tg://", "mailto:")


def _escape(text: str) -> str:
    """HTML-экранирование (без кавычек — Telegram их не требует в тексте)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(text: str) -> str:
    """То же что _escape, плюс двойные кавычки — для значений атрибутов."""
    return _escape(text).replace('"', "&quot;")


def _is_safe_link(url: str) -> bool:
    u = url.strip().lower()
    return any(u.startswith(s) for s in _SAFE_LINK_SCHEMES)


def md_to_telegram_html(text: str) -> str:  # noqa: C901  — сложность естественная
    """Превращает Markdown в Telegram-совместимый HTML.

    Безопасно для произвольного LLM-вывода: сначала вырезаются код-блоки
    (внутри них Markdown-парсинг отключён), потом остальной текст
    HTML-экранируется, и поверх натягиваются inline-теги.
    """
    if not text:
        return ""

    placeholders: dict[str, str] = {}

    def _stash(html: str) -> str:
        token = f"\x00MD{len(placeholders)}\x00"
        placeholders[token] = html
        return token

    # 1. ```fenced code blocks``` (с опциональным языком).
    def _fenced(m: re.Match[str]) -> str:
        lang = (m.group(1) or "").strip()
        body = m.group(2)
        body_esc = _escape(body)
        if lang:
            html = (
                f'<pre><code class="language-{_escape_attr(lang)}">'
                f"{body_esc}</code></pre>"
            )
        else:
            html = f"<pre>{body_esc}</pre>"
        return _stash(html)

    text = re.sub(
        r"```([^\n`]*)\n?(.*?)```",
        _fenced,
        text,
        flags=re.DOTALL,
    )

    # 2. Inline `code`. Захватываем только парные одиночные бэктики на той же строке,
    #    чтобы не съесть случайный одиночный `.
    def _inline_code(m: re.Match[str]) -> str:
        return _stash(f"<code>{_escape(m.group(1))}</code>")

    text = re.sub(r"`([^`\n]+)`", _inline_code, text)

    # 3. [text](url) — ссылки. Делаем до экранирования, чтобы скобки не превратились
    #    в &lt;.
    def _link(m: re.Match[str]) -> str:
        label = m.group(1)
        url = m.group(2).strip()
        if not _is_safe_link(url):
            return _stash(_escape(label))
        # URL экранируем как атрибут (особенно важно для кавычек, иначе href
        # развалится).
        return _stash(f'<a href="{_escape_attr(url)}">{_escape(label)}</a>')

    text = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", _link, text)

    # 4. Теперь экранируем всё остальное.
    text = _escape(text)

    # 5. Заголовки `# `, `## `, `### ` → жирный текст. Поддерживаем до уровня 6.
    text = re.sub(
        r"^(#{1,6})\s+(.+?)\s*$",
        lambda m: f"<b>{m.group(2)}</b>",
        text,
        flags=re.MULTILINE,
    )

    # 6. **bold** и __bold__ — нежадно, не пересекаем переводы строк.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__([^\n_]+)__", r"<b>\1</b>", text)

    # 7. *italic* и _italic_. Аккуратно: не должно цепляться к словам типа
    #    snake_case_var и к одиночным звёздам в перечислениях.
    #    Требуем чтобы перед открывающим была граница (начало строки/пробел/пунктуация),
    #    и чтобы внутри не было пустоты/переноса.
    text = re.sub(
        r"(?<![A-Za-z0-9_*])\*([^*\n]+?)\*(?![A-Za-z0-9_*])",
        r"<i>\1</i>",
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])",
        r"<i>\1</i>",
        text,
    )

    # 8. ~~strike~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)

    # 9. > цитата (одна строка). Несколько подряд не объединяем — Telegram
    #    отрисует каждую как отдельный <blockquote>, что приемлемо.
    text = re.sub(
        r"^&gt;\s+(.+?)\s*$",
        lambda m: f"<blockquote>{m.group(1)}</blockquote>",
        text,
        flags=re.MULTILINE,
    )

    # 10. Восстанавливаем заглушки для кода и ссылок.
    for token, html in placeholders.items():
        text = text.replace(token, html)

    return text
