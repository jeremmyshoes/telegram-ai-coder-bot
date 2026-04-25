"""Команды бота: /start, /help, /provider, /setkey, /model, /mode, /reset, /workdir."""

from __future__ import annotations

import logging
import shutil
from contextlib import suppress

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message as TgMessage

from bot.handlers.common import (
    AppContext,
    is_allowed,
    provider_titles,
    send_long,
)
from bot.providers import PROVIDER_PRESETS

logger = logging.getLogger(__name__)


HELP_TEXT = """\
<b>Telegram AI Coder Bot</b> — аналог opencode/Cursor в Telegram.

<b>Базовые команды</b>
/start — приветствие
/help — эта справка
/providers — список встроенных провайдеров
/provider &lt;id&gt; — выбрать провайдера (openai, openrouter, anthropic, deepseek, groq, xai, mistral, google, custom)
/setkey &lt;provider&gt; &lt;api_key&gt; [base_url] — сохранить API-ключ (шифруется на диске)
/keys — показать какие ключи сохранены (без значений)
/delkey &lt;provider&gt; — удалить ключ
/model &lt;model_id&gt; — задать модель (напр. <code>gpt-4o</code>, <code>claude-sonnet-4-5-20250929</code>, <code>anthropic/claude-3.5-sonnet</code>)
/models — показать рекомендованные модели для текущего провайдера
/mode agent|chat — режим (agent с инструментами / обычный chat)
/status — текущие настройки
/reset — очистить историю разговора
/workdir — показать содержимое рабочей папки
/clearwd — очистить рабочую папку

<b>Использование</b>
1. Установите ключ: <code>/setkey openai sk-...</code>
2. Выберите провайдера и модель: <code>/provider openai</code>, <code>/model gpt-4o</code>
3. Просто пишите сообщения — в режиме <b>agent</b> модель сама запустит bash/прочитает файлы и т.д.
4. Можно прислать файл — он попадёт в рабочую папку.
"""


def register_command_handlers(dp: Dispatcher, ctx: AppContext) -> None:
    @dp.message(Command("start"))
    async def cmd_start(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            await message.answer("Доступ запрещён. Свяжитесь с администратором бота.")
            return
        await send_long(message, HELP_TEXT, parse_mode="HTML")

    @dp.message(Command("help"))
    async def cmd_help(message: TgMessage) -> None:
        await send_long(message, HELP_TEXT, parse_mode="HTML")

    @dp.message(Command("providers"))
    async def cmd_providers(message: TgMessage) -> None:
        await send_long(message, "<b>Провайдеры</b>:\n" + provider_titles(), parse_mode="HTML")

    @dp.message(Command("provider"))
    async def cmd_provider(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        provider_id = (command.args or "").strip()
        if not provider_id:
            await message.answer(
                "Использование: <code>/provider openai</code>\n\n" + provider_titles(),
                parse_mode="HTML",
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
        if not model:
            await message.answer("Использование: <code>/model gpt-4o</code>", parse_mode="HTML")
            return
        assert message.from_user
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
        if mode not in ("agent", "chat"):
            await message.answer("Использование: /mode agent | /mode chat")
            return
        assert message.from_user
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

    # Заглушка — на случай неизвестных команд (только если начинается с /)
    @dp.message(F.text.startswith("/"))
    async def cmd_unknown(message: TgMessage) -> None:
        await message.answer("Неизвестная команда. /help")
