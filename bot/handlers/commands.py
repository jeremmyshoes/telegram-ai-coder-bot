"""Команды бота: /start, /help, /provider, /setkey, /model, /mode, /reset, /workdir."""

from __future__ import annotations

import html
import io
import logging
import math
import re
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
    send_llm_response,
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
    persona_kb,
    providers_kb,
    settings_kb,
)
from bot.handlers.personas import (
    get_persona,
    list_personas_html,
)
from bot.providers import PROVIDER_PRESETS, ImageData, Message, ProviderError
from bot.providers.openai_compat import OpenAICompatProvider
from bot.tools.deep_search import run_deep_search
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
/search &lt;вопрос&gt; — Perplexity-style: query expansion → 15 источников → ответ + follow-ups
/search -n 18 &lt;вопрос&gt; — взять N итоговых источников (1..20)
/search -quick &lt;запрос&gt; — быстрый режим (один запрос, ~8 источников)
/search -raw &lt;запрос&gt; — сырой список ссылок без LLM-синтеза
/yt &lt;url&gt; — пересказ YouTube-видео (Whisper → gpt-5)
/yt -full &lt;url&gt; — полный транскрипт без пересказа
/persona &lt;ключ&gt; — стиль общения (gopnik, professor, child, …)
/freekeys — список бесплатных LLM-провайдеров со ссылками
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
• Документ (PDF, DOCX, XLSX, RTF) — бот извлечёт текст и передаст модели.
  Если PDF — скан (нет текста), бот распознает страницы через vision.
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

