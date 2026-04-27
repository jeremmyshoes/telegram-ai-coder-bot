"""Веб-поиск для бота.

Поддерживается два провайдера:

1. **DuckDuckGo** (`duckduckgo`) — без API-ключей, парсим HTML lite-версии.
   Используется по умолчанию. Лимиты неофициальные, но для одного-двух
   юзеров хватает с запасом.
2. **Google Custom Search JSON API** (`google`) — требует
   `GOOGLE_SEARCH_API_KEY` и `GOOGLE_SEARCH_CSE_ID`. 100 запросов/сутки
   бесплатно. Используется если оба ключа заданы.

Удобный фасад: :func:`web_search` — выбирает провайдер автоматически.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass

import httpx

GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class WebSearchError(RuntimeError):
    """Поднимается, когда поиск не удался (нет ключа, ошибка API/HTML, и т.п.)."""


@dataclass(slots=True)
class SearchResult:
    title: str
    link: str
    snippet: str


def _clean(text: str) -> str:
    """Декодирует HTML-сущности и схлопывает пробелы/переводы строк."""
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _unwrap_ddg_url(raw: str) -> str:
    """DDG обёртывает результаты в редирект `/l/?uddg=<urlencoded>` —
    извлекаем настоящий URL.
    """
    if not raw:
        return raw
    if raw.startswith("//"):
        raw = "https:" + raw
    m = re.search(r"[?&]uddg=([^&]+)", raw)
    if m:
        from urllib.parse import unquote

        return unquote(m.group(1))
    return raw


def _parse_ddg_html(body: str, limit: int) -> list[SearchResult]:
    """Парсит HTML с https://html.duckduckgo.com/html/.

    Структура каждой карточки:
        <div class="result"> ...
          <a class="result__a" href="...">TITLE</a> ...
          <a class="result__snippet" ...>SNIPPET</a>
        </div>
    """
    results: list[SearchResult] = []
    # Делим по карточкам.
    blocks = re.split(r'<div[^>]*class="[^"]*result\b[^"]*"[^>]*>', body)
    for block in blocks[1:]:
        if len(results) >= limit:
            break
        link_match = re.search(
            r'<a[^>]*class="[^"]*result__a\b[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        )
        if not link_match:
            continue
        href, title_html = link_match.group(1), link_match.group(2)
        url = _unwrap_ddg_url(html_lib.unescape(href))
        title = _clean(title_html)

        snippet = ""
        snip_match = re.search(
            r'<a[^>]*class="[^"]*result__snippet\b[^"]*"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        )
        if snip_match:
            snippet = _clean(snip_match.group(1))
        else:
            # fallback: <div class="result__snippet">…</div>
            snip_match = re.search(
                r'<div[^>]*class="[^"]*result__snippet\b[^"]*"[^>]*>(.*?)</div>',
                block,
                re.DOTALL,
            )
            if snip_match:
                snippet = _clean(snip_match.group(1))

        if not url or not title:
            continue
        results.append(SearchResult(title=title, link=url, snippet=snippet))

    return results


async def duckduckgo_search(
    query: str,
    *,
    num_results: int = 5,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Веб-поиск через DuckDuckGo (без API-ключей).

    DDG-страница `html.duckduckgo.com/html/` обычно отдаёт ~30 результатов
    в одном ответе, так что для запросов до 25 источников хватает одного
    HTTP-запроса.
    """
    query = (query or "").strip()
    if not query:
        raise WebSearchError("пустой запрос")

    num = max(1, min(int(num_results), 25))
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }
    data = {"q": query, "kl": "wt-wt"}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as http:
            r = await http.post(DDG_HTML_URL, data=data)
    except httpx.HTTPError as exc:
        raise WebSearchError(f"сеть: {exc}") from exc

    if r.status_code != 200:
        raise WebSearchError(f"DuckDuckGo HTTP {r.status_code}")

    body = r.text or ""
    if ("anomaly" in body.lower() or "blocked" in body.lower()) and "result__a" not in body:
        raise WebSearchError(
            "DuckDuckGo временно блокирует запросы (rate-limit). Повторите через минуту."
        )

    results = _parse_ddg_html(body, num)
    if not results:
        # Не сломались, но пусто. Возвращаем пустой список —
        # вызывающий код покажет «ничего не найдено».
        return []
    return results


