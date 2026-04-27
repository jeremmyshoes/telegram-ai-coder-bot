"""Регистрация инструментов агента и их выполнение."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.providers.base import ToolDefinition
from bot.tools.sandbox import Sandbox
from bot.tools.web_search import (
    WebSearchError,
    format_results,
    web_search,
)

logger = logging.getLogger(__name__)


MAX_FILE_BYTES = 200_000  # 200 КБ — не отдаём в LLM огромные файлы


@dataclass
class ToolHandler:
    definition: ToolDefinition
    handler: Callable[[dict[str, Any]], Awaitable[str]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolHandler] = {}

    def register(self, tool: ToolHandler) -> None:
        self._tools[tool.definition.name] = tool

    def definitions(self) -> list[ToolDefinition]:
        return [t.definition for t in self._tools.values()]

    def has(self, name: str) -> bool:
        return name in self._tools

    async def call(self, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"[error] неизвестный инструмент: {name}"
        try:
            return await tool.handler(args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка инструмента %s", name)
            return f"[error] {type(exc).__name__}: {exc}"


def _resolve_path(workdir: Path, raw_path: str) -> Path:
    """Разрешает путь относительно workdir и запрещает выход наружу."""
    p = (workdir / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).resolve()
    workdir = workdir.resolve()
    try:
        p.relative_to(workdir)
    except ValueError as exc:
        raise PermissionError(f"путь выходит за пределы workdir: {raw_path}") from exc
    return p


def build_tool_registry(
    *,
    sandbox: Sandbox,
    sandbox_timeout: int,
    google_search_api_key: str = "",
    google_search_cse_id: str = "",
) -> ToolRegistry:
    workdir = sandbox.workdir
    reg = ToolRegistry()

    # ------------------------------------------------------------------ bash
    async def bash_handler(args: dict[str, Any]) -> str:
        command = (args.get("command") or "").strip()
        if not command:
            return "[error] command пустой"
        timeout = int(args.get("timeout") or sandbox_timeout)
        timeout = max(1, min(timeout, sandbox_timeout * 4))
        result = await sandbox.run(command, timeout=timeout)
        return result.format()

    reg.register(
        ToolHandler(
            definition=ToolDefinition(
                name="bash",
                description=(
                    "Выполняет shell-команду в изолированной песочнице (Docker). "
                    "Используется для запуска кода, тестов, установки пакетов и т.д. "
                    "Рабочая директория сохраняется между вызовами."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Команда для bash -lc",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Таймаут в секундах",
                            "default": sandbox_timeout,
                        },
                    },
                    "required": ["command"],
                },
            ),
            handler=bash_handler,
        )
    )

    # -------------------------------------------------------------- read_file
    async def read_handler(args: dict[str, Any]) -> str:
        rel = args.get("path") or ""
        try:
            target = _resolve_path(workdir, rel)
        except PermissionError as exc:
            return f"[error] {exc}"
        if not target.exists():
            return f"[error] файл не найден: {rel}"
        if not target.is_file():
            return f"[error] не файл: {rel}"
        try:
            data = target.read_bytes()
        except OSError as exc:
            return f"[error] {exc}"
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
            return data.decode("utf-8", errors="replace") + "\n[файл обрезан]"
        return data.decode("utf-8", errors="replace")

    reg.register(
        ToolHandler(
            definition=ToolDefinition(
                name="read_file",
                description="Читает текстовый файл из рабочей директории.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Относительный путь"},
                    },
                    "required": ["path"],
                },
            ),
            handler=read_handler,
        )
    )

    # ------------------------------------------------------------- write_file
    async def write_handler(args: dict[str, Any]) -> str:
        rel = args.get("path") or ""
        content = args.get("content") or ""
        try:
            target = _resolve_path(workdir, rel)
        except PermissionError as exc:
            return f"[error] {exc}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"[ok] записано {len(content)} символов в {rel}"

    reg.register(
        ToolHandler(
            definition=ToolDefinition(
                name="write_file",
                description="Перезаписывает (или создаёт) файл с переданным содержимым.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            ),
            handler=write_handler,
        )
    )

    # -------------------------------------------------------------- edit_file
    async def edit_handler(args: dict[str, Any]) -> str:
        rel = args.get("path") or ""
        old = args.get("old_string") or ""
        new = args.get("new_string") or ""
        if not old:
            return "[error] old_string пустой"
        try:
            target = _resolve_path(workdir, rel)
        except PermissionError as exc:
            return f"[error] {exc}"
        if not target.exists():
            return f"[error] файл не найден: {rel}"
        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return "[error] old_string не найден в файле"
        if count > 1:
            return f"[error] old_string встречается {count} раз — уточните контекст, требуется уникальное совпадение"
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"[ok] заменено в {rel}"

    reg.register(
        ToolHandler(
            definition=ToolDefinition(
                name="edit_file",
                description=(
                    "Заменяет одно уникальное вхождение old_string на new_string. "
                    "Если old_string встречается >1 раза — вернёт ошибку."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            ),
            handler=edit_handler,
        )
    )

    # -------------------------------------------------------------------- ls
    async def ls_handler(args: dict[str, Any]) -> str:
        rel = args.get("path") or "."
        try:
            target = _resolve_path(workdir, rel)
        except PermissionError as exc:
            return f"[error] {exc}"
        if not target.exists():
            return f"[error] не найдено: {rel}"
        if target.is_file():
            return f"{target.name} (file, {target.stat().st_size} bytes)"
        items: list[str] = []
        for entry in sorted(target.iterdir()):
            kind = "dir" if entry.is_dir() else "file"
            size = entry.stat().st_size if entry.is_file() else 0
            items.append(f"{entry.name}\t{kind}\t{size}")
        return "\n".join(items) if items else "(пусто)"

    reg.register(
        ToolHandler(
            definition=ToolDefinition(
                name="ls",
                description="Возвращает листинг каталога в workdir.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                    },
                    "required": [],
                },
            ),
            handler=ls_handler,
        )
    )

    # ----------------------------------------------------------- web_search
    # Всегда доступен: DuckDuckGo работает без ключей. Если есть Google-ключи —
    # web_search() автоматически предпочтёт их (см. bot/tools/web_search.py).
    async def web_search_handler(args: dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        num = int(args.get("num_results") or 5)
        try:
            results, used = await web_search(
                query,
                google_api_key=google_search_api_key,
                google_cse_id=google_search_cse_id,
                num_results=num,
            )
        except WebSearchError as exc:
            return f"[error] web_search: {exc}"
        header = f"(via {used})\n" if results else ""
        return header + format_results(results)

    reg.register(
        ToolHandler(
            definition=ToolDefinition(
                name="web_search",
                description=(
                    "Ищет в интернете (DuckDuckGo по умолчанию или Google "
                    "Custom Search, если в .env заданы ключи). Используй "
                    "когда нужна свежая информация (события после "
                    "knowledge cutoff модели), факты, проверка цифр, "
                    "ссылки на источники. Возвращает топ-результатов с "
                    "title, link и snippet."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Поисковый запрос",
                        },
                        "num_results": {
                            "type": "integer",
                            "description": "Сколько результатов (1..20)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            handler=web_search_handler,
        )
    )

    return reg
