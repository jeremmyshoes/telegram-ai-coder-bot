"""Озвучка текста и клонирование голоса.

Команды:
- ``/voice <текст>`` — синтез через OpenAI TTS (`gpt-4o-mini-tts`,
  6 встроенных голосов). Без клонирования. Бот возвращает голосовое
  сообщение (opus). Нужен OpenAI-ключ (берётся из настроек юзера или
  админа, как в `/yt`/`/search`).
- ``/clone`` — сохранить свой голосовой образец. Можно отправить
  голосовое/аудио с подписью ``/clone`` либо в reply на голосовое
  написать ``/clone``. Опционально вторым аргументом — расшифровка
  образца (``/clone привет это мой голос``); если не указать,
  расшифровку сделает Whisper при синтезе.
- ``/cvoice <текст>`` — синтез цитированной речи в **вашем** голосе
  через F5-TTS на Replicate (https://github.com/SWivid/F5-TTS).
  Нужен `REPLICATE_API_TOKEN` в .env. Возвращает аудиофайл (.wav).
- ``/voicedel`` — удалить сохранённый образец.

Замечания:
- Образцы хранятся в `data/workspaces/<uid>/voice_sample.<ext>`
  и сайдкаре `voice_sample.txt` (расшифровка). У каждого юзера —
  свой независимый образец.
- F5-TTS на Replicate: модель `lucataco/f5-tts`. Если у `ref_text`
  пусто, модель сама прогонит образец через Whisper (опция
  ``whisper_model_id``, дефолт мелкая).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from html import escape as html_escape
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile
from aiogram.types import Message as TgMessage
from openai import AsyncOpenAI

from bot.handlers.common import AppContext, is_allowed

logger = logging.getLogger(__name__)


# ---- OpenAI TTS ----------------------------------------------------------

OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICES = (
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "onyx", "nova", "sage", "shimmer", "verse",
)
DEFAULT_OPENAI_VOICE = "alloy"
MAX_TTS_TEXT = 4000  # OpenAI: ~4096 симв на запрос


# ---- Replicate F5-TTS ----------------------------------------------------

# Каноничная связка SWivid/F5-TTS (8.7k★ на GitHub) на Replicate.
# Модель принимает `ref_audio`, опциональный `ref_text` и `gen_text`.
# Если `ref_text` не указан — внутри модели отрабатывает Whisper
# и достаёт расшифровку сама.
# Используем эндпоинт `models/{owner}/{name}/predictions`, который сам
# подставляет официальную версию — не нужно зашивать version-hash.
F5_TTS_MODEL_OWNER = "lucataco"
F5_TTS_MODEL_NAME = "f5-tts"

REPLICATE_BASE = "https://api.replicate.com/v1"
# F5-TTS не любит образцы длиннее 15 секунд (по доке репозитория) —
# берём с запасом и не пускаем слишком короткие/длинные.
SAMPLE_MIN_SEC = 3
SAMPLE_MAX_SEC = 30


# ---- helpers -------------------------------------------------------------


def _voice_sample_dir(ctx: AppContext, user_id: int) -> Path:
    d = ctx.workdir_for(user_id) / "_voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _existing_sample(ctx: AppContext, user_id: int) -> Path | None:
    d = _voice_sample_dir(ctx, user_id)
    for ext in ("ogg", "oga", "mp3", "m4a", "wav"):
        p = d / f"sample.{ext}"
        if p.exists():
            return p
    return None


def _sample_text_path(ctx: AppContext, user_id: int) -> Path:
    return _voice_sample_dir(ctx, user_id) / "sample.txt"


async def _openai_tts(api_key: str, base_url: str | None, text: str, voice: str) -> bytes:
    """Возвращает opus-bytes."""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    # OpenAI SDK 1.54: `client.audio.speech.with_streaming_response.create`.
    # Используем простой `.create()` — он возвращает HttpxBinaryResponseContent.
    resp = await client.audio.speech.create(
        model=OPENAI_TTS_MODEL,
        voice=voice,  # type: ignore[arg-type]  # SDK 1.54 narrows к старым 6 голосам
        input=text,
        response_format="opus",
    )
    return await resp.aread()


async def _replicate_predict(
    token: str,
    *,
    ref_audio_path: Path,
    ref_text: str,
    gen_text: str,
) -> bytes:
    """Запускает F5-TTS на Replicate и тянет результат как bytes (wav)."""
    # Заливаем файл в Replicate Files API; он отдаёт публичный URL,
    # пригодный для использования в `input.ref_audio`.
    ref_audio_url = await _replicate_upload_file(token, ref_audio_path)

    payload = {
        "input": {
            "gen_text": gen_text,
            "ref_audio": ref_audio_url,
            "ref_text": ref_text or "",
            "remove_silence": True,
        },
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "wait",  # синхронный режим до 60с
    }
    url = (
        f"{REPLICATE_BASE}/models/{F5_TTS_MODEL_OWNER}/"
        f"{F5_TTS_MODEL_NAME}/predictions"
    )
    async with httpx.AsyncClient(timeout=180.0) as cli:
        r = await cli.post(url, headers=headers, json=payload)
        r.raise_for_status()
        pred = r.json()
        # Если синхронный wait не успел, опрашиваем сами.
        for _ in range(120):
            status = pred.get("status")
            if status in ("succeeded", "failed", "canceled"):
                break
            await asyncio.sleep(2.0)
            r = await cli.get(pred["urls"]["get"], headers=headers)
            r.raise_for_status()
            pred = r.json()
        if pred.get("status") != "succeeded":
            err = pred.get("error") or pred.get("status") or "unknown"
            raise RuntimeError(f"F5-TTS failed: {err}")
        out = pred.get("output")
        # output может быть строкой-URL или списком
        url = out if isinstance(out, str) else (out[0] if out else None)
        if not url:
            raise RuntimeError("F5-TTS: пустой output")
        rr = await cli.get(url)
        rr.raise_for_status()
        return rr.content


async def _replicate_upload_file(token: str, path: Path) -> str:
    """Загружает локальный файл в Replicate Files API и возвращает urls.get."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60.0) as cli:
        with path.open("rb") as fh:
            files = {"content": (path.name, fh, "application/octet-stream")}
            r = await cli.post(f"{REPLICATE_BASE}/files", headers=headers, files=files)
        r.raise_for_status()
        body = r.json()
        url = body.get("urls", {}).get("get") or body.get("url")
        if not url:
            raise RuntimeError(f"Replicate Files: нет URL в ответе: {body}")
        return url


