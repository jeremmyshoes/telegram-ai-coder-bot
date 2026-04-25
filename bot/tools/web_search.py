"""Google Programmable Search Engine (Custom Search JSON API).

Документация: https://developers.google.com/custom-search/v1/using_rest

Требует два значения (положить в .env):
- GOOGLE_SEARCH_API_KEY — обычный API-ключ из Google Cloud Console.
- GOOGLE_SEARCH_CSE_ID — id поискового движка из programmablesearchengine.google.com.

Бесплатно: 100 запросов/сутки. Дальше ~$5/1000.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"


class WebSearchError(RuntimeError):
    """Поднимается, когда поиск не удался (нет ключа, ошибка API, и т.п.)."""


@dataclass(slots=True)
class SearchResult:
    title: str
    link: str
    snippet: str


async def google_search(
    query: str,
    *,
    api_key: str,
    cse_id: str,
    num_results: int = 5,
    timeout: float = 15.0,
) -> list[SearchResult]:
    """Выполняет поиск Google и возвращает список SearchResult.

    Поднимает WebSearchError при пустом запросе, отсутствии ключей или ошибке API.
    """
    query = (query or "").strip()
    if not query:
        raise WebSearchError("пустой запрос")
    if not api_key or not cse_id:
        raise WebSearchError(
            "не настроены GOOGLE_SEARCH_API_KEY или GOOGLE_SEARCH_CSE_ID в .env"
        )

    num = max(1, min(int(num_results), 10))
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": num,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            r = await http.get(GOOGLE_CSE_URL, params=params)
    except httpx.HTTPError as exc:
        raise WebSearchError(f"сеть: {exc}") from exc

    if r.status_code != 200:
        # Пытаемся вытащить осмысленный message из тела
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
    results: list[SearchResult] = []
    for it in items:
        results.append(
            SearchResult(
                title=str(it.get("title") or "").strip(),
                link=str(it.get("link") or "").strip(),
                snippet=str(it.get("snippet") or "").strip().replace("\n", " "),
            )
        )
    return results


def format_results(results: list[SearchResult]) -> str:
    """Удобная строковая выдача результатов поиска (для tool-output и /search)."""
    if not results:
        return "(ничего не найдено)"
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        title = r.title or "(без заголовка)"
        lines.append(f"{i}. {title}\n   {r.link}\n   {r.snippet}")
    return "\n\n".join(lines)
