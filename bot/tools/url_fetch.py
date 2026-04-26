"""Загрузка и очистка содержимого веб-страниц.

Используется perplexity-style режимом `/search`: после получения списка
ссылок от поисковика мы качаем страницы, вырезаем HTML/скрипты/стили и
передаём чистый текст модели для синтеза ответа.

Минимум зависимостей: только `httpx` (он уже есть для web_search). HTML
парсим регулярками — этого достаточно чтобы выкинуть теги/script/style и
оставить читабельный текст.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Регэкспы для зачистки HTML.
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_NOSCRIPT_RE = re.compile(r"<noscript\b[^>]*>.*?</noscript>", re.IGNORECASE | re.DOTALL)
_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")
_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class FetchedPage:
    """Результат скачивания одной страницы."""

    url: str
    title: str
    text: str
    error: str | None = None  # если страница не скачалась — текст ошибки


def _extract_text(html: str) -> tuple[str, str]:
    """Вырезает теги/скрипты/стили, возвращает (title, plaintext)."""
    title_match = _TITLE_RE.search(html)
    title = ""
    if title_match:
        title = html_lib.unescape(title_match.group(1)).strip()
        title = _WS_RE.sub(" ", title)

    body = _SCRIPT_RE.sub(" ", html)
    body = _STYLE_RE.sub(" ", body)
    body = _NOSCRIPT_RE.sub(" ", body)
    body = _HEAD_RE.sub(" ", body)
    # Превращаем блочные теги в перевод строки до удаления тегов, чтобы
    # абзацы не слипались в одну простыню.
    body = re.sub(
        r"</(p|div|section|article|li|ul|ol|h1|h2|h3|h4|h5|h6|br|tr)\s*>",
        "\n",
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = _TAG_RE.sub("", body)
    body = html_lib.unescape(body)
    body = _WS_RE.sub(" ", body)
    body = _NL_RE.sub("\n\n", body)
    body = "\n".join(line.strip() for line in body.splitlines())
    body = body.strip()
    return title, body


async def fetch_page(
    url: str,
    *,
    timeout: float = 12.0,
    max_chars: int = 4000,
) -> FetchedPage:
    """Скачивает URL, возвращает очищенный текст обрезанный до max_chars.

    На ошибку не бросает — возвращает FetchedPage с заполненным error.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en;q=0.9,ru;q=0.8"},
        ) as client:
            r = await client.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        if "html" not in ctype and "text" not in ctype:
            return FetchedPage(url=url, title="", text="", error=f"unsupported content-type: {ctype or 'unknown'}")
        title, text = _extract_text(r.text)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return FetchedPage(url=url, title=title, text=text)
    except httpx.HTTPStatusError as exc:
        return FetchedPage(url=url, title="", text="", error=f"HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        return FetchedPage(url=url, title="", text="", error=f"network: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_page failed for %s: %s", url, exc)
        return FetchedPage(url=url, title="", text="", error=str(exc))


async def fetch_pages(
    urls: list[str],
    *,
    timeout: float = 12.0,
    max_chars: int = 4000,
    max_concurrency: int = 5,
) -> list[FetchedPage]:
    """Параллельно качает URL'ы. Сохраняет порядок входного списка."""
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(url: str) -> FetchedPage:
        async with sem:
            return await fetch_page(url, timeout=timeout, max_chars=max_chars)

    return await asyncio.gather(*(_one(u) for u in urls))
