"""Команды бота: /start, /help, /provider, /setkey, /model, /mode, /reset, /workdir."""

from __future__ import annotations

import html
import logging
import shutil
from contextlib import suppress

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ReplyKeyboardRemove,
)
from aiogram.types import Message as TgMessage

from bot.handlers.common import (
    AppContext,
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
    main_menu_kb,
    mode_kb,
    models_kb,
    providers_kb,
    settings_kb,
)
from bot.providers import PROVIDER_PRESETS, Message, ProviderError
from bot.providers.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)


HELP_TEXT = """\
<b>Telegram AI Coder Bot</b> — мульти-провайдерный AI-бот: текст и картинки.

<b>Быстрые команды</b>
/chat &lt;запрос&gt; — one-shot ответ от текущей модели (без истории, без инструментов)
/img &lt;промпт&gt; — сгенерировать картинку через OpenAI Images API
   • размер: <code>/img -s 1792x1024 закат над морем</code>
   • качество (dall-e-3): <code>/img -q hd кот в очках</code>

<b>Настройка</b>
/start — приветствие
/help — эта справка
/providers — список встроенных провайдеров
/provider &lt;id&gt; — выбрать провайдера (openai, openrouter, anthropic, deepseek, groq, xai, mistral, google, custom)
/setkey &lt;provider&gt; &lt;api_key&gt; [base_url] — сохранить API-ключ (шифруется на диске)
/keys — показать какие ключи сохранены (без значений)
/delkey &lt;provider&gt; — удалить ключ
/model &lt;model_id&gt; — задать модель (напр. <code>gpt-4o</code>, <code>claude-sonnet-4-5-20250929</code>)
/models — показать рекомендованные модели для текущего провайдера
/mode agent|chat — режим обычной переписки (agent с инструментами / chat — без)
/status — текущие настройки
/reset — очистить историю разговора
/workdir — показать содержимое рабочей папки
/clearwd — очистить рабочую папку

<b>Использование</b>
1. Установите ключ: <code>/setkey openai sk-...</code>
2. Выберите провайдера и модель: <code>/provider openai</code>, <code>/model gpt-4o</code>
3. Быстрый чат: <code>/chat объясни SOLID за 3 предложения</code>
4. Картинка: <code>/img киберпанк Москва ночью</code>
5. Длинный диалог / coder-режим: просто пишите сообщения — в режиме <b>agent</b>
   модель сама вызовет bash/file-tools (как opencode/Cursor).
6. Можно прислать файл — он попадёт в рабочую папку.
"""


# Размеры, которые принимает OpenAI Images API
_DALLE3_SIZES = {"1024x1024", "1024x1792", "1792x1024"}
_DALLE2_SIZES = {"256x256", "512x512", "1024x1024"}
_GPT_IMAGE_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}


def _parse_img_args(args: str) -> tuple[str, dict[str, str]]:
    """Простой парсер: вытаскивает -s WxH и -q quality, остальное — промпт."""
    flags: dict[str, str] = {}
    tokens = args.split()
    prompt_parts: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-s", "--size") and i + 1 < len(tokens):
            flags["size"] = tokens[i + 1]
            i += 2
            continue
        if t in ("-q", "--quality") and i + 1 < len(tokens):
            flags["quality"] = tokens[i + 1]
            i += 2
            continue
        prompt_parts.append(t)
        i += 1
    return " ".join(prompt_parts).strip(), flags


