"""Клавиатуры (Inline / Reply) и подписи к командам для меню Telegram."""

from __future__ import annotations

from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.handlers.personas import PERSONAS
from bot.providers import PROVIDER_PRESETS

# Текстовые лейблы кнопок (используются и Reply-клавиатурой, и inline).
BTN_CHAT = "💬 Чат"
BTN_IMAGE = "🎨 Картинка"
BTN_SEARCH = "🔍 Поиск"
BTN_STATUS = "📊 Статус"
BTN_RESET = "🧹 Сброс"
BTN_HELP = "ℹ️ Помощь"
BTN_SETTINGS = "⚙️ Настройки"
BTN_ADMIN = "🛠 Админ"
BTN_PERSONA = "🎭 Стиль"

MAIN_MENU_LABELS = {BTN_CHAT, BTN_IMAGE, BTN_SETTINGS, BTN_HELP, BTN_STATUS}


def main_menu_inline_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Главное inline-меню. Админу показываем доп. кнопку «Админ»."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=BTN_CHAT, callback_data="act:chat"),
            InlineKeyboardButton(text=BTN_IMAGE, callback_data="act:image"),
        ],
        [
            InlineKeyboardButton(text=BTN_SEARCH, callback_data="act:search"),
            InlineKeyboardButton(text=BTN_PERSONA, callback_data="menu:persona"),
        ],
        [
            InlineKeyboardButton(text=BTN_STATUS, callback_data="act:status"),
            InlineKeyboardButton(text=BTN_RESET, callback_data="act:reset"),
        ],
        [
            InlineKeyboardButton(text=BTN_HELP, callback_data="act:help"),
        ],
    ]
    if is_admin:
        rows.append(
            [InlineKeyboardButton(text=BTN_ADMIN, callback_data="menu:admin")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Reply-клавиатура внизу чата (минимальная: чат / картинка / меню).

    Inline-меню — основной способ навигации. Reply-клавиатура оставлена
    для быстрого доступа к самым частым действиям.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CHAT), KeyboardButton(text=BTN_IMAGE)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Сообщение или /команда…",
    )


def admin_kb() -> InlineKeyboardMarkup:
    """Админ-меню (видно только админу)."""
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
                InlineKeyboardButton(text="📁 Workdir", callback_data="menu:workdir"),
                InlineKeyboardButton(text="🗑 Очистить wd", callback_data="menu:clearwd"),
            ],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="menu:home")],
        ]
    )


# Старое имя сохраняем как алиас, чтобы код не сломался.
def settings_kb() -> InlineKeyboardMarkup:
    return admin_kb()


def providers_kb() -> InlineKeyboardMarkup:
    """Inline-список провайдеров."""
    rows: list[list[InlineKeyboardButton]] = []
    cur: list[InlineKeyboardButton] = []
    for preset in PROVIDER_PRESETS.values():
        label = preset.id
        cur.append(InlineKeyboardButton(text=label, callback_data=f"prov:{preset.id}"))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="menu:admin")])
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
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def persona_kb(current: str | None) -> InlineKeyboardMarkup:
    """Inline-сетка персон. Текущая помечается ✅."""
    rows: list[list[InlineKeyboardButton]] = []
    cur_row: list[InlineKeyboardButton] = []
    for p in PERSONAS.values():
        prefix = "✅ " if current == p.key else ""
        cur_row.append(
            InlineKeyboardButton(
                text=f"{prefix}{p.emoji} {p.name}",
                callback_data=f"persona:{p.key}",
            )
        )
        if len(cur_row) == 2:
            rows.append(cur_row)
            cur_row = []
    if cur_row:
        rows.append(cur_row)
    off_label = "🚫 Выключить" if current else "✅ 🚫 Выключено"
    rows.append(
        [InlineKeyboardButton(text=off_label, callback_data="persona:off")]
    )
    rows.append(
        [InlineKeyboardButton(text="⬅ Назад", callback_data="menu:home")]
    )
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
            [InlineKeyboardButton(text="⬅ Назад", callback_data="menu:admin")],
        ]
    )


# --- Списки команд для меню Telegram (всплывающее по «/») ---

# Команды для обычных пользователей (короткий список). Видны всем по умолчанию.
USER_BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="chat", description="Один вопрос модели (без истории)"),
    BotCommand(command="img", description="Сгенерировать картинку"),
    BotCommand(command="search", description="Умный веб-поиск со ссылками"),
    BotCommand(command="yt", description="Пересказ YouTube-видео"),
    BotCommand(command="voice", description="Озвучить текст (TTS)"),
    BotCommand(command="clone", description="Сохранить ваш голосовой образец"),
    BotCommand(command="cvoice", description="Озвучить вашим голосом (F5-TTS)"),
    BotCommand(command="persona", description="Стиль общения (гопник, профессор, …)"),
    BotCommand(command="freekeys", description="Бесплатные LLM-провайдеры со ссылками"),
    BotCommand(command="acedata", description="AceData: модели и ориентир по ценам"),
    BotCommand(command="status", description="Текущие настройки"),
    BotCommand(command="reset", description="Очистить историю"),
    BotCommand(command="help", description="Справка"),
]

# Полный список команд (с админскими) — выставляется только для chat'ов админов
# через BotCommandScopeChat в bot/__main__.py.
ADMIN_BOT_COMMANDS: list[BotCommand] = USER_BOT_COMMANDS + [
    BotCommand(command="provider", description="Выбрать провайдера (admin)"),
    BotCommand(command="providers", description="Список провайдеров (admin)"),
    BotCommand(command="model", description="Выбрать модель (admin)"),
    BotCommand(command="models", description="Список моделей (admin)"),
    BotCommand(command="mode", description="agent / chat (admin)"),
    BotCommand(command="setkey", description="Сохранить API-ключ (admin)"),
    BotCommand(command="keys", description="Список ключей (admin)"),
    BotCommand(command="delkey", description="Удалить ключ (admin)"),
    BotCommand(command="workdir", description="Файлы рабочей папки (admin)"),
    BotCommand(command="clearwd", description="Очистить рабочую папку (admin)"),
]

# Старое имя — оставляем для совместимости с кодом, который импортирует BOT_COMMANDS.
BOT_COMMANDS: list[BotCommand] = USER_BOT_COMMANDS