# /freekeys — список халявных провайдеров с прямыми ссылками на регистрацию.
# Цифры лимитов — актуальные на апрель 2026. Источники:
#   OpenRouter  — https://openrouter.ai/docs/api/reference/limits
#   Groq        — https://console.groq.com/docs/rate-limits
#   Cerebras    — https://inference-docs.cerebras.ai/support/rate-limits
#   Gemini      — https://ai.google.dev/gemini-api/docs/rate-limits
#   GitHub Models — https://docs.github.com/en/github-models
#   SambaNova   — https://docs.sambanova.ai/docs/en/models/rate-limits
# Все сервисы меняют лимиты — цифры могут устареть.
_FREEKEYS_TEXT = """\
<b>🆓 Халявные LLM-провайдеры (апрель 2026)</b>

Все дают рабочие ключи без оплаты карты. Порядок: зарегали → забрали
ключ → админу <code>/setkey &lt;provider&gt; &lt;ключ&gt;</code> →
<code>/provider &lt;provider&gt;</code> + <code>/model &lt;model&gt;</code>.

<b>1. OpenRouter</b> — агрегатор сотни моделей через один ключ.
   • <b>Лимиты free</b>: 20 req/min, 200 req/день. После пополнения на $10
     дневной лимит растёт до 1000 req/день, а до этого — только free-модели.
   • <b>Реальные free-модели (ID оканчивается на <code>:free</code>, сверено
     с /api/v1/models, апрель 2026)</b>:
     <code>openai/gpt-oss-120b:free</code>,
     <code>openai/gpt-oss-20b:free</code>,
     <code>qwen/qwen3-coder:free</code>,
     <code>qwen/qwen3-next-80b-a3b-instruct:free</code>,
     <code>z-ai/glm-4.5-air:free</code>,
     <code>minimax/minimax-m2.5:free</code>,
     <code>google/gemma-3-27b-it:free</code>,
     <code>nvidia/nemotron-3-nano-30b-a3b:free</code>,
     <code>nousresearch/hermes-3-llama-3.1-405b:free</code>,
     <code>meta-llama/llama-3.2-3b-instruct:free</code>.
   • ⚠ <b>Gemini на free-тире OpenRouter сейчас нет</b> — только на платных
     моделях (прежний <code>gemini-2.0-flash-exp:free</code> отозван Google).
   • <b>Плюс</b>: <i>работает из РФ/РБ без VPN</i>.
   • Ключ: <a href="https://openrouter.ai/keys">openrouter.ai/keys</a>
   • Бот: <code>/setkey openrouter sk-or-…</code>

<b>2. Groq</b> — самый быстрый инференс в мире (LPU-чипы, ~500 tok/сек).
   • <b>Лимиты free</b>: 30 req/min, 1000 req/день, 6K токенов/min для
     большинства моделей. У llama-3.1-8b дневной лимит — 14400 req/день.
   • <b>Модели</b>: llama-3.3-70b-versatile, llama-3.1-8b-instant,
     llama-4-scout-17b, llama-4-maverick-17b, deepseek-r1-distill-70b,
     qwen-qwq-32b, gemma-2-9b, openai/gpt-oss-120b, openai/gpt-oss-20b.
   • Ключ: <a href="https://console.groq.com/keys">console.groq.com/keys</a>
   • Бот: <code>/setkey groq gsk_…</code>

<b>3. Cerebras</b> — конкурент Groq на WSE-чипах, <b>1 000 000 токенов/день</b>.
   • <b>Лимиты free</b>: 30 req/min, 60K–100K токенов/min, context сейчас
     временно обрезан до 8192 токенов для всех free-моделей.
   • <b>Модели</b>: llama-3.3-70b, qwen-3-coder-480b, zai-glm-4.7,
     gpt-oss-120b (qwen-3-235b и llama3.1-8b <i>deprecated 27 May 2026</i>).
   • Ключ: <a href="https://cloud.cerebras.ai">cloud.cerebras.ai</a>
   • Бот: <code>/setkey cerebras csk-…</code>

<b>4. Google AI Studio (Gemini)</b> — 1500 req/день на flash-lite.
   • <b>Лимиты free</b> (после снижения в декабре 2025):
     gemini-2.5-flash-lite — 15 RPM / 1500 RPD,
     gemini-2.5-flash — 10 RPM / 250 RPD,
     gemini-2.5-pro — 5 RPM / 100 RPD. 250K tokens/min на всех.
   • ⚠ <b>Недоступно в РФ/РБ</b> — нужен VPN (US/EU/UA) при регистрации
     и первом запросе, иначе Google блочит по IP/региону аккаунта.
   • Free tier <i>использует ваши данные для обучения моделей</i>.
   • Ключ: <a href="https://aistudio.google.com/apikey">aistudio.google.com/apikey</a>
   • Бот: <code>/setkey custom &lt;ключ&gt; https://generativelanguage.googleapis.com/v1beta/openai/</code>

<b>5. GitHub Models</b> — ~50 req/день на high-tier (gpt-4o/claude/grok).
   • <b>Лимиты free</b>: ~50 req/день на high-tier, больше на low-tier.
     В апреле 2026 лимиты ужесточили, могут менять.
   • <b>Модели</b>: gpt-4o, gpt-4o-mini, o1-mini, claude-3.5-sonnet,
     llama-3.3-70b, deepseek-v3, mistral, phi-4, grok-3, cohere-command-r.
   • Ключ = GitHub <b>fine-grained PAT</b> со scope <code>models:read</code>:
     <a href="https://github.com/settings/personal-access-tokens">github.com/settings/personal-access-tokens</a>
   • Бот: <code>/setkey custom ghp_… https://models.github.ai/inference</code>

<b>6. SambaNova Cloud</b> — llama-3.3-70b/405b, DeepSeek-V3.1, MiniMax-M2.5.
   • <b>Лимиты free</b>: 20 req/min и дневной token-cap (варьируется по
     модели). Developer tier (с картой) поднимает до 240 RPM.
   • <b>Модели</b>: Llama-3.3-70B, DeepSeek-V3.1, MiniMax-M2.5, gpt-oss-120b.
   • Ключ: <a href="https://cloud.sambanova.ai">cloud.sambanova.ai</a>
   • Бот: <code>/setkey sambanova …</code>

⚠ <b>HuggingFace Inference</b> — убрал из списка: с 2025 года у free-юзера
всего $0.10 кредитов/мес, этого хватает буквально на несколько запросов.
PRO ($9/мес) даёт $2/мес — всё равно мало. Не халява.

<b>Итого</b>: если нужна просто рабочая халява без возни — Groq + OpenRouter.
Если важен длинный context и объём — Cerebras (1M токенов/день).
Gemini бесплатно возможен только через AI Studio с VPN (в РФ/РБ заблокирован).
"""


