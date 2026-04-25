"""Клавиатуры (Reply / Inline) и подписи к командам для меню Telegram."""

from __future__ import annotations

from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.providers import PROVIDER_PRESETS

# Текстовые лейблы кнопок главного меню (внизу клавиатуры).
BTN_CHAT = "💬 Чат"
BTN_IMAGE = "🎨 Картинка"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "ℹ️ Помощь"
BTN_STATUS = "📊 Статус"

MAIN_MENU_LABELS = {BTN_CHAT, BTN_IMAGE, BTN_SETTINGS, BTN_HELP, BTN_STATUS}


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Reply-клавиатура внизу чата — основные действия."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CHAT), KeyboardButton(text=BTN_IMAGE)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_STATUS)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Введите команду или сообщение…",
    )


def settings_kb() -> InlineKeyboardMarkup:
    """Inline-меню настроек."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔌 Провайдер", callback_data="menu:providers"),
                InlineKeyboardButton(text="🧠 Модель", callback_data="menu:models"),
            ],
            [
                InlineKeyboardButton(text="🔁 Режим", callback_data="menu:mode"),
                InlineKeyboardButton(text="🔑 Ключи", callback_data="menu:keys"),
            ],
            [
                InlineKeyboardButton(text="🧹 Очистить историю", callback_data="menu:reset"),
                InlineKeyboardButton(text="📁 Workdir", callback_data="menu:workdir"),
            ],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:close")],
        ]
    )


def providers_kb() -> InlineKeyboardMarkup:
    """Inline-список провайдеров."""
    rows: list[list[InlineKeyboardButton]] = []
    cur: list[InlineKeyboardButton] = []
    for preset in PROVIDER_PRESETS.values():
        # Короткий лейбл для кнопки
        label = preset.id
        cur.append(InlineKeyboardButton(text=label, callback_data=f"prov:{preset.id}"))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="menu:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_kb(provider_id: str) -> InlineKeyboardMarkup:
    preset = PROVIDER_PRESETS.get(provider_id)
    rows: list[list[InlineKeyboardButton]] = []
    if preset:
        for m in preset.suggested_models:
            rows.append([InlineKeyboardButton(text=m, callback_data=f"model:{m}")])
    if not rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text="(нет подсказок — задайте /model вручную)",
                    callback_data="menu:close",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 Сменить провайдера", callback_data="menu:providers")]
    )
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mode_kb(current: str | None) -> InlineKeyboardMarkup:
    def _label(value: str, title: str) -> str:
        return f"✅ {title}" if current == value else title

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_label("agent", "🤖 agent"), callback_data="mode:agent"),
                InlineKeyboardButton(text=_label("chat", "💬 chat"), callback_data="mode:chat"),
            ],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="menu:close")],
        ]
    )


# Список команд для меню Telegram (отображается при клике "/")
BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="chat", description="Один вопрос модели (без истории)"),
    BotCommand(command="img", description="Сгенерировать картинку"),
    BotCommand(command="provider", description="Выбрать провайдера"),
    BotCommand(command="model", description="Выбрать модель"),
    BotCommand(command="mode", description="Режим: agent / chat"),
    BotCommand(command="setkey", description="Сохранить API-ключ"),
    BotCommand(command="keys", description="Список сохранённых ключей"),
    BotCommand(command="status", description="Текущие настройки"),
    BotCommand(command="reset", description="Очистить историю"),
    BotCommand(command="workdir", description="Файлы рабочей папки"),
    BotCommand(command="clearwd", description="Очистить рабочую папку"),
    BotCommand(command="help", description="Справка"),
]
