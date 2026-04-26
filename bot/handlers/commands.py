"""Команды бота: /start, /help, /provider, /setkey, /model, /mode, /reset, /workdir."""

from __future__ import annotations

import html
import io
import logging
import math
import shutil
import time
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ReplyKeyboardRemove,
)
from aiogram.types import Message as TgMessage

from bot.handlers.common import (
    AppContext,
    is_admin,
    is_allowed,
    provider_titles,
    send_long,
)
from bot.handlers.keyboards import (
    BTN_CHAT,
    BTN_HELP,
    BTN_IMAGE,
    BTN_SETTINGS,
    BTN_STATUS,
    admin_kb,
    main_menu_inline_kb,
    mode_kb,
    models_kb,
    providers_kb,
    settings_kb,
)
from bot.providers import PROVIDER_PRESETS, ImageData, Message, ProviderError
from bot.providers.openai_compat import OpenAICompatProvider
from bot.tools.web_search import (
    WebSearchError,
    web_search,
)
from bot.tools.web_search import (
    format_results as format_search_results,
)

logger = logging.getLogger(__name__)


USER_HELP_TEXT = """\
<b>🤖 Как пользоваться ботом</b>

Просто пишите сообщения в чат — бот ответит. Или вызовите меню командой /menu.

<b>Основные команды</b>
/chat &lt;вопрос&gt; — один вопрос модели (без истории)
/img &lt;промпт&gt; — сгенерировать картинку
/search &lt;запрос&gt; — веб-поиск (DuckDuckGo)
/status — текущие настройки
/reset — очистить историю
/menu — открыть меню
/help — эта справка

<b>🎨 Картинки (флаги /img)</b>
• размер: <code>/img -s 1792x1024 закат над морем</code>
• качество: <code>/img -q high кот в очках</code>
• модель: <code>/img -m gpt-image-2 космонавт</code>

<b>📷 Фото и файлы</b>
• Пришлите фото (с подписью или без) — модель «увидит» и ответит.
• Картинку как файл (jpg/png/webp/gif) — то же самое.
• Текстовый файл (.py/.md/.txt/.json/.log/…) — содержимое уйдёт в промпт.
• Ответьте на фото командой <code>/chat ваш вопрос</code> — модель получит
  и фото, и текст.
"""

ADMIN_HELP_TEXT = """\
<b>🛠 Админские команды</b>

/providers — список встроенных провайдеров
/provider &lt;id&gt; — выбрать провайдера
/setkey &lt;provider&gt; &lt;api_key&gt; [base_url] — сохранить API-ключ (шифруется)
/keys — показать какие ключи сохранены
/delkey &lt;provider&gt; — удалить ключ
/model &lt;model_id&gt; — задать модель
/models — список моделей провайдера
/mode agent|chat — режим работы
/workdir — файлы рабочей папки
/clearwd — очистить рабочую папку

Быстрый вход: кнопка «🛠 Админ» в главном меню — открывает все
эти действия кликами без набора в ручную.
"""

# Старое имя — для совместимости с кодом, который мог импортировать HELP_TEXT.
HELP_TEXT = USER_HELP_TEXT


# Размеры, которые принимает OpenAI Images API
_DALLE3_SIZES = {"1024x1024", "1024x1792", "1792x1024"}
_DALLE2_SIZES = {"256x256", "512x512", "1024x1024"}
# gpt-image-* развивается, поэтому строгий whitelist убрали — валидация только
# для dall-e-*. gpt-image-* размеры пробрасываем как есть (OpenAI вернёт ошибку,
# если что-то не так).


def _aspect_ratio(size: str) -> str:
    """\"1024x1536\" -> \"2:3\"; \"auto\" / неразбираемое -> \"\"."""
    try:
        w_s, h_s = size.lower().split("x")
        w, h = int(w_s), int(h_s)
    except (ValueError, AttributeError):
        return ""
    if w <= 0 or h <= 0:
        return ""
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}"


def _parse_img_args(args: str) -> tuple[str, dict[str, str]]:
    """Парсер флагов: -s WxH, -q quality, -m model, -p provider; остальное — промпт."""
    flags: dict[str, str] = {}
    tokens = args.split()
    prompt_parts: list[str] = []
    short = {"-s": "size", "--size": "size", "-q": "quality", "--quality": "quality",
             "-m": "model", "--model": "model", "-p": "provider", "--provider": "provider"}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in short and i + 1 < len(tokens):
            flags[short[t]] = tokens[i + 1]
            i += 2
            continue
        prompt_parts.append(t)
        i += 1
    return " ".join(prompt_parts).strip(), flags


