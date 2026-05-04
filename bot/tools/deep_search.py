"""Perplexity-style deep web search.

Pipeline:

1. **Query expansion** — LLM разбивает вопрос юзера на 3-5 поисковых
   под-запросов (включая англоязычные варианты для русских вопросов и
   наоборот), чтобы покрыть тему с разных сторон.
2. **Multi-search** — каждый под-запрос параллельно идёт через web_search
   (Google CSE если есть ключи, иначе DuckDuckGo).
3. **Дедуп** — результаты сливаются и фильтруются по канонической ссылке
   (схема + хост + путь без tracking-параметров).
4. **Fetch** — параллельная загрузка содержимого топ-N страниц.
5. **Synth** — LLM пишет ответ с цитатами `[1]`, `[2]`, … + блок
   «Похожие вопросы» в конце.

Модуль предоставляет высокоуровневую функцию :func:`run_deep_search` и
вспомогательные кусочки (`expand_query`, `dedupe_results`,
`multi_search`), которые можно тестировать отдельно.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bot.providers.base import Message, ProviderError
from bot.providers.openai_compat import OpenAICompatProvider
from bot.tools.url_fetch import FetchedPage, fetch_pages
from bot.tools.web_search import SearchResult, WebSearchError, web_search

logger = logging.getLogger(__name__)

# tracking-параметры, которые надо отбрасывать при канонизации URL.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "fbclid",
        "gclid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "ref",
        "ref_src",
        "ref_url",
        "igshid",
        "feature",
        "si",
    }
)


def _canonical_url(url: str) -> str:
    """Канонизирует URL для дедупа.

    - lower-case схема и хост, убираем `www.`
    - убираем фрагмент (`#…`)
    - выкидываем tracking-параметры из query
    - нормализуем хвостовой слеш
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()

    # Нормализуем http/https в одно и то же — большинство сайтов в 2026
    # уже имеют https, и отличий по контенту между http и https быть не
    # должно. Это убирает фантомные дубли.
    scheme = "https"
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query_pairs.sort()
    query = urlencode(query_pairs)

    return urlunparse((scheme, host, path, "", query, ""))


def dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    """Убирает дубли по канонической ссылке, сохраняя первый встреченный результат."""
    seen: set[str] = set()
    out: list[SearchResult] = []
    for r in results:
        if not r.link:
            continue
        canon = _canonical_url(r.link)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(r)
    return out


_QUERY_EXPANSION_SYSTEM = (
    "Ты помогаешь поисковому ассистенту. Тебе дают вопрос юзера; ты "
    "возвращаешь 4-6 коротких поисковых запросов, которые покрывают разные "
    "стороны вопроса. Правила:\n"
    "- Каждый запрос — НЕ вопрос, а поисковая строка (как в Google), 2-7 слов.\n"
    "- Включи английский вариант если оригинальный вопрос на русском (и наоборот) "
    "  — это резко расширит охват источников.\n"
    "- НЕ повторяй один и тот же запрос разными словами.\n"
    "- НЕ добавляй кавычки/звёздочки/нумерацию.\n"
    'Верни СТРОГО JSON-объект вида {"queries": ["…", "…", "…"]} без какого-либо '
    "текста вокруг."
)


def _parse_expanded_queries(text: str, fallback: str) -> list[str]:
    """Парсит JSON-ответ модели, возвращает список запросов.

    На любую ошибку — возвращает [fallback], чтобы пайплайн не сломался.
    """
    if not text:
        return [fallback]
    # Иногда модели оборачивают JSON в ```json … ``` — выкинем.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # fallback: ищем JSON-объект внутри текста
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not m:
            return [fallback]
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [fallback]
    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list):
        return [fallback]
    out: list[str] = []
    for q in queries:
        if isinstance(q, str):
            qs = q.strip().strip('"').strip("'")
            if qs and qs not in out:
                out.append(qs)
    return out or [fallback]