# /acedata — показывается если у юзера (или у админа) нет сохранённого ключа.
_ACEDATA_NOKEY_TEXT = """\
<b>AceData Cloud — ключ не сохранён</b>

AceData — агрегатор, даёт единый ключ ко всем популярным моделям
(GPT, Claude, Gemini, DeepSeek, Grok, Kimi, + картинки/видео/музыка).
Биллинг pay-as-you-go в Credits (1 Credit ≈ $0.01).

<b>Что сделать</b>:
1. Зарегистрироваться: <a href="https://platform.acedata.cloud">platform.acedata.cloud</a>
2. Создать API-Token в консоли → вкладка Tokens.
3. В боте: <code>/setkey acedata ваш-токен</code>
4. Снова: <code>/acedata</code> — покажу актуальный список моделей и цены.

Смотреть баланс Credits и фактические списания можно только в их
дашборде — публичного balance-endpoint'а они не публикуют.
"""

# Ориентир по ценам для /acedata — собраны из официальных прайсов OpenAI/
# Anthropic/Google/DeepSeek/xAI (на ~апрель 2026). AceData обычно держит
# 80–95% от официальных цен. Цены in/out за 1M токенов.
_ACEDATA_PRICE_HINTS: dict[str, tuple[str, str]] = {
    "gpt-4o-mini": ("$0.15", "$0.60"),
    "gpt-4o": ("$2.50", "$10"),
    "gpt-4.1": ("$2.00", "$8"),
    "gpt-4.1-mini": ("$0.40", "$1.60"),
    "gpt-5": ("$1.25", "$10"),
    "gpt-5-mini": ("$0.25", "$2"),
    "o3": ("$2", "$8"),
    "o4-mini": ("$1.10", "$4.40"),
    "claude-3-5-haiku": ("$0.80", "$4"),
    "claude-3-5-sonnet": ("$3", "$15"),
    "claude-3-7-sonnet": ("$3", "$15"),
    "claude-opus-4": ("$15", "$75"),
    "claude-sonnet-4": ("$3", "$15"),
    "gemini-2.5-flash": ("$0.30", "$2.50"),
    "gemini-2.5-flash-lite": ("$0.10", "$0.40"),
    "gemini-2.5-pro": ("$1.25", "$10"),
    "deepseek-chat": ("$0.27", "$1.10"),
    "deepseek-reasoner": ("$0.55", "$2.19"),
    "grok-4": ("$3", "$15"),
    "grok-3": ("$3", "$15"),
    "kimi-k2": ("$0.15", "$2.50"),
}


def _classify_acedata_model(model_id: str) -> str:
    """Определяет семью модели для группировки в /acedata."""
    m = model_id.lower()
    if m.startswith(("gpt-image", "dall-e")):
        return "🎨 Картинки (OpenAI)"
    if m.startswith(("flux", "black-forest", "sd-", "stable-diffusion", "seedream", "nano-banana", "midjourney", "mj-")):
        return "🎨 Картинки"
    if m.startswith(("sora", "veo", "luma", "kling", "hailuo", "seedance", "wan")):
        return "🎬 Видео"
    if m.startswith(("suno", "fish", "producer", "tts-", "whisper")):
        return "🎵 Аудио"
    if m.startswith(("gpt-", "chatgpt", "o1", "o3", "o4")):
        return "🤖 OpenAI (GPT/o*)"
    if m.startswith("claude"):
        return "🤖 Anthropic (Claude)"
    if m.startswith("gemini"):
        return "🤖 Google (Gemini)"
    if m.startswith("deepseek"):
        return "🤖 DeepSeek"
    if m.startswith("grok"):
        return "🤖 xAI (Grok)"
    if m.startswith(("kimi", "moonshot")):
        return "🤖 Moonshot (Kimi)"
    if m.startswith("text-embedding"):
        return "🧮 Embeddings"
    return "📦 Прочее"