def register_command_handlers(dp: Dispatcher, ctx: AppContext) -> None:
    def _user_id(
        m: TgMessage | CallbackQuery,
    ) -> int | None:
        return m.from_user.id if m.from_user else None

    async def _ensure_admin_msg(message: TgMessage) -> bool:
        """True если юзер админ. Иначе отправляет отказ."""
        if not is_admin(ctx.settings, _user_id(message)):
            await message.answer("🔒 Эта команда доступна только администратору.")
            return False
        return True

    async def _ensure_admin_cb(query: CallbackQuery) -> bool:
        if not is_admin(ctx.settings, _user_id(query)):
            await query.answer("Только для админа", show_alert=True)
            return False
        return True

    def _help_for(user_id: int | None) -> str:
        if is_admin(ctx.settings, user_id):
            return USER_HELP_TEXT + "\n" + ADMIN_HELP_TEXT
        return USER_HELP_TEXT

    @dp.message(Command("start"))
    async def cmd_start(message: TgMessage) -> None:
        uid = _user_id(message)
        if not is_allowed(ctx.settings, uid):
            await message.answer("Доступ запрещён. Свяжитесь с администратором бота.")
            return
        await message.answer(
            "👋 Привет! Выберите действие кнопкой ниже или просто пишите сообщение — бот ответит.",
            reply_markup=main_menu_inline_kb(is_admin=is_admin(ctx.settings, uid)),
        )

    @dp.message(Command("help"))
    async def cmd_help(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        await send_long(message, _help_for(_user_id(message)), parse_mode="HTML")

    @dp.message(Command("menu"))
    async def cmd_menu(message: TgMessage) -> None:
        uid = _user_id(message)
        if not is_allowed(ctx.settings, uid):
            return
        await message.answer(
            "Главное меню:",
            reply_markup=main_menu_inline_kb(is_admin=is_admin(ctx.settings, uid)),
        )

    @dp.message(Command("providers"))
    async def cmd_providers(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        await message.answer(
            "<b>Выберите провайдера</b>:\n" + provider_titles(),
            parse_mode="HTML",
            reply_markup=providers_kb(),
        )

    @dp.message(Command("provider"))
    async def cmd_provider(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        provider_id = (command.args or "").strip()
        if not provider_id:
            await message.answer(
                "Выберите провайдера:",
                reply_markup=providers_kb(),
            )
            return
        if provider_id not in PROVIDER_PRESETS:
            await message.answer(f"Неизвестный провайдер: {provider_id}")
            return
        assert message.from_user
        await ctx.db.update_user(message.from_user.id, provider=provider_id)
        preset = PROVIDER_PRESETS[provider_id]
        suggested = ", ".join(preset.suggested_models) or "(выберите модель сами)"
        await message.answer(
            f"Провайдер: <b>{preset.title}</b>\n"
            f"Подсказки моделей: {suggested}\n"
            f"Получить API-ключ: {preset.api_key_url or '—'}",
            parse_mode="HTML",
        )

    @dp.message(Command("setkey"))
    async def cmd_setkey(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        if not command.args:
            await message.answer(
                "Использование: <code>/setkey &lt;provider&gt; &lt;api_key&gt; [base_url]</code>",
                parse_mode="HTML",
            )
            return
        parts = command.args.split()
        if len(parts) < 2:
            await message.answer("Нужно как минимум provider и api_key")
            return
        provider_id, api_key = parts[0], parts[1]
        base_url = parts[2] if len(parts) > 2 else None
        if provider_id not in PROVIDER_PRESETS:
            await message.answer(f"Неизвестный провайдер: {provider_id}")
            return
        assert message.from_user
        encrypted = ctx.vault.encrypt(api_key)
        await ctx.db.upsert_key(message.from_user.id, provider_id, encrypted, base_url)
        # удаляем сообщение с ключом из чата для безопасности
        with suppress(Exception):
            await message.delete()
        await message.answer(
            f"Ключ для <b>{provider_id}</b> сохранён (зашифрован).\n"
            f"Сообщение с ключом удалено.\n"
            f"Текущий провайдер: используйте <code>/provider {provider_id}</code>.",
            parse_mode="HTML",
        )

    @dp.message(Command("keys"))
    async def cmd_keys(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        assert message.from_user
        keys = await ctx.db.list_keys(message.from_user.id)
        if not keys:
            await message.answer("Сохранённых ключей нет. Используйте /setkey.")
            return
        lines = []
        for k in keys:
            extra = f" base_url={k.base_url}" if k.base_url else ""
            lines.append(f"• {k.provider}{extra}")
        await message.answer("Сохранённые ключи:\n" + "\n".join(lines))

    @dp.message(Command("delkey"))
    async def cmd_delkey(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        provider_id = (command.args or "").strip()
        if not provider_id:
            await message.answer("Укажите провайдера: /delkey openai")
            return
        assert message.from_user
        await ctx.db.delete_key(message.from_user.id, provider_id)
        await message.answer(f"Ключ {provider_id} удалён.")

    @dp.message(Command("model"))
    async def cmd_model(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        model = (command.args or "").strip()
        assert message.from_user
        if not model:
            user = await ctx.db.ensure_user(message.from_user.id)
            if not user.provider:
                await message.answer(
                    "Сначала выберите провайдера:",
                    reply_markup=providers_kb(),
                )
                return
            await message.answer(
                f"Выберите модель для <b>{user.provider}</b>:",
                parse_mode="HTML",
                reply_markup=models_kb(user.provider),
            )
            return
        await ctx.db.update_user(message.from_user.id, model=model)
        await message.answer(f"Модель: <code>{model}</code>", parse_mode="HTML")

    @dp.message(Command("models"))
    async def cmd_models(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        assert message.from_user
        user = await ctx.db.ensure_user(message.from_user.id)
        if not user.provider:
            await message.answer("Сначала выберите провайдера: /provider <id>")
            return
        preset = PROVIDER_PRESETS.get(user.provider)
        suggested = preset.suggested_models if preset else ()
        text = "<b>Рекомендованные модели</b>:\n"
        if suggested:
            text += "\n".join(f"• <code>{m}</code>" for m in suggested)
        else:
            text += "(нет предустановленного списка — задайте через /model)"

        # Попытаемся подтянуть live список через API
        key_row = await ctx.db.get_key(message.from_user.id, user.provider)
        if key_row and user.provider != "anthropic":
            try:
                from bot.providers import create_provider

                provider = create_provider(
                    user.provider,
                    api_key=ctx.vault.decrypt(key_row.encrypted),
                    base_url=key_row.base_url,
                )
                live = await provider.list_models()
                if live:
                    text += "\n\n<b>Доступные через API</b> (первые 30):\n"
                    text += "\n".join(f"• <code>{m}</code>" for m in live[:30])
                    if len(live) > 30:
                        text += f"\n…и ещё {len(live) - 30}"
            except Exception as exc:  # noqa: BLE001
                text += f"\n\n(не удалось получить список: {exc})"
        await send_long(message, text, parse_mode="HTML")

    @dp.message(Command("mode"))
    async def cmd_mode(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        mode = (command.args or "").strip().lower()
        assert message.from_user
        if mode not in ("agent", "chat"):
            user = await ctx.db.ensure_user(message.from_user.id)
            await message.answer(
                f"Текущий режим — <b>{user.mode}</b>. Выберите:",
                parse_mode="HTML",
                reply_markup=mode_kb(user.mode),
            )
            return
        await ctx.db.update_user(message.from_user.id, mode=mode)
        await message.answer(f"Режим: <b>{mode}</b>", parse_mode="HTML")

    @dp.message(Command("status"))
    async def cmd_status(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user
        user = await ctx.db.ensure_user(message.from_user.id)
        keys = await ctx.db.list_keys(message.from_user.id)
        wd = ctx.workdir_for(message.from_user.id)
        text = (
            f"<b>Статус</b>\n"
            f"Провайдер: <code>{user.provider or '—'}</code>\n"
            f"Модель: <code>{user.model or '—'}</code>\n"
            f"Режим: <code>{user.mode}</code>\n"
            f"Ключей сохранено: {len(keys)}\n"
            f"Рабочая папка: <code>{wd}</code>"
        )
        await message.answer(text, parse_mode="HTML")

    @dp.message(Command("reset"))
    async def cmd_reset(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user
        await ctx.db.clear_history(message.from_user.id)
        await message.answer("История очищена.")

    @dp.message(Command("workdir"))
    async def cmd_workdir(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        assert message.from_user
        wd = ctx.workdir_for(message.from_user.id)
        items = []
        for entry in sorted(wd.iterdir()):
            kind = "dir" if entry.is_dir() else "file"
            size = entry.stat().st_size if entry.is_file() else "-"
            items.append(f"{entry.name}\t{kind}\t{size}")
        listing = "\n".join(items) or "(пусто)"
        await message.answer(f"<code>{wd}</code>\n<pre>{listing}</pre>", parse_mode="HTML")

    @dp.message(Command("clearwd"))
    async def cmd_clearwd(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        if not await _ensure_admin_msg(message):
            return
        assert message.from_user
        wd = ctx.workdir_for(message.from_user.id)
        for entry in wd.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                with suppress(OSError):
                    entry.unlink()
        await message.answer("Рабочая папка очищена.")

    @dp.message(Command("chat"))
    async def cmd_chat(message: TgMessage, command: CommandObject, bot: Bot) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        prompt = (command.args or "").strip()
        # если /chat ответ на фото — берём его как vision-вход
        images: list[ImageData] = []
        reply = message.reply_to_message
        if reply and reply.photo:
            try:
                buf = io.BytesIO()
                await bot.download(reply.photo[-1], destination=buf)
                images.append(ImageData(data=buf.getvalue(), mime="image/jpeg"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("/chat reply photo download failed: %s", exc)
        if not prompt:
            if images:
                prompt = "Опиши изображение."
            else:
                await message.answer(
                    "Использование: <code>/chat ваш вопрос</code>\n"
                    "Можно ответить на фото командой /chat — модель его «увидит».",
                    parse_mode="HTML",
                )
                return
        assert message.from_user
        provider_data = await ctx.get_provider_for(message.from_user.id)
        if provider_data is None:
            await message.answer(
                "Сначала настройте провайдер/модель/ключ. /help",
            )
            return
        provider, model = provider_data

        thinking = await message.answer("⏳ Думаю…")
        try:
            response = await provider.complete(
                messages=[Message(role="user", content=prompt, images=images)],
                model=model,
            )
        except ProviderError as exc:
            with suppress(Exception):
                await thinking.delete()
            await message.answer(f"⚠ Ошибка провайдера: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("/chat failed")
            with suppress(Exception):
                await thinking.delete()
            await message.answer(f"⚠ Ошибка: {exc}")
            return

        with suppress(Exception):
            await thinking.delete()

        text = (response.content or "").strip() or "(пустой ответ)"
        await send_long(message, text)

    @dp.message(Command("search"))
    async def cmd_search(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        query = (command.args or "").strip()
        if not query:
            await message.answer(
                "Использование: <code>/search ваш запрос</code>\n"
                "По умолчанию использует DuckDuckGo (без ключей). Если в "
                "<code>.env</code> заданы <code>GOOGLE_SEARCH_API_KEY</code> + "
                "<code>GOOGLE_SEARCH_CSE_ID</code> — будет использован Google.",
                parse_mode="HTML",
            )
            return
        thinking = await message.answer("🔎 Ищу…")
        try:
            results, used = await web_search(
                query,
                google_api_key=ctx.settings.google_search_api_key,
                google_cse_id=ctx.settings.google_search_cse_id,
                num_results=5,
            )
        except WebSearchError as exc:
            with suppress(Exception):
                await thinking.delete()
            await message.answer(f"⚠ Ошибка поиска: {html.escape(str(exc))}", parse_mode="HTML")
            return
        with suppress(Exception):
            await thinking.delete()
        header = f"<i>via {html.escape(used)}</i>\n\n"
        await send_long(message, header + format_search_results(results))

    @dp.message(Command("img"))
    async def cmd_img(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        if not command.args:
            await message.answer(
                "Использование: <code>/img промпт</code>\n"
                "Флаги: <code>-s WxH</code>, <code>-q hd|standard</code>, "
                "<code>-m model</code>, <code>-p provider</code>\n"
                "Пример (AceData + GPT-image): "
                "<code>/img -p acedata -m gpt-image-1 -s 1024x1024 кот</code>",
                parse_mode="HTML",
            )
            return
        prompt, flags = _parse_img_args(command.args)
        if not prompt:
            await message.answer("Промпт пуст после парсинга флагов.")
            return

        assert message.from_user
        # /img работает через любой OpenAI-совместимый провайдер (openai, acedata,
        # custom и др.). Порядок приоритета:
        #   1) явный -p provider в команде
        #   2) текущий провайдер пользователя (если не anthropic)
        #   3) openai
        explicit_provider = flags.get("provider")
        used_provider: str | None = None
        key_row = None
        if explicit_provider:
            if explicit_provider == "anthropic":
                await message.answer("Anthropic не умеет генерировать картинки. Выберите openai или acedata.")
                return
            key_row = await ctx.db.get_key(message.from_user.id, explicit_provider)
            used_provider = explicit_provider
            if key_row is None:
                await message.answer(
                    f"Ключ для провайдера <code>{html.escape(explicit_provider)}</code> не найден.\n"
                    f"Сохраните: <code>/setkey {html.escape(explicit_provider)} ваш-ключ</code>",
                    parse_mode="HTML",
                )
                return
        if key_row is None:
            user = await ctx.db.ensure_user(message.from_user.id)
            if user.provider and user.provider != "anthropic":
                k = await ctx.db.get_key(message.from_user.id, user.provider)
                if k is not None:
                    key_row, used_provider = k, user.provider
        if key_row is None:
            k = await ctx.db.get_key(message.from_user.id, "openai")
            if k is not None:
                key_row, used_provider = k, "openai"
        if key_row is None or used_provider is None:
            await message.answer(
                "Нужен OpenAI-совместимый ключ для генерации картинок:\n"
                "<code>/setkey openai sk-...</code> или "
                "<code>/setkey acedata ваш-ключ</code>.\n"
                "Можно явно: <code>/img -p acedata -m gpt-image-1 кот в очках</code>.",
                parse_mode="HTML",
            )
            return

        try:
            api_key = ctx.vault.decrypt(key_row.encrypted)
        except RuntimeError:
            await message.answer("Не удалось расшифровать ключ. Переустановите /setkey.")
            return

        preset = PROVIDER_PRESETS.get(used_provider)
        effective_base = key_row.base_url or (preset.base_url if preset else None)
        provider = OpenAICompatProvider(
            name=used_provider,
            api_key=api_key,
            base_url=effective_base,
        )

        # Модель: -m flag > дефолт для acedata (gpt-image-1) > глобальный IMAGE_MODEL
        if "model" in flags:
            model = flags["model"]
        elif used_provider == "acedata":
            model = "gpt-image-1"
        else:
            model = ctx.settings.image_model
        size = flags.get("size", ctx.settings.image_size)
        quality = flags.get("quality") or (ctx.settings.image_quality or None)

        # Лёгкая валидация — только для dall-e-*. gpt-image-* пропускаем, т.к. лайнап
        # моделей и поддерживаемых размеров расширяется (gpt-image-2 и пр.).
        valid_sizes: set[str] | None = None
        if model.startswith("dall-e-3"):
            valid_sizes = _DALLE3_SIZES
        elif model.startswith("dall-e-2"):
            valid_sizes = _DALLE2_SIZES
        if valid_sizes and size not in valid_sizes:
            await message.answer(
                f"Размер <code>{size}</code> не поддержан для модели "
                f"<code>{model}</code>. Допустимые: "
                + ", ".join(sorted(valid_sizes)),
                parse_mode="HTML",
            )
            return

        progress = await message.answer(f"🎨 Генерирую картинку ({model}, {size})…")
        t0 = time.monotonic()
        try:
            images = await provider.generate_image(
                prompt=prompt,
                model=model,
                size=size,
                quality=quality,
            )
        except ProviderError as exc:
            with suppress(Exception):
                await progress.delete()
            await message.answer(f"⚠ Ошибка генерации: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("/img failed")
            with suppress(Exception):
                await progress.delete()
            await message.answer(f"⚠ Ошибка: {exc}")
            return
        elapsed = time.monotonic() - t0

        with suppress(Exception):
            await progress.delete()

        ratio = _aspect_ratio(size)
        ratio_part = f"{ratio} ({size})" if ratio else size
        meta_bits = [model, ratio_part]
        if quality:
            meta_bits.append(quality)
        meta_bits.append("png")
        meta_line = " | ".join(meta_bits)

        for idx, img in enumerate(images, start=1):
            header = f"🎨 Готово ({elapsed:.1f}с)"
            prompt_block = (
                "📝 Твой промпт:\n" + html.escape(prompt[:600])
            )
            revised_block = ""
            if img.revised_prompt and img.revised_prompt.strip() != prompt.strip():
                revised_block = (
                    "\n\n<b>Revised:</b> "
                    + html.escape(img.revised_prompt[:400])
                )
            caption = (
                f"{header}\n\n"
                f"{prompt_block}{revised_block}\n\n"
                f"<code>{html.escape(meta_line)}</code>"
            )
            caption = caption[:1024]
            photo = BufferedInputFile(img.data, filename=f"img_{idx}.png")
            try:
                await message.answer_photo(photo, caption=caption, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                # если caption слишком длинный или с битым HTML — отдаём без caption
                photo = BufferedInputFile(img.data, filename=f"img_{idx}.png")
                await message.answer_photo(photo)

    # ---- Reply-клавиатура: обработчики кнопок главного меню ----

    @dp.message(F.text == BTN_CHAT)
    async def btn_chat(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        await message.answer(
            "Отправьте: <code>/chat ваш вопрос</code>\n"
            "Например: <code>/chat объясни принцип DRY</code>",
            parse_mode="HTML",
        )

    @dp.message(F.text == BTN_IMAGE)
    async def btn_image(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        await message.answer(
            "Отправьте: <code>/img промпт</code>\n"
            "Размер: <code>/img -s 1792x1024 закат над морем</code>",
            parse_mode="HTML",
        )

    @dp.message(F.text == BTN_HELP)
    async def btn_help(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        await send_long(message, _help_for(_user_id(message)), parse_mode="HTML")

    @dp.message(F.text == BTN_SETTINGS)
    async def btn_settings(message: TgMessage) -> None:
        # Кнопка остаётся для юзеров со старой reply-клавиатурой в кэше Telegram.
        # admin_kb/settings_kb показываем только админам — без admin-проверки
        # обычный юзер увидел бы админские кнопки (даже если callback'и потом
        # отказывают).
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        if not await _ensure_admin_msg(message):
            return
        await message.answer("⚙️ Настройки:", reply_markup=settings_kb())

    @dp.message(F.text == BTN_STATUS)
    async def btn_status(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        assert message.from_user
        user = await ctx.db.ensure_user(message.from_user.id)
        keys = await ctx.db.list_keys(message.from_user.id)
        wd = ctx.workdir_for(message.from_user.id)
        await message.answer(
            f"<b>Статус</b>\n"
            f"Провайдер: <code>{user.provider or '—'}</code>\n"
            f"Модель: <code>{user.model or '—'}</code>\n"
            f"Режим: <code>{user.mode}</code>\n"
            f"Ключей сохранено: {len(keys)}\n"
            f"Рабочая папка: <code>{wd}</code>",
            parse_mode="HTML",
        )

    @dp.message(Command("hidekb"))
    async def cmd_hidekb(message: TgMessage) -> None:
        await message.answer("Клавиатура скрыта. /menu — вернуть.", reply_markup=ReplyKeyboardRemove())

    # ---- CallbackQuery (inline-кнопки) ----

    @dp.callback_query(F.data.startswith("prov:"))
    async def cb_select_provider(query: CallbackQuery) -> None:
        if not is_allowed(ctx.settings, query.from_user.id):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        if not await _ensure_admin_cb(query):
            return
        provider_id = query.data.split(":", 1)[1] if query.data else ""
        if provider_id not in PROVIDER_PRESETS:
            await query.answer("Неизвестный провайдер", show_alert=True)
            return
        await ctx.db.update_user(query.from_user.id, provider=provider_id)
        preset = PROVIDER_PRESETS[provider_id]
        text = (
            f"Провайдер: <b>{preset.title}</b>\n"
            f"Получить API-ключ: {preset.api_key_url or '—'}\n\n"
            f"Дальше: <code>/setkey {provider_id} ваш-ключ</code>, потом выберите модель."
        )
        with suppress(Exception):
            await query.message.edit_text(text, parse_mode="HTML", reply_markup=models_kb(provider_id))
        await query.answer(f"✓ {preset.title}")

    @dp.callback_query(F.data.startswith("model:"))
    async def cb_select_model(query: CallbackQuery) -> None:
        if not is_allowed(ctx.settings, query.from_user.id):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        if not await _ensure_admin_cb(query):
            return
        model_id = query.data.split(":", 1)[1] if query.data else ""
        if not model_id:
            await query.answer("Пустая модель", show_alert=True)
            return
        await ctx.db.update_user(query.from_user.id, model=model_id)
        with suppress(Exception):
            await query.message.edit_text(
                f"Модель: <code>{html.escape(model_id)}</code>",
                parse_mode="HTML",
            )
        await query.answer(f"✓ {model_id}")

    @dp.callback_query(F.data.startswith("mode:"))
    async def cb_select_mode(query: CallbackQuery) -> None:
        if not is_allowed(ctx.settings, query.from_user.id):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        if not await _ensure_admin_cb(query):
            return
        mode = query.data.split(":", 1)[1] if query.data else ""
        if mode not in ("agent", "chat"):
            await query.answer("Неверный режим", show_alert=True)
            return
        await ctx.db.update_user(query.from_user.id, mode=mode)
        with suppress(Exception):
            await query.message.edit_text(
                f"Режим: <b>{mode}</b>",
                parse_mode="HTML",
                reply_markup=mode_kb(mode),
            )
        await query.answer(f"✓ {mode}")

    @dp.callback_query(F.data.startswith("act:"))
    async def cb_action(query: CallbackQuery) -> None:
        """Обработчик кнопок главного инлайн-меню (юзерских)."""
        if not is_allowed(ctx.settings, query.from_user.id):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        action = query.data.split(":", 1)[1] if query.data else ""
        uid = query.from_user.id
        admin = is_admin(ctx.settings, uid)

        if action == "chat":
            await query.answer()
            with suppress(Exception):
                await query.message.edit_text(
                    "💬 Просто введите вопрос сообщением — бот ответит с учётом истории.\n\n"
                    "Или <code>/chat ваш вопрос</code> — один вопрос без истории.",
                    parse_mode="HTML",
                    reply_markup=main_menu_inline_kb(is_admin=admin),
                )
            return
        if action == "image":
            await query.answer()
            with suppress(Exception):
                await query.message.edit_text(
                    "🎨 Отправьте: <code>/img промпт</code>\n\n"
                    "Примеры:\n"
                    "• <code>/img кот в очках</code>\n"
                    "• <code>/img -s 1792x1024 закат над морем</code>\n"
                    "• <code>/img -q high -m gpt-image-2 киберпанк</code>",
                    parse_mode="HTML",
                    reply_markup=main_menu_inline_kb(is_admin=admin),
                )
            return
        if action == "search":
            await query.answer()
            with suppress(Exception):
                await query.message.edit_text(
                    "🔍 Отправьте: <code>/search запрос</code>\n\n"
                    "Пример: <code>/search новости AI сегодня</code>",
                    parse_mode="HTML",
                    reply_markup=main_menu_inline_kb(is_admin=admin),
                )
            return
        if action == "status":
            user = await ctx.db.ensure_user(uid)
            keys = await ctx.db.list_keys(uid)
            text = (
                "<b>📊 Статус</b>\n"
                f"Провайдер: <code>{user.provider or '—'}</code>\n"
                f"Модель: <code>{user.model or '—'}</code>\n"
                f"Режим: <code>{user.mode}</code>\n"
                f"Ключей: {len(keys)}"
            )
            with suppress(Exception):
                await query.message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=main_menu_inline_kb(is_admin=admin),
                )
            await query.answer()
            return
        if action == "reset":
            await ctx.db.clear_history(uid)
            await query.answer("История очищена")
            with suppress(Exception):
                await query.message.edit_text(
                    "🧹 История разговора очищена.",
                    reply_markup=main_menu_inline_kb(is_admin=admin),
                )
            return
        if action == "help":
            await query.answer()
            with suppress(Exception):
                await query.message.edit_text(
                    _help_for(uid),
                    parse_mode="HTML",
                    reply_markup=main_menu_inline_kb(is_admin=admin),
                )
            return
        await query.answer()

    @dp.callback_query(F.data.startswith("menu:"))
    async def cb_menu(query: CallbackQuery) -> None:
        if not is_allowed(ctx.settings, query.from_user.id):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        action = query.data.split(":", 1)[1] if query.data else ""
        uid = query.from_user.id
        admin = is_admin(ctx.settings, uid)

        if action == "close":
            with suppress(Exception):
                await query.message.delete()
            await query.answer()
            return
        if action == "home":
            with suppress(Exception):
                await query.message.edit_text(
                    "Главное меню:",
                    reply_markup=main_menu_inline_kb(is_admin=admin),
                )
            await query.answer()
            return
        if action == "admin":
            if not await _ensure_admin_cb(query):
                return
            with suppress(Exception):
                await query.message.edit_text(
                    "<b>🛠 Админ-меню</b>",
                    parse_mode="HTML",
                    reply_markup=admin_kb(),
                )
            await query.answer()
            return
        # Все остальные menu:* — админские.
        if not await _ensure_admin_cb(query):
            return
        if action == "providers":
            with suppress(Exception):
                await query.message.edit_text("Выберите провайдера:", reply_markup=providers_kb())
            await query.answer()
            return
        if action == "models":
            user = await ctx.db.ensure_user(query.from_user.id)
            if not user.provider:
                await query.answer("Сначала выберите провайдера", show_alert=True)
                with suppress(Exception):
                    await query.message.edit_text("Выберите провайдера:", reply_markup=providers_kb())
                return
            with suppress(Exception):
                await query.message.edit_text(
                    f"Выберите модель для <b>{user.provider}</b>:",
                    parse_mode="HTML",
                    reply_markup=models_kb(user.provider),
                )
            await query.answer()
            return
        if action == "mode":
            user = await ctx.db.ensure_user(query.from_user.id)
            with suppress(Exception):
                await query.message.edit_text(
                    f"Текущий режим — <b>{user.mode}</b>. Выберите:",
                    parse_mode="HTML",
                    reply_markup=mode_kb(user.mode),
                )
            await query.answer()
            return
        if action == "keys":
            keys = await ctx.db.list_keys(query.from_user.id)
            if not keys:
                text = "Сохранённых ключей нет. Используйте /setkey &lt;provider&gt; &lt;api_key&gt;."
            else:
                lines = "\n".join(f"• {html.escape(k.provider)}" for k in keys)
                text = "Сохранённые ключи:\n" + lines
            with suppress(Exception):
                await query.message.edit_text(text, parse_mode="HTML", reply_markup=settings_kb())
            await query.answer()
            return
        if action == "reset":
            await ctx.db.clear_history(query.from_user.id)
            await query.answer("История очищена", show_alert=False)
            with suppress(Exception):
                await query.message.edit_text("История очищена.", reply_markup=settings_kb())
            return
        if action == "workdir":
            wd = ctx.workdir_for(query.from_user.id)
            items = []
            for entry in sorted(wd.iterdir()):
                kind = "d" if entry.is_dir() else "f"
                size = entry.stat().st_size if entry.is_file() else "-"
                items.append(f"{entry.name}\t{kind}\t{size}")
            listing = "\n".join(items) or "(пусто)"
            with suppress(Exception):
                await query.message.edit_text(
                    f"<code>{html.escape(str(wd))}</code>\n<pre>{html.escape(listing)}</pre>",
                    parse_mode="HTML",
                    reply_markup=admin_kb(),
                )
            await query.answer()
            return
        if action == "clearwd":
            wd = ctx.workdir_for(query.from_user.id)
            removed = 0
            for entry in list(wd.iterdir()):
                with suppress(Exception):
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                    removed += 1
            with suppress(Exception):
                await query.message.edit_text(
                    f"🗑 Рабочая папка очищена ({removed} элементов).",
                    reply_markup=admin_kb(),
                )
            await query.answer("Очищено")
            return
        await query.answer()

    # Заглушка — на случай неизвестных команд (только если начинается с /)
    @dp.message(F.text.startswith("/"))
    async def cmd_unknown(message: TgMessage) -> None:
        await message.answer("Неизвестная команда. /help")
