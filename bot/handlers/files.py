"""Обработчик загрузки файлов и фотографий.

Логика:
- **Фото или картинка-документ** → отправляем модели как multimodal-vision запрос
  (caption — это промпт; если caption пуст — «Опиши изображение»). Параллельно
  сохраняем файл в рабочую папку, чтобы его можно было использовать в agent-режиме.
- **Текстовый файл** (utf-8 ≤ 256КБ) — содержимое прикладывается к промпту в виде
  ``<file name="...">…</file>`` и идёт в чат-режиме (или в agent-режиме).
- **Бинарный/большой документ** — сохраняем в workspace, агент сможет прочитать
  через инструмент `read_file`.
"""

from __future__ import annotations

import io
import logging
import time
from contextlib import suppress
from html import escape as html_escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Document, PhotoSize
from aiogram.types import Message as TgMessage

from bot.agent import SYSTEM_PROMPT_AGENT, SYSTEM_PROMPT_CHAT, Agent, AgentEvent
from bot.handlers.common import AppContext, is_allowed, send_llm_response
from bot.handlers.file_extract import (
    SUPPORTED_DOC_EXTS,
    extract_doc_by_ext,
    render_pdf_to_jpegs,
)
from bot.handlers.personas import apply_persona
from bot.providers.base import ImageData, ProviderError
from bot.tools import build_tool_registry
from bot.tools.sandbox import build_sandbox

logger = logging.getLogger(__name__)


MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ — лимит Telegram-bot API
MAX_TEXT_INLINE = 256 * 1024  # сколько байт текста цепляем к промпту
# Для документов (PDF/DOCX/XLSX) ограничение жёстче — после извлечения
# текст уже почти готов к prompt, и LLM лучше съест ~120K символов чем 256K.
MAX_EXTRACTED_DOC = 120 * 1024
# Максимум страниц PDF которые рендерим в картинки при OCR-fallback.
# Vision-запрос с большим числом картинок: дорого + медленно + риск 413.
MAX_OCR_PAGES = 6
TEXT_EXTS = {
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".ini", ".cfg", ".conf",
    ".toml", ".yaml", ".yml", ".json", ".xml", ".html", ".htm", ".css", ".js",
    ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java", ".kt", ".swift",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".php", ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".env", ".gitignore", ".dockerfile", ".makefile", ".lua", ".pl",
}
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def register_file_handlers(dp: Dispatcher, ctx: AppContext) -> None:
    @dp.message(F.photo)
    async def on_photo(message: TgMessage, bot: Bot) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user and message.photo
        photo: PhotoSize = message.photo[-1]
        # сохраняем в workspace
        wd = ctx.workdir_for(message.from_user.id)
        target = _safe_target(wd, f"photo_{photo.file_unique_id}.jpg")
        await bot.download(photo, destination=target)
        # читаем для отправки модели
        data = target.read_bytes()
        await _vision_reply(
            ctx,
            message,
            images=[ImageData(data=data, mime="image/jpeg")],
            user_text=(message.caption or "").strip() or "Что на этом изображении? Опиши подробно.",
        )

    @dp.message(F.document)
    async def on_document(message: TgMessage, bot: Bot) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user
        doc: Document = message.document  # type: ignore[assignment]
        if doc.file_size and doc.file_size > MAX_FILE_SIZE:
            await message.answer(f"Файл слишком большой (>{MAX_FILE_SIZE // 1024 // 1024} МБ).")
            return
        wd = ctx.workdir_for(message.from_user.id)
        filename = doc.file_name or f"file_{doc.file_id}"
        target = _safe_target(wd, filename)
        await bot.download(doc, destination=target)

        ext = Path(filename).suffix.lower()
        mime = (doc.mime_type or "").lower()

        # 1. Картинка-документ → отправляем модели как vision
        if mime in IMAGE_MIMES or ext in IMAGE_EXTS:
            data = target.read_bytes()
            mime_use = mime if mime in IMAGE_MIMES else _ext_to_mime(ext)
            await _vision_reply(
                ctx,
                message,
                images=[ImageData(data=data, mime=mime_use)],
                user_text=(message.caption or "").strip()
                or f"Что на изображении ({filename})? Опиши подробно.",
            )
            return

        # 2. Структурированные документы (PDF/DOCX/XLSX/RTF) → извлекаем текст
        if ext in SUPPORTED_DOC_EXTS:
            extracted = extract_doc_by_ext(target, ext)
            if extracted is not None and extracted.text:
                content = extracted.text
                truncated = False
                if len(content) > MAX_EXTRACTED_DOC:
                    content = content[:MAX_EXTRACTED_DOC]
                    truncated = True
                user_q = (
                    (message.caption or "").strip()
                    or "Прочитай документ и кратко опиши что в нём."
                )
                meta_bits = [extracted.format]
                if extracted.pages_or_sheets:
                    unit = "стр" if extracted.format == "pdf" else "блоков"
                    meta_bits.append(f"{extracted.pages_or_sheets} {unit}")
                meta_bits.append(f"{target.stat().st_size // 1024} КБ")
                if truncated:
                    meta_bits.append("обрезано")
                meta = ", ".join(meta_bits)
                prompt = (
                    f"{user_q}\n\n"
                    f'<file name="{filename}" meta="{meta}">\n{content}\n</file>'
                )
                await _vision_reply(ctx, message, images=[], user_text=prompt)
                return
            # Не получилось извлечь текст. Для PDF без текста (сканов)
            # — fallback в OCR через vision-модель.
            if ext == ".pdf":
                try:
                    pages, total_pages = render_pdf_to_jpegs(
                        target, max_pages=MAX_OCR_PAGES
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("PDF render for OCR failed")
                    pages, total_pages = [], 0
                if pages:
                    note = await message.answer(
                        f"📄 PDF без текста — распознаю {len(pages)} "
                        f"страниц(ы) через vision…"
                    )
                    user_q = (message.caption or "").strip() or (
                        "Это PDF из сканов. Извлеки весь текст со страниц "
                        "и кратко перескажи содержание. Сохраняй структуру."
                    )
                    truncated_pages = total_pages > len(pages)
                    if truncated_pages:
                        user_q += (
                            f"\n\n(Прислано первые {len(pages)} из {total_pages} "
                            "страниц — остальные пропущены.)"
                        )
                    images = [
                        ImageData(data=b, mime="image/jpeg") for b in pages
                    ]
                    with suppress(Exception):
                        await note.delete()
                    await _vision_reply(
                        ctx, message, images=images, user_text=user_q
                    )
                    return
            # Не PDF или рендер не получился — понятная ошибка.
            err = (extracted.error if extracted else "формат не поддержан") or "пусто"
            await message.answer(
                f"⚠ Не смог прочитать <code>{html_escape(filename)}</code>: {html_escape(err)}",
                parse_mode="HTML",
            )
            return

        # 3. Текстовый файл → прикладываем содержимое к промпту
        if (
            ext in TEXT_EXTS
            or mime.startswith("text/")
            or mime in ("application/json", "application/xml")
        ):
            try:
                raw = target.read_bytes()
                if len(raw) > MAX_TEXT_INLINE:
                    raw = raw[:MAX_TEXT_INLINE]
                text = raw.decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                logger.exception("text file read failed")
                await message.answer(
                    f"⚠ Сохранил, но прочитать как текст не получилось: {html_escape(str(exc))}",
                    parse_mode="HTML",
                )
                return
            user_q = (message.caption or "").strip() or "Прочитай файл и кратко опиши что в нём."
            prompt = (
                f"{user_q}\n\n"
                f'<file name="{filename}">\n{text}\n</file>'
            )
            await _vision_reply(ctx, message, images=[], user_text=prompt)
            return

        # 4. Бинарный / неизвестный тип → только сохраняем
        rel = target.relative_to(wd)
        await message.answer(
            f"Сохранил <code>{html_escape(str(rel))}</code> "
            f"({target.stat().st_size // 1024} КБ).\n"
            "Не умею читать этот формат напрямую. Поддержаны: PDF, DOCX, XLSX, "
            "RTF, текстовые файлы (.txt/.md/.py/.json/...), картинки. В "
            "agent-режиме можно попросить: «прочитай файл ...».",
            parse_mode="HTML",
        )


async def _vision_reply(
    ctx: AppContext,
    message: TgMessage,
    *,
    images: list[ImageData],
    user_text: str,
) -> None:
    """Запускает агента с прикреплёнными изображениями / расширенным текстом."""
    assert message.from_user
    provider_data = await ctx.get_provider_for(message.from_user.id)
    if provider_data is None:
        await message.answer(
            "Файл получен и сохранён, но я не могу его обработать — у меня "
            "нет настроенного LLM-провайдера/ключа.\n\n"
            "Админу: <code>/provider openai</code> → "
            "<code>/setkey openai sk-...</code> → <code>/model gpt-5</code>.",
            parse_mode="HTML",
        )
        return
    provider, model = provider_data
    user_settings = await ctx.db.ensure_user(message.from_user.id)
    history = await ctx.load_history(message.from_user.id)

    if user_settings.mode == "agent":
        workdir = ctx.workdir_for(message.from_user.id)
        sandbox = build_sandbox(workdir, max_output=ctx.settings.sandbox_max_output)
        tools = build_tool_registry(
            sandbox=sandbox,
            sandbox_timeout=ctx.settings.sandbox_timeout,
            google_search_api_key=ctx.settings.google_search_api_key,
            google_search_cse_id=ctx.settings.google_search_cse_id,
        )
    else:
        tools = None

    agent = Agent(
        provider=provider,
        model=model,
        tools=tools,
        max_iterations=ctx.settings.max_agent_iterations,
    )

    progress = await message.answer("👁 Анализирую…" if images else "📄 Читаю…")
    last_edit = 0.0
    log_lines: list[str] = []

    async def on_event(ev: AgentEvent) -> None:
        nonlocal last_edit
        if ev.kind == "thinking":
            line = f"💭 {ev.text}"
        elif ev.kind == "tool_call":
            line = f"🛠 {ev.text}"
        elif ev.kind == "tool_result":
            line = f"   → {ev.text[:200]}"
        elif ev.kind == "error":
            line = f"⚠ {ev.text}"
        elif ev.kind == "final":
            return
        else:
            line = ev.text
        log_lines.append(line)
        now = time.monotonic()
        if now - last_edit < 1.2:
            return
        last_edit = now
        with suppress(Exception):
            await progress.edit_text("\n".join(log_lines[-12:])[-3500:])

    base_prompt = SYSTEM_PROMPT_AGENT if tools else SYSTEM_PROMPT_CHAT
    sys_prompt = apply_persona(base_prompt, user_settings.persona)

    try:
        result = await agent.run(
            history,
            user_text,
            on_event=on_event,
            images=images,
            system_prompt=sys_prompt,
        )
    except ProviderError as exc:
        await progress.edit_text(f"⚠ Ошибка провайдера: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("vision agent failed")
        await progress.edit_text(f"⚠ Ошибка: {exc}")
        return

    with suppress(Exception):
        await progress.delete()

    await ctx.save_messages(message.from_user.id, result.new_messages)
    await send_llm_response(message, result.final_text or "(пустой ответ)")


def _ext_to_mime(ext: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")


def _safe_target(workdir: Path, filename: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in filename).strip() or "file"
    target = workdir / safe
    base, _, ext = safe.rpartition(".")
    counter = 1
    while target.exists():
        if base:
            target = workdir / f"{base}_{counter}.{ext}"
        else:
            target = workdir / f"{safe}_{counter}"
        counter += 1
    return target


_ = io  # silence unused-import lint when not actively used