async def _google_search_page(
    *,
    http: httpx.AsyncClient,
    api_key: str,
    cse_id: str,
    query: str,
    start: int,
    num: int,
) -> list[SearchResult]:
    """Один пейдж Google CSE (до 10 результатов)."""
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": max(1, min(num, 10)),
        "start": max(1, start),
    }
    try:
        r = await http.get(GOOGLE_CSE_URL, params=params)
    except httpx.HTTPError as exc:
        raise WebSearchError(f"сеть: {exc}") from exc

    if r.status_code != 200:
        try:
            err = r.json().get("error", {}).get("message") or r.text[:300]
        except Exception:  # noqa: BLE001
            err = r.text[:300]
        raise WebSearchError(f"HTTP {r.status_code}: {err}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise WebSearchError(f"некорректный JSON ответ: {exc}") from exc

    items = payload.get("items") or []
    out: list[SearchResult] = []
    for it in items:
        out.append(
            SearchResult(
                title=str(it.get("title") or "").strip(),
                link=str(it.get("link") or "").strip(),
                snippet=str(it.get("snippet") or "").strip().replace("\n", " "),
            )
        )
    return out


async def google_search(
    query: str,
    *,
    api_key: str,
    cse_id: str,
    num_results: int = 5,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Поиск через Google Custom Search JSON API.

    CSE отдаёт максимум 10 результатов на запрос; для больших значений
    `num_results` (до 30) делаем несколько последовательных запросов с
    параметром `start`.
    """
    query = (query or "").strip()
    if not query:
        raise WebSearchError("пустой запрос")
    if not api_key or not cse_id:
        raise WebSearchError(
            "не настроены GOOGLE_SEARCH_API_KEY или GOOGLE_SEARCH_CSE_ID в .env"
        )

    total = max(1, min(int(num_results), 30))
    results: list[SearchResult] = []
    async with httpx.AsyncClient(timeout=timeout) as http:
        start = 1
        while len(results) < total:
            need = min(10, total - len(results))
            page = await _google_search_page(
                http=http,
                api_key=api_key,
                cse_id=cse_id,
                query=query,
                start=start,
                num=need,
            )
            if not page:
                break
            results.extend(page)
            if len(page) < need:
                # CSE больше не отдаёт — не дожимаем дальше.
                break
            start += len(page)
    return results[:total]


async def web_search(
    query: str,
    *,
    google_api_key: str = "",
    google_cse_id: str = "",
    provider: str = "auto",
    num_results: int = 5,
    timeout: float = 15.0,
) -> tuple[list[SearchResult], str]:
    """Унифицированный веб-поиск.

    provider:
      - "auto" (по умолчанию): Google если ключи заданы, иначе DuckDuckGo.
      - "google": строго Google (ошибка если ключи не заданы).
      - "duckduckgo" / "ddg": строго DuckDuckGo.

    Возвращает кортеж ``(results, provider_used)`` — имя реально
    использованного провайдера, чтобы UI мог показать «via DuckDuckGo».
    """
    p = (provider or "auto").lower().strip()
    if p in {"ddg", "duckduckgo"}:
        return await duckduckgo_search(query, num_results=num_results, timeout=timeout), "duckduckgo"
    if p == "google":
        return (
            await google_search(
                query,
                api_key=google_api_key,
                cse_id=google_cse_id,
                num_results=num_results,
                timeout=timeout,
            ),
            "google",
        )
    # auto
    if google_api_key and google_cse_id:
        try:
            return (
                await google_search(
                    query,
                    api_key=google_api_key,
                    cse_id=google_cse_id,
                    num_results=num_results,
                    timeout=timeout,
                ),
                "google",
            )
        except WebSearchError:
            # Google упал — пробуем DDG как fallback.
            pass
    return await duckduckgo_search(query, num_results=num_results, timeout=timeout), "duckduckgo"


def format_results(results: list[SearchResult]) -> str:
    """Строковая выдача результатов поиска (для tool-output и /search)."""
    if not results:
        return "(ничего не найдено)"
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        title = r.title or "(без заголовка)"
        lines.append(f"{i}. {title}\n   {r.link}\n   {r.snippet}")
    return "\n\n".join(lines)