def register_command_handlers(dp: Dispatcher, ctx: AppContext) -> None:
    @dp.message(Command("start"))
    async def cmd_start(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            await message.answer("Доступ запрещён. Свяжитесь с администратором бота.")
            return
        await message.answer(
            "👋 Бот готов к работе. Выберите действие на клавиатуре или используйте команды.",
            reply_markup=main_menu_kb(),
        )
        await send_long(message, HELP_TEXT, parse_mode="HTML")

    @dp.message(Command("help"))
    async def cmd_help(message: TgMessage) -> None:
        await send_long(message, HELP_TEXT, parse_mode="HTML")

    @dp.message(Command("menu"))
    async def cmd_menu(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        await message.answer("Меню:", reply_markup=main_menu_kb())

    @dp.message(Command("providers"))
    async def cmd_providers(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        await message.answer(
            "<b>Выберите провайдера</b>:\n" + provider_titles(),
            parse_mode="HTML",
            reply_markup=providers_kb(),
        )

    @dp.message(Command("provider"))
    async def cmd_provider(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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
    async def cmd_chat(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        prompt = (command.args or "").strip()
        if not prompt:
            await message.answer(
                "Использование: <code>/chat ваш вопрос</code>",
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
                messages=[Message(role="user", content=prompt)],
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

    @dp.message(Command("img"))
    async def cmd_img(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        if not command.args:
            await message.answer(
                "Использование: <code>/img промпт</code> или "
                "<code>/img -s 1792x1024 -q hd промпт</code>",
                parse_mode="HTML",
            )
            return
        prompt, flags = _parse_img_args(command.args)
        if not prompt:
            await message.answer("Промпт пуст после парсинга флагов.")
            return

        assert message.from_user
        # Для /img нужен OpenAI-совместимый ключ. По умолчанию берём ключ от
        # провайдера 'openai'. Если его нет — пробуем текущего провайдера
        # пользователя, при условии что это OpenAI-compat (не anthropic).
        key_row = await ctx.db.get_key(message.from_user.id, "openai")
        used_provider = "openai"
        if key_row is None:
            user = await ctx.db.ensure_user(message.from_user.id)
            if user.provider and user.provider != "anthropic":
                key_row = await ctx.db.get_key(message.from_user.id, user.provider)
                used_provider = user.provider
        if key_row is None:
            await message.answer(
                "Нужен OpenAI ключ для генерации картинок: "
                "<code>/setkey openai sk-...</code>",
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

        model = ctx.settings.image_model
        size = flags.get("size", ctx.settings.image_size)
        quality = flags.get("quality")

        # Лёгкая валидация
        valid_sizes = (
            _DALLE3_SIZES
            if model.startswith("dall-e-3")
            else _DALLE2_SIZES
            if model.startswith("dall-e-2")
            else _GPT_IMAGE_SIZES
            if model.startswith("gpt-image")
            else None
        )
        if valid_sizes and size not in valid_sizes:
            await message.answer(
                f"Размер <code>{size}</code> не поддержан для модели "
                f"<code>{model}</code>. Допустимые: "
                + ", ".join(sorted(valid_sizes)),
                parse_mode="HTML",
            )
            return

        progress = await message.answer(f"🎨 Генерирую картинку ({model}, {size})…")
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

        with suppress(Exception):
            await progress.delete()

        for idx, img in enumerate(images, start=1):
            caption_parts = [f"<i>{html.escape(prompt[:200])}</i>"]
            if img.revised_prompt and img.revised_prompt.strip() != prompt.strip():
                caption_parts.append(
                    f"\n<b>Revised:</b> {html.escape(img.revised_prompt[:600])}"
                )
            caption = "".join(caption_parts)[:1024]
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
        await send_long(message, HELP_TEXT, parse_mode="HTML")

    @dp.message(F.text == BTN_SETTINGS)
    async def btn_settings(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
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

    @dp.callback_query(F.data.startswith("menu:"))
    async def cb_menu(query: CallbackQuery) -> None:
        if not is_allowed(ctx.settings, query.from_user.id):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        action = query.data.split(":", 1)[1] if query.data else ""
        if action == "close":
            with suppress(Exception):
                await query.message.delete()
            await query.answer()
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
                    reply_markup=settings_kb(),
                )
            await query.answer()
            return
        await query.answer()

    # Заглушка — на случай неизвестных команд (только если начинается с /)
    @dp.message(F.text.startswith("/"))
    async def cmd_unknown(message: TgMessage) -> None:
        await message.answer("Неизвестная команда. /help")