async def expand_query(
    query: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    max_queries: int = 6,
) -> list[str]:
    """Возвращает 1..max_queries поисковых под-запросов.

    Первым в списке всегда идёт оригинальный запрос юзера, чтобы даже при
    деградации LLM (timeout, странный вывод) поиск продолжал работать.
    """
    base_query = (query or "").strip()
    if not base_query:
        return []

    provider = OpenAICompatProvider(name="openai", api_key=api_key, base_url=base_url)
    try:
        resp = await provider.complete(
            messages=[
                Message(role="system", content=_QUERY_EXPANSION_SYSTEM),
                Message(role="user", content=f"Вопрос: {base_query}"),
            ],
            model=model,
        )
    except ProviderError as exc:
        logger.warning("expand_query: provider error %s — fallback to single query", exc)
        return [base_query]
    except Exception as exc:  # noqa: BLE001
        logger.warning("expand_query: %s — fallback to single query", exc)
        return [base_query]

    parsed = _parse_expanded_queries(resp.content or "", fallback=base_query)
    # Гарантируем что оригинал всегда есть и идёт первым.
    out = [base_query]
    for q in parsed:
        if q.lower() == base_query.lower():
            continue
        out.append(q)
        if len(out) >= max_queries:
            break
    return out


async def multi_search(
    queries: list[str],
    *,
    google_api_key: str,
    google_cse_id: str,
    per_query: int = 6,
    timeout: float = 15.0,
) -> tuple[list[SearchResult], list[str]]:
    """Параллельно ищет все ``queries`` и сливает результаты с дедупом.

    Возвращает (объединённые_результаты, провайдеры_по_порядку).
    Порядок результатов: сначала первый результат каждого под-запроса,
    потом вторые и т.д. (round-robin) — это балансирует выдачу между
    разными запросами вместо «всё первые 10 от первого запроса».
    """
    if not queries:
        return [], []

    async def _one(q: str) -> tuple[list[SearchResult], str]:
        try:
            return await web_search(
                q,
                google_api_key=google_api_key,
                google_cse_id=google_cse_id,
                num_results=per_query,
                timeout=timeout,
            )
        except WebSearchError as exc:
            logger.warning("multi_search: query %r failed: %s", q, exc)
            return [], ""

    pairs = await asyncio.gather(*(_one(q) for q in queries))
    providers_used = [p for _, p in pairs if p]

    # Round-robin: берём 1-й результат из каждой пачки, потом 2-й, и т.д.
    interleaved: list[SearchResult] = []
    max_len = max((len(r) for r, _ in pairs), default=0)
    for i in range(max_len):
        for results, _ in pairs:
            if i < len(results):
                interleaved.append(results[i])

    return dedupe_results(interleaved), providers_used


@dataclass(slots=True)
class DeepSearchOutput:
    """Результат deep-search: то что показываем юзеру."""

    answer: str
    cited_sources: list[tuple[int, str, str]]  # (n, title, url)
    follow_ups: list[str]
    expanded_queries: list[str]
    providers_used: list[str]
    fetched_count: int


_SYNTH_SYSTEM = (
    "Ты ассистент в стиле Perplexity. Тебе дан вопрос юзера и пачка "
    "веб-источников, пронумерованных [1], [2], …\n\n"
    "Правила:\n"
    "1. Отвечай на языке вопроса юзера (русский → русский, English → English).\n"
    "2. Используй ТОЛЬКО информацию из источников ниже. Если её "
    "недостаточно — скажи прямо «по этим источникам неясно».\n"
    "3. После каждого утверждения ставь номер(а) источника в квадратных "
    "скобках, например: «Python — динамически типизированный язык [1][3].»\n"
    "4. Структурируй ответ: 1-2 абзаца саммари + при необходимости "
    "маркированные пункты по подтемам. Markdown допустим.\n"
    "5. НЕ выдумывай источников и не придумывай ссылок.\n"
    "6. НЕ дублируй список источников в конце ответа — интерфейс "
    "пришлёт его отдельно.\n"
    "7. В САМОМ КОНЦЕ ответа добавь блок ровно такого формата:\n"
    "\n"
    "FOLLOW_UPS:\n"
    "- первый уточняющий вопрос\n"
    "- второй уточняющий вопрос\n"
    "- третий уточняющий вопрос\n"
    "\n"
    "Это 3 коротких связанных вопроса для углубления темы. Они должны быть "
    "сформулированы как полные вопросы, готовые к копи-пасту в /search."
)


_FOLLOW_UP_RE = re.compile(
    r"FOLLOW_UPS:\s*\n((?:\s*[-*]\s*.+\n?)+)", flags=re.IGNORECASE
)


def _split_answer_and_followups(text: str) -> tuple[str, list[str]]:
    """Отрезает блок FOLLOW_UPS: от ответа, возвращает (answer, follow_ups)."""
    m = _FOLLOW_UP_RE.search(text)
    if not m:
        return text.strip(), []
    follow_block = m.group(1)
    answer = text[: m.start()].rstrip()
    follow_ups: list[str] = []
    for line in follow_block.splitlines():
        line = line.strip()
        if not line:
            continue
        # Срезаем маркер `- ` или `* `.
        line = re.sub(r"^[-*]\s+", "", line)
        line = line.strip().strip('"').strip("'")
        if line:
            follow_ups.append(line)
    return answer, follow_ups[:5]


