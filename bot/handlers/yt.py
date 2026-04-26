"""Команда `/yt` — транскрипция и пересказ YouTube-видео.

Под капотом:
1. `yt-dlp` качает аудиодорожку (без ffmpeg-постпроцесса) — берём
   формат с самым низким битрейтом, чтобы уложиться в 25МБ-лимит
   OpenAI `/audio/transcriptions`.
2. OpenAI `gpt-4o-transcribe` делает текст из аудио.
3. OpenAI же (модель из `SEARCH_SYNTH_MODEL`, дефолт `gpt-5`) пишет
   краткий пересказ. Флаг `-full` отдаёт сырой транскрипт без
   синтеза.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from contextlib import suppress
from html import escape as html_escape
from pathlib import Path
from typing import Any

from aiogram import Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message as TgMessage
from openai import AsyncOpenAI

from bot.handlers.common import AppContext, is_allowed, send_llm_response, send_long
from bot.providers.base import Message, ProviderError
from bot.providers.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

# 25МБ — лимит OpenAI на /audio/transcriptions; берём 24 для запаса.
MAX_AUDIO_BYTES = 24 * 1024 * 1024
TRANSCRIBE_MODEL = "gpt-4o-transcribe"
# Жёстко режем транскрипт перед отправкой в синт-модель: для длинных
# подкастов это ~80K символов ≈ 20K токенов.
MAX_TRANSCRIPT_FOR_SYNTH = 80_000


def _yt_download_audio(url: str, dest_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Качает аудио в `dest_dir` и возвращает (путь, info-dict).

    `format` подобран так, чтобы yt-dlp выбирал самую лёгкую аудиодорожку
    среди доступных (m4a/webm) — без ffmpeg-перекодирования. Если ничего
    не лезет в ~24МБ, всё равно скачаем bestaudio (потом отдадим ошибку
    «слишком большое» по факту).
    """
    import yt_dlp  # type: ignore[import-untyped]

    outtmpl = str(dest_dir / "%(id)s.%(ext)s")
    ydl_opts: dict[str, Any] = {
        "format": (
            # 1. идеально — m4a/webm небольшого размера без перекодирования
            "ba[ext=m4a][filesize<24M]/ba[ext=webm][filesize<24M]/"
            # 2. приемлемо — низкий битрейт
            "ba[abr<=64]/ba[abr<=80]/"
            # 3. в крайнем случае — bestaudio (может не влезть в 24МБ)
            "bestaudio/best"
        ),
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # для одного видео:
        path_str = ydl.prepare_filename(info)
    return Path(path_str), info or {}


def register_yt_handlers(dp: Dispatcher, ctx: AppContext) -> None:
    @dp.message(Command("yt"))
    async def cmd_yt(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(
            ctx.settings, message.from_user.id if message.from_user else None
        ):
            return
        assert message.from_user

        raw = (command.args or "").strip()
        full_mode = False
        if raw.startswith("-full "):
            full_mode = True
            raw = raw[len("-full "):].strip()
        elif raw == "-full":
            raw = ""

        if not raw:
            await message.answer(
                "Использование:\n"
                "<code>/yt URL</code> — краткий пересказ видео\n"
                "<code>/yt -full URL</code> — полный транскрипт без пересказа\n\n"
                "Аудио до ~30 минут (≤24 МБ). Длиннее не примет API.",
                parse_mode="HTML",
            )
            return

        url = raw.split()[0]
        if not url.startswith(("http://", "https://")):
            await message.answer("URL должен начинаться с http(s)://")
            return

        # Нужен openai-ключ: и для транскрипции, и для пересказа.
        key_pair = await ctx.find_openai_key(message.from_user.id)
        if key_pair is None:
            await message.answer(
                "Для <code>/yt</code> нужен OpenAI-ключ (модели "
                f"<code>{TRANSCRIBE_MODEL}</code> + "
                f"<code>{html_escape(ctx.settings.search_synth_model)}</code>).\n"
                "Админу: <code>/setkey openai sk-...</code>",
                parse_mode="HTML",
            )
            return
        api_key, base_url = key_pair

        progress = await message.answer("⬇️ Скачиваю аудио…")

        # 1. Скачиваем во временную папку — после транскрипции удалится сама.
        with tempfile.TemporaryDirectory(prefix="yt_") as tmp:
            tmp_dir = Path(tmp)
            try:
                audio_path, info = await asyncio.to_thread(
                    _yt_download_audio, url, tmp_dir
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("yt-dlp failed for %s", url)
                with suppress(Exception):
                    await progress.delete()
                await message.answer(
                    f"⚠ Не смог скачать видео: {html_escape(str(exc)[:300])}",
                    parse_mode="HTML",
                )
                return

            if not audio_path.exists():
                with suppress(Exception):
                    await progress.delete()
                await message.answer(
                    "⚠ yt-dlp не оставил аудиофайла. "
                    "Возможно у видео нет аудиодорожки или оно приватное."
                )
                return

            size = audio_path.stat().st_size
            if size > MAX_AUDIO_BYTES:
                with suppress(Exception):
                    await progress.delete()
                await message.answer(
                    f"⚠ Аудио {size // 1024 // 1024} МБ — больше лимита "
                    "24 МБ OpenAI /audio/transcriptions. Длинные видео пока "
                    "не поддержаны (нужен ffmpeg для нарезки на куски). "
                    "Попробуйте видео покороче или менее качественную дорожку."
                )
                return

            title = info.get("title") or "видео"
            duration_sec = int(info.get("duration") or 0)
            mins, secs = divmod(duration_sec, 60)

            with suppress(Exception):
                await progress.edit_text(
                    f"🎙 Транскрибирую ({mins:d}:{secs:02d}, "
                    f"{size // 1024} КБ, {TRANSCRIBE_MODEL})…"
                )

            # 2. Транскрибируем.
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            try:
                with audio_path.open("rb") as f:
                    tr = await client.audio.transcriptions.create(
                        file=f,
                        model=TRANSCRIBE_MODEL,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("OpenAI transcribe failed")
                with suppress(Exception):
                    await progress.delete()
                await message.answer(
                    "⚠ Транскрипция не удалась: "
                    f"{html_escape(str(exc)[:300])}",
                    parse_mode="HTML",
                )
                return

        transcript = (getattr(tr, "text", "") or "").strip()

        if not transcript:
            with suppress(Exception):
                await progress.delete()
            await message.answer("⚠ Whisper вернул пустой транскрипт.")
            return

        # 3a. -full → отдаём сырой транскрипт.
        if full_mode:
            with suppress(Exception):
                await progress.delete()
            header = f"📝 Транскрипт «{title}»:\n\n"
            await send_long(message, header + transcript)
            return

        # 3b. Дефолт — пересказ через synth-модель.
        with suppress(Exception):
            await progress.edit_text("🧠 Делаю пересказ…")

        synth = OpenAICompatProvider(
            name="openai", api_key=api_key, base_url=base_url
        )
        synth_model = ctx.settings.search_synth_model
        system_prompt = (
            "Ты пересказываешь содержание видео по его автоматически "
            "сгенерированному транскрипту.\n\n"
            "Правила:\n"
            "1. Отвечай на том же языке, что и транскрипт.\n"
            "2. Структура: 1-2 коротких абзаца сути + список ключевых "
            "тезисов (буллеты). Если уместно — раздел «Цифры/факты» "
            "или «Выводы».\n"
            "3. Без воды и без вступлений вроде «в этом видео автор "
            "рассказывает о…». Сразу по делу.\n"
            "4. Markdown допустим: жирный, списки, заголовки H3.\n"
            "5. Если транскрипт обрезан — отметь это в конце пересказа."
        )
        was_truncated = len(transcript) > MAX_TRANSCRIPT_FOR_SYNTH
        body = transcript[:MAX_TRANSCRIPT_FOR_SYNTH]
        if was_truncated:
            body += "\n\n[…транскрипт обрезан, дальше не показано]"
        user_prompt = f"Видео: «{title}»\n\nТранскрипт:\n{body}"

        try:
            resp = await synth.complete(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                model=synth_model,
            )
        except ProviderError as exc:
            with suppress(Exception):
                await progress.delete()
            await message.answer(
                f"⚠ Ошибка модели <code>{html_escape(synth_model)}</code>: "
                f"{html_escape(str(exc))}\n\n"
                "Транскрипт получился — попробуйте "
                "<code>/yt -full URL</code>.",
                parse_mode="HTML",
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("yt synth failed")
            with suppress(Exception):
                await progress.delete()
            await message.answer(
                f"⚠ Ошибка пересказа: {html_escape(str(exc)[:300])}",
                parse_mode="HTML",
            )
            return

        with suppress(Exception):
            await progress.delete()

        summary = (resp.content or "").strip()
        if not summary:
            await message.answer("Модель вернула пустой пересказ.")
            return

        head = f"📺 <b>{html_escape(title)}</b>\n\n"
        await send_llm_response(message, head + summary)
        await message.answer(
            "💬 Полный транскрипт: <code>/yt -full URL</code>",
            parse_mode="HTML",
        )