# ---- handlers ------------------------------------------------------------


def register_voice_handlers(dp: Dispatcher, ctx: AppContext) -> None:

    # ============================================================ /voice

    @dp.message(Command("voice"))
    async def cmd_voice(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user

        raw = (command.args or "").strip()
        # Поддержим `/voice -v nova текст…` чтобы выбрать голос на лету.
        voice = DEFAULT_OPENAI_VOICE
        if raw.startswith("-v "):
            tokens = raw.split(maxsplit=2)
            if len(tokens) >= 2 and tokens[1].lower() in OPENAI_TTS_VOICES:
                voice = tokens[1].lower()
                raw = tokens[2] if len(tokens) > 2 else ""

        if not raw:
            await message.answer(
                "Использование:\n"
                "<code>/voice текст…</code> — озвучить текст\n"
                "<code>/voice -v nova текст…</code> — выбрать голос\n\n"
                f"Голоса: <code>{', '.join(OPENAI_TTS_VOICES)}</code>",
                parse_mode="HTML",
            )
            return

        if len(raw) > MAX_TTS_TEXT:
            await message.answer(
                f"⚠ Текст длиннее {MAX_TTS_TEXT} симв — обрежу."
            )
            raw = raw[:MAX_TTS_TEXT]

        key_pair = await ctx.find_openai_key(message.from_user.id)
        if key_pair is None:
            await message.answer(
                "Для <code>/voice</code> нужен OpenAI-ключ. "
                "Админу: <code>/setkey openai sk-...</code>",
                parse_mode="HTML",
            )
            return
        api_key, base_url = key_pair

        progress = await message.answer("🎙 Синтезирую…")
        try:
            audio = await _openai_tts(api_key, base_url, raw, voice)
        except Exception as exc:  # noqa: BLE001
            logger.exception("openai tts failed")
            with suppress(Exception):
                await progress.delete()
            await message.answer(f"⚠ Ошибка TTS: {html_escape(str(exc)[:300])}")
            return

        with suppress(Exception):
            await progress.delete()
        await message.answer_voice(
            BufferedInputFile(audio, filename="voice.ogg"),
            caption=f"🗣 {voice}",
        )

    # ============================================================ /clone

    @dp.message(Command("clone"))
    async def cmd_clone(
        message: TgMessage, command: CommandObject, bot: Bot
    ) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user
        uid = message.from_user.id

        ref_text = (command.args or "").strip()

        # Источник голоса: само сообщение или reply.
        source = message
        media = message.voice or message.audio
        if media is None and message.reply_to_message:
            media = message.reply_to_message.voice or message.reply_to_message.audio
            if media is not None:
                source = message.reply_to_message

        if media is None:
            await message.answer(
                "Прикрепите голосовое или аудио к команде "
                "(или ответьте <code>/clone</code> на голосовое сообщение).\n\n"
                "Образец нужен длительностью "
                f"{SAMPLE_MIN_SEC}–{SAMPLE_MAX_SEC} сек, чем чище — тем "
                "лучше клон. По желанию — расшифровка после команды:\n"
                "<code>/clone привет, это мой голос</code>",
                parse_mode="HTML",
            )
            return

        # Длительность
        duration = getattr(media, "duration", None) or 0
        if duration and (duration < SAMPLE_MIN_SEC or duration > SAMPLE_MAX_SEC):
            await message.answer(
                f"⚠ Образец {duration} сек — нужно "
                f"{SAMPLE_MIN_SEC}–{SAMPLE_MAX_SEC}."
            )
            return

        # Качаем файл во временное место в _voice
        d = _voice_sample_dir(ctx, uid)
        # Очистка старого
        for old in d.glob("sample.*"):
            with suppress(Exception):
                old.unlink()

        # Расширение по mime/типу
        if message.voice or (message.reply_to_message and message.reply_to_message.voice):
            ext = "ogg"
        else:
            mime = (getattr(media, "mime_type", None) or "").lower()
            ext = {
                "audio/mpeg": "mp3", "audio/mp3": "mp3",
                "audio/mp4": "m4a", "audio/x-m4a": "m4a",
                "audio/wav": "wav", "audio/x-wav": "wav",
                "audio/ogg": "ogg", "audio/opus": "ogg",
            }.get(mime, "ogg")

        target = d / f"sample.{ext}"
        try:
            await bot.download(media, destination=target)
        except Exception as exc:  # noqa: BLE001
            logger.exception("download voice sample failed")
            await message.answer(f"⚠ Не смог скачать: {html_escape(str(exc)[:200])}")
            return

        # Расшифровка
        text_path = _sample_text_path(ctx, uid)
        if ref_text:
            text_path.write_text(ref_text, encoding="utf-8")
        else:
            with suppress(Exception):
                text_path.unlink()

        size_kb = target.stat().st_size // 1024
        await message.answer(
            "✅ Голосовой образец сохранён "
            f"({duration} сек, {size_kb} КБ"
            f"{', с расшифровкой' if ref_text else ', расшифровку сделает Whisper'}).\n\n"
            "Используй <code>/cvoice текст</code> чтобы озвучить.",
            parse_mode="HTML",
        )
        _ = source  # подавление unused

    # ============================================================ /cvoice

    @dp.message(Command("cvoice"))
    async def cmd_cvoice(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user
        uid = message.from_user.id

        gen_text = (command.args or "").strip()
        if not gen_text:
            await message.answer(
                "Использование: <code>/cvoice текст для озвучки</code>",
                parse_mode="HTML",
            )
            return
        if len(gen_text) > MAX_TTS_TEXT:
            gen_text = gen_text[:MAX_TTS_TEXT]

        sample = _existing_sample(ctx, uid)
        if sample is None:
            await message.answer(
                "Сначала сохраните голосовой образец командой "
                "<code>/clone</code> (см. <code>/clone</code> без аргументов).",
                parse_mode="HTML",
            )
            return

        token = ctx.settings.replicate_api_token
        if not token:
            await message.answer(
                "Для <code>/cvoice</code> нужен <code>REPLICATE_API_TOKEN</code> "
                "в .env. Берётся на https://replicate.com/account/api-tokens",
                parse_mode="HTML",
            )
            return

        ref_text_path = _sample_text_path(ctx, uid)
        ref_text = ref_text_path.read_text(encoding="utf-8") if ref_text_path.exists() else ""

        progress = await message.answer("🎙 Клонирую голос (F5-TTS)…")
        try:
            wav = await _replicate_predict(
                token,
                ref_audio_path=sample,
                ref_text=ref_text,
                gen_text=gen_text,
            )
        except httpx.HTTPStatusError as exc:
            logger.exception("replicate http error")
            with suppress(Exception):
                await progress.delete()
            body = exc.response.text[:300] if exc.response is not None else ""
            await message.answer(
                f"⚠ Replicate {exc.response.status_code}: {html_escape(body)}"
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("f5-tts failed")
            with suppress(Exception):
                await progress.delete()
            await message.answer(f"⚠ Ошибка клонирования: {html_escape(str(exc)[:300])}")
            return

        with suppress(Exception):
            await progress.delete()
        await message.answer_audio(
            BufferedInputFile(wav, filename="cvoice.wav"),
            caption="🗣 ваш клон (F5-TTS)",
        )

    # ============================================================ /voicedel

    @dp.message(Command("voicedel"))
    async def cmd_voicedel(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user
        uid = message.from_user.id
        d = _voice_sample_dir(ctx, uid)
        removed = 0
        for p in d.glob("sample.*"):
            with suppress(Exception):
                p.unlink()
                removed += 1
        await message.answer(
            f"🗑 Удалено: {removed}." if removed else "У вас не было сохранённого образца."
        )