async def run_deep_search(
    query: str,
    *,
    api_key: str,
    base_url: str,
    synth_model: str,
    google_api_key: str = "",
    google_cse_id: str = "",
    max_sources: int = 15,
    per_subquery: int = 6,
    fetch_timeout: float = 10.0,
    fetch_max_chars: int = 2200,
    fetch_concurrency: int = 10,
    expand_queries: bool = True,
    on_progress: object = None,
) -> DeepSearchOutput:
    """Запускает полный deep-search пайплайн.

    on_progress: опциональный async callable(stage: str, info: dict) — для
    UI-прогресса. Этапы: "expand", "search", "fetch", "synth".
    """

    async def _notify(stage: str, info: dict) -> None:
        if on_progress is None:
            return
        try:
            await on_progress(stage, info)  # type: ignore[misc]
        except Exception:  # noqa: BLE001
            pass

    # 1. Query expansion (можно отключить флагом — для быстрого режима).
    if expand_queries:
        await _notify("expand", {})
        expanded = await expand_query(
            query,
            api_key=api_key,
            base_url=base_url,
            model=synth_model,
        )
    else:
        expanded = [query.strip()] if query.strip() else []

    # 2. Multi-search.
    await _notify("search", {"queries": expanded})
    merged, providers_used = await multi_search(
        expanded,
        google_api_key=google_api_key,
        google_cse_id=google_cse_id,
        per_query=per_subquery,
    )

    if not merged:
        raise WebSearchError("по объединённому запросу ничего не найдено")

    # Берём кандидатов с запасом x2: часть страниц не откроется, часть
    # отдаст плейсхолдер. Лимит сверху 30 чтобы не выжигать сеть.
    candidates = merged[: min(max(max_sources * 2, max_sources + 5), 30)]
    urls = [r.link for r in candidates if r.link]

    # 3. Fetch.
    await _notify("fetch", {"count": len(urls)})
    pages: list[FetchedPage] = await fetch_pages(
        urls,
        timeout=fetch_timeout,
        max_chars=fetch_max_chars,
        max_concurrency=fetch_concurrency,
    )

    usable: list[tuple[str, str, str]] = []  # (title, url, text)
    for res, page in zip(candidates[: len(pages)], pages, strict=True):
        if page.error or not page.text:
            continue
        title = page.title or res.title or page.url
        usable.append((title, page.url, page.text))
        if len(usable) >= max_sources:
            break

    if not usable:
        # fallback: сниппеты из самой выдачи (без скачивания страниц)
        for res in candidates[:max_sources]:
            if not res.snippet:
                continue
            usable.append((res.title or res.link, res.link, res.snippet))
        if not usable:
            raise WebSearchError("источники недоступны (все 4xx/5xx или без текста)")

    # 4. Synth.
    await _notify("synth", {"sources": len(usable)})
    context_blocks: list[str] = []
    cited_sources: list[tuple[int, str, str]] = []
    for n, (title, url, text) in enumerate(usable, start=1):
        context_blocks.append(f"[{n}] {title}\nURL: {url}\n{text}")
        cited_sources.append((n, title, url))

    synth_provider = OpenAICompatProvider(
        name="openai", api_key=api_key, base_url=base_url
    )
    user_prompt = (
        f"Вопрос юзера: {query}\n\n"
        f"Вот {len(usable)} веб-источников, прочитанных и обрезанных:\n\n"
        + "\n\n---\n\n".join(context_blocks)
        + "\n\nДай связный ответ с цитатами [N] и блоком FOLLOW_UPS в конце."
    )

    response = await synth_provider.complete(
        messages=[
            Message(role="system", content=_SYNTH_SYSTEM),
            Message(role="user", content=user_prompt),
        ],
        model=synth_model,
    )
    raw_answer = (response.content or "").strip()
    answer, follow_ups = _split_answer_and_followups(raw_answer)

    return DeepSearchOutput(
        answer=answer or "(модель вернула пустой ответ)",
        cited_sources=cited_sources,
        follow_ups=follow_ups,
        expanded_queries=expanded,
        providers_used=providers_used,
        fetched_count=len(usable),
    )