def _price_hint_for(model_id: str) -> str | None:
    """Ищет приблизительную цену для модели (по префиксу)."""
    ml = model_id.lower()
    best_key: str | None = None
    for key in _ACEDATA_PRICE_HINTS:
        if ml.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return None
    inp, out = _ACEDATA_PRICE_HINTS[best_key]
    return f"{inp} / {out}"


def _build_acedata_info(models: list[str], fetch_error: str | None) -> str:
    """Собирает HTML-текст для ответа /acedata."""
    lines: list[str] = ["<b>🔷 AceData Cloud</b>"]
    if fetch_error:
        lines.append(
            "⚠ Не смог получить список моделей через API: "
            f"<code>{html.escape(fetch_error)}</code>\n"
            "Проверьте что ключ и base_url (<code>https://api.acedata.cloud/openai</code>) живые."
        )
    elif not models:
        lines.append(
            "⚠ API вернул пустой список моделей. Возможно ключ не привязан "
            "к подписке или истёк — проверьте в дашборде."
        )
    else:
        lines.append(f"Доступно моделей: <b>{len(models)}</b>")
        groups: dict[str, list[str]] = {}
        for m in models:
            groups.setdefault(_classify_acedata_model(m), []).append(m)
        # Выводим группы в предсказуемом порядке.
        order = [
            "🤖 OpenAI (GPT/o*)",
            "🤖 Anthropic (Claude)",
            "🤖 Google (Gemini)",
            "🤖 DeepSeek",
            "🤖 xAI (Grok)",
            "🤖 Moonshot (Kimi)",
            "🎨 Картинки (OpenAI)",
            "🎨 Картинки",
            "🎬 Видео",
            "🎵 Аудио",
            "🧮 Embeddings",
            "📦 Прочее",
        ]
        for group in order:
            items = groups.get(group) or []
            if not items:
                continue
            lines.append("")
            lines.append(f"<b>{group}</b>")
            for mid in items[:30]:
                price = _price_hint_for(mid)
                suffix = f" — <i>≈ {price} / 1M</i>" if price else ""
                lines.append(f"• <code>{html.escape(mid)}</code>{suffix}")
            if len(items) > 30:
                lines.append(f"  …и ещё {len(items) - 30}")
    lines.append("")
    lines.append(
        "<b>Цены</b>: AceData — реселлер, обычно 80–95% от официальных "
        "прайсов OpenAI/Anthropic/Google. Выше по моделям указаны "
        "ориентировочные цены <i>input / output за 1M токенов</i>."
    )
    lines.append(
        "<b>Баланс Credits</b>: только в дашборде — "
        "<a href=\"https://platform.acedata.cloud\">platform.acedata.cloud</a>."
    )
    lines.append(
        "<b>Использование в боте</b>: "
        "<code>/provider acedata</code> + <code>/model &lt;id&gt;</code> — "
        "обычный чат. <code>/img -p acedata -m gpt-image-1 &lt;промпт&gt;</code> — "
        "картинка через OpenAI gpt-image-1 через acedata."
    )
    return "\n".join(lines)


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
        first_name = ""
        if message.from_user and message.from_user.first_name:
            first_name = html.escape(message.from_user.first_name)
        greeting_who = f", <b>{first_name}</b>" if first_name else ""
        await message.answer(
            f"👋 Привет{greeting_who}! Я <b>AI Helper</b> — готов к работе.\n\n"
            "Что умею:\n"
            "• 💬 <b>Чат</b> — пишите вопрос обычным сообщением\n"
            "• 🎨 <b>Картинки</b> — <code>/img промпт</code>\n"
            "• 🔍 <b>Поиск в интернете</b> — <code>/search вопрос</code>\n"
            "• 📄 <b>Файлы и фото</b> — пришлите PDF/Word/Excel/картинку, "
            "перескажу содержание\n"
            "• 🎬 <b>YouTube</b> — <code>/yt &lt;ссылка&gt;</code> для пересказа\n"
            "• 🎙 <b>Голос</b> — <code>/voice текст</code> (озвучка), "
            "<code>/clone</code> + <code>/cvoice</code> (ваш голос)\n"
            "• 🎭 <b>Стиль общения</b> — <code>/persona</code> "
            "(гопник, профессор, Fanuc-оператор и др.)\n\n"
            "Жмите кнопку ниже или сразу пишите вопрос 👇",
            parse_mode="HTML",
            reply_markup=main_menu_inline_kb(is_admin=is_admin(ctx.settings, uid)),
        )

    @dp.message(Command("help"))
    async def cmd_help(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        await send_long(message, _help_for(_user_id(message)), parse_mode="HTML")

    @dp.message(Command("freekeys"))
    async def cmd_freekeys(message: TgMessage) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        await send_long(message, _FREEKEYS_TEXT, parse_mode="HTML")

    @dp.message(Command("acedata"))
    async def cmd_acedata(message: TgMessage) -> None:
        """Инфо о AceData: актуальный список моделей, прайс-чит и ссылки.

        Тянет `GET /openai/v1/models` с сохранённым acedata-ключом, группирует
        модели по семьям и дополняет справочными ценами (публичных
        прайс-endpoint'ов у acedata нет, балансы — только в дашборде).
        """
        uid = _user_id(message)
        if not is_allowed(ctx.settings, uid):
            return
        assert uid is not None

        # Ищем acedata-ключ: сам юзер → затем админы (как в find_image_key).
        key_row = None
        candidates = [uid]
        with suppress(Exception):
            for admin_id in ctx._fallback_admin_ids():  # noqa: SLF001
                if admin_id not in candidates:
                    candidates.append(admin_id)
        for cand in candidates:
            key_row = await ctx.db.get_key(cand, "acedata")
            if key_row is not None:
                break

        if key_row is None:
            await send_long(message, _ACEDATA_NOKEY_TEXT, parse_mode="HTML")
            return

        try:
            api_key = ctx.vault.decrypt(key_row.encrypted)
        except RuntimeError:
            await message.answer(
                "Не удалось расшифровать acedata-ключ — переустановите через "
                "<code>/setkey acedata …</code>.",
                parse_mode="HTML",
            )
            return

        base_url = (key_row.base_url or "https://api.acedata.cloud/openai").rstrip("/")
        models_url = f"{base_url}/v1/models"

        models: list[str] = []
        fetch_error: str | None = None
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20.0) as cli:
                r = await cli.get(
                    models_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if r.status_code == 200:
                payload = r.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            model_id = item.get("id")
                            if isinstance(model_id, str):
                                models.append(model_id)
                models.sort()
            else:
                fetch_error = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            fetch_error = f"{type(exc).__name__}: {exc}"

        text = _build_acedata_info(models, fetch_error)
        await send_long(message, text, parse_mode="HTML")

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
        cur_persona = get_persona(user.persona)
        persona_line = (
            f"{cur_persona.emoji} {cur_persona.name} (<code>{cur_persona.key}</code>)"
            if cur_persona
            else "—"
        )
        text = (
            f"<b>Статус</b>\n"
            f"Провайдер: <code>{user.provider or '—'}</code>\n"
            f"Модель: <code>{user.model or '—'}</code>\n"
            f"Режим: <code>{user.mode}</code>\n"
            f"Стиль: {persona_line}\n"
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

    @dp.message(Command("persona"))
    async def cmd_persona(
        message: TgMessage, command: CommandObject
    ) -> None:
        if not is_allowed(ctx.settings, _user_id(message)):
            return
        assert message.from_user
        user = await ctx.db.ensure_user(message.from_user.id)
        arg = (command.args or "").strip().lower()

        # Без аргумента — показываем список + текущую персону + клавиатуру
        if not arg:
            current = get_persona(user.persona)
            cur_line = (
                f"Сейчас: <b>{current.emoji} {current.name}</b> "
                f"(<code>{current.key}</code>)"
                if current
                else "Сейчас: <b>обычный режим</b> (без стиля)"
            )
            text = f"{cur_line}\n\n{list_personas_html()}"
            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=persona_kb(user.persona),
            )
            return

        if arg in {"off", "none", "сбросить", "выкл", "default"}:
            await ctx.db.update_user(message.from_user.id, clear_persona=True)
            await message.answer(
                "Стиль выключен — обычный режим.",
                reply_markup=persona_kb(None),
            )
            return

        p = get_persona(arg)
        if p is None:
            await message.answer(
                f"Неизвестный стиль: <code>{html.escape(arg)}</code>.\n\n"
                + list_personas_html(),
                parse_mode="HTML",
            )
            return
        await ctx.db.update_user(message.from_user.id, persona=p.key)
        await message.answer(
            f"Включён стиль: <b>{p.emoji} {p.name}</b>",
            parse_mode="HTML",
            reply_markup=persona_kb(p.key),
        )

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
        await send_llm_response(message, text)

    @dp.message(Command("search"))
    async def cmd_search(message: TgMessage, command: CommandObject) -> None:
        if not is_allowed(ctx.settings, message.from_user.id if message.from_user else None):
            return
        raw_query = (command.args or "").strip()
        # Флаги:
        #   -raw       — только сырой список ссылок (без LLM).
        #   -quick     — старый режим: один запрос, без query-expansion.
        #   -n N       — лимит итоговых источников (1..20).
        # Можно комбинировать в любом порядке: `/search -quick -n 8 запрос`.
        raw_mode = False
        quick_mode = False
        num_override: int | None = None
        while True:
            if raw_query.startswith("-raw "):
                raw_mode = True
                raw_query = raw_query[len("-raw "):].strip()
                continue
            if raw_query == "-raw":
                raw_query = ""
                break
            if raw_query.startswith("-quick "):
                quick_mode = True
                raw_query = raw_query[len("-quick "):].strip()
                continue
            if raw_query == "-quick":
                raw_query = ""
                break
            m = re.match(r"-n\s+(\d+)(?:\s+(.*))?$", raw_query, flags=re.DOTALL)
            if m:
                try:
                    num_override = max(1, min(int(m.group(1)), 20))
                except ValueError:
                    num_override = None
                raw_query = (m.group(2) or "").strip()
                continue
            break

        if not raw_query:
            await message.answer(
                "Использование: <code>/search ваш вопрос</code> — deep-режим в стиле "
                "Perplexity: бот разбивает вопрос на несколько под-запросов, ищет по "
                "ним параллельно, читает 15 источников и пишет связный ответ со "
                "ссылками <code>[1]</code>, <code>[2]</code>, … + 3 уточняющих "
                "вопроса в конце.\n\n"
                "Флаги:\n"
                "• <code>/search -n 18 запрос</code> — взять N итоговых "
                "источников (1..20)\n"
                "• <code>/search -quick запрос</code> — быстрый режим (один "
                "поисковый запрос, ~8 источников, без расширения)\n"
                "• <code>/search -raw запрос</code> — только список ссылок "
                "без LLM-синтеза\n"
                "• флаги комбинируются: "
                "<code>/search -quick -n 6 запрос</code>",
                parse_mode="HTML",
            )
            return
        query = raw_query
        assert message.from_user

        # === Сырой режим — просто выдача ссылок. ==========================
        if raw_mode:
            raw_num = num_override if num_override is not None else 15
            thinking = await message.answer(f"🔎 Ищу ({raw_num} источников)…")
            try:
                results, used = await web_search(
                    query,
                    google_api_key=ctx.settings.google_search_api_key,
                    google_cse_id=ctx.settings.google_search_cse_id,
                    num_results=raw_num,
                )
            except WebSearchError as exc:
                with suppress(Exception):
                    await thinking.delete()
                await message.answer(
                    f"⚠ Ошибка поиска: {html.escape(str(exc))}", parse_mode="HTML"
                )
                return
            with suppress(Exception):
                await thinking.delete()
            header = f"(via {used})\n\n"
            await send_long(message, header + format_search_results(results))
            return

        # === Perplexity-режим (deep / quick) — нужен openai-ключ. =========
        key_pair = await ctx.find_openai_key(message.from_user.id)
        if key_pair is None:
            await message.answer(
                "Для perplexity-режима нужен OpenAI-ключ (модель "
                f"<code>{html.escape(ctx.settings.search_synth_model)}</code>).\n"
                "Админу: <code>/setkey openai sk-...</code>\n\n"
                "Либо используйте <code>/search -raw запрос</code> — выдаст "
                "сырые ссылки без LLM-синтеза.",
                parse_mode="HTML",
            )
            return
        api_key, base_url = key_pair

        # Дефолты по режимам.
        if quick_mode:
            max_sources = num_override if num_override is not None else 8
        else:
            max_sources = num_override if num_override is not None else 15

        progress = await message.answer("🔎 Готовлю поиск…")

        async def _on_progress(stage: str, info: dict) -> None:
            if stage == "expand":
                txt = "🧠 Расширяю запрос на под-запросы…"
            elif stage == "search":
                queries = info.get("queries") or []
                txt = f"🔍 Ищу {len(queries)} под-запросов параллельно…"
            elif stage == "fetch":
                count = info.get("count") or 0
                txt = f"📚 Читаю {count} источников…"
            elif stage == "synth":
                sources = info.get("sources") or 0
                txt = f"🧠 Синтезирую ответ из {sources} источников…"
            else:
                return
            with suppress(Exception):
                await progress.edit_text(txt)

        synth_model = ctx.settings.search_synth_model
        try:
            output = await run_deep_search(
                query,
                api_key=api_key,
                base_url=base_url,
                synth_model=synth_model,
                google_api_key=ctx.settings.google_search_api_key,
                google_cse_id=ctx.settings.google_search_cse_id,
                max_sources=max_sources,
                # В quick-режиме увеличиваем `per_subquery` чтобы добрать
                # столько же кандидатов из одного запроса.
                per_subquery=(max_sources + 4) if quick_mode else 6,
                expand_queries=not quick_mode,
                on_progress=_on_progress,
            )
        except WebSearchError as exc:
            with suppress(Exception):
                await progress.delete()
            await message.answer(
                f"⚠ Ошибка поиска: {html.escape(str(exc))}", parse_mode="HTML"
            )
            return
        except ProviderError as exc:
            with suppress(Exception):
                await progress.delete()
            await message.answer(
                f"⚠ Ошибка модели <code>{html.escape(synth_model)}</code>: "
                f"{html.escape(str(exc))}",
                parse_mode="HTML",
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("/search deep failed")
            with suppress(Exception):
                await progress.delete()
            await message.answer(f"⚠ Ошибка: {exc}")
            return

        with suppress(Exception):
            await progress.delete()

        # 1) Сам ответ.
        await send_llm_response(message, output.answer)

        # 2) Источники + (опционально) под-запросы.
        used_label = ", ".join(dict.fromkeys(output.providers_used)) or "search"
        src_lines = [f"📎 Источники ({output.fetched_count}, via {used_label}):"]
        for n, title, url in output.cited_sources:
            short_title = title if len(title) <= 90 else title[:87] + "…"
            src_lines.append(f"[{n}] {short_title}\n    {url}")
        if not quick_mode and len(output.expanded_queries) > 1:
            src_lines.append("")
            src_lines.append("🔎 Под-запросы:")
            for q in output.expanded_queries:
                src_lines.append(f"  • {q}")
        await message.answer(
            "\n".join(src_lines), disable_web_page_preview=True
        )

        # 3) Follow-ups.
        if output.follow_ups:
            fu_lines = ["💡 <b>Похожие вопросы</b> (скопируйте в /search):"]
            for q in output.follow_ups[:3]:
                fu_lines.append(f"• <code>/search {html.escape(q)}</code>")
            await message.answer(
                "\n".join(fu_lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

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
        if explicit_provider == "anthropic":
            await message.answer(
                "Anthropic не умеет генерировать картинки. Выберите openai или acedata."
            )
            return
        found = await ctx.find_image_key(message.from_user.id, explicit_provider)
        if found is None:
            if explicit_provider:
                await message.answer(
                    f"Ключ для провайдера <code>{html.escape(explicit_provider)}</code> не найден.\n"
                    f"Админу: <code>/setkey {html.escape(explicit_provider)} ваш-ключ</code>",
                    parse_mode="HTML",
                )
                return
            await message.answer(
                "Нужен OpenAI-совместимый ключ для генерации картинок.\n"
                "Админу: <code>/setkey openai sk-...</code> или "
                "<code>/setkey acedata ваш-ключ</code>.\n"
                "Можно явно: <code>/img -p acedata -m gpt-image-1 кот в очках</code>.",
                parse_mode="HTML",
            )
            return
        key_row, used_provider = found

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
        cur_persona = get_persona(user.persona)
        persona_line = (
            f"{cur_persona.emoji} {cur_persona.name} (<code>{cur_persona.key}</code>)"
            if cur_persona
            else "—"
        )
        await message.answer(
            f"<b>Статус</b>\n"
            f"Провайдер: <code>{user.provider or '—'}</code>\n"
            f"Модель: <code>{user.model or '—'}</code>\n"
            f"Режим: <code>{user.mode}</code>\n"
            f"Стиль: {persona_line}\n"
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

    @dp.callback_query(F.data.startswith("persona:"))
    async def cb_select_persona(query: CallbackQuery) -> None:
        if not is_allowed(ctx.settings, query.from_user.id):
            await query.answer("Доступ запрещён", show_alert=True)
            return
        key = query.data.split(":", 1)[1] if query.data else ""
        uid = query.from_user.id
        if key == "off":
            await ctx.db.update_user(uid, clear_persona=True)
            text = "Сейчас: <b>обычный режим</b>"
            with suppress(Exception):
                await query.message.edit_text(
                    f"{text}\n\n{list_personas_html()}",
                    parse_mode="HTML",
                    reply_markup=persona_kb(None),
                )
            await query.answer("✓ выкл")
            return
        p = get_persona(key)
        if p is None:
            await query.answer("Неизвестный стиль", show_alert=True)
            return
        await ctx.db.update_user(uid, persona=p.key)
        text = f"Сейчас: <b>{p.emoji} {p.name}</b>"
        with suppress(Exception):
            await query.message.edit_text(
                f"{text}\n\n{list_personas_html()}",
                parse_mode="HTML",
                reply_markup=persona_kb(p.key),
            )
        await query.answer(f"✓ {p.name}")

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
                    "Примеры:\n"
                    "• <code>/search новости AI сегодня</code>\n"
                    "• <code>/search -n 15 история Linux</code> — 15 источников\n"
                    "• <code>/search -raw -n 20 python вакансии</code> — 20 сырых ссылок",
                    parse_mode="HTML",
                    reply_markup=main_menu_inline_kb(is_admin=admin),
                )
            return
        if action == "status":
            user = await ctx.db.ensure_user(uid)
            keys = await ctx.db.list_keys(uid)
            cur_persona = get_persona(user.persona)
            persona_line = (
                f"{cur_persona.emoji} {cur_persona.name}"
                if cur_persona
                else "—"
            )
            text = (
                "<b>📊 Статус</b>\n"
                f"Провайдер: <code>{user.provider or '—'}</code>\n"
                f"Модель: <code>{user.model or '—'}</code>\n"
                f"Режим: <code>{user.mode}</code>\n"
                f"Стиль: {persona_line}\n"
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
        if action == "persona":
            user = await ctx.db.ensure_user(uid)
            current = get_persona(user.persona)
            cur_line = (
                f"Сейчас: <b>{current.emoji} {current.name}</b>"
                if current
                else "Сейчас: <b>обычный режим</b>"
            )
            with suppress(Exception):
                await query.message.edit_text(
                    f"{cur_line}\n\n{list_personas_html()}",
                    parse_mode="HTML",
                    reply_markup=persona_kb(user.persona),
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
