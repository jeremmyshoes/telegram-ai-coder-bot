# Telegram AI Coder Bot

Telegram-бот — аналог [opencode](https://opencode.ai) и Cursor: универсальная
обёртка над любым LLM-провайдером, с автономным **agent-режимом** (модель сама
выполняет shell-команды, читает/правит файлы) и обычным **chat-режимом** для
быстрого тестирования моделей. Для каждого пользователя — свой sandbox в
Docker, свой набор API-ключей и своя история.

> Идеален, чтобы быстро прогнать новую модель: вставил API-ключ → выбрал
> модель → пишешь как в Cursor.

## Возможности

- **Множество провайдеров через единый интерфейс**:
  OpenAI, OpenRouter (300+ моделей), Anthropic Claude, DeepSeek, Groq,
  xAI Grok, Mistral, Together AI, Google Gemini (через OpenAI-совместимый
  endpoint), произвольный self-hosted (Ollama / LM Studio / vLLM) через
  `provider=custom` + `base_url`.
- **Agent-режим с tool-calling**:
  - `bash` — выполнение команд в Docker-sandbox
  - `read_file`, `write_file`, `edit_file`, `ls`
  - Состояние рабочей папки сохраняется между сообщениями.
- **Chat-режим** — без инструментов, чистый диалог.
- **Шифрование ключей**: API-ключи шифруются Fernet и хранятся в SQLite.
- **Whitelist пользователей** через `ALLOWED_USER_IDS`.
- **Загрузка файлов**: пришлите документ — он окажется в рабочей папке и будет
  доступен агенту.
- **UX-интерфейс**:
  - Меню Telegram при нажатии `/` (через `set_my_commands`).
  - Reply-клавиатура внизу: 💬 Чат, 🎨 Картинка, ⚙️ Настройки, 📊 Статус, ℹ️ Помощь.
  - Inline-кнопки для выбора провайдера и модели — никаких ID руками.
  - `/menu` — открыть главное меню, `/hidekb` — спрятать клавиатуру.

## Команды

| Команда | Описание |
|---|---|
| `/chat <запрос>` | One-shot ответ от текущей модели (без истории, без tools) |
| `/img <промпт>` | Сгенерировать картинку через OpenAI Images API |
| `/start`, `/help` | Справка |
| `/providers` | Список встроенных провайдеров |
| `/provider <id>` | Выбрать провайдера (`openai`, `openrouter`, …) |
| `/setkey <provider> <api_key> [base_url]` | Сохранить ключ (шифруется) |
| `/keys` / `/delkey <provider>` | Просмотр / удаление ключей |
| `/model <model_id>` | Задать модель |
| `/models` | Подсказки + live-список моделей провайдера |
| `/mode agent\|chat` | Режим длинного диалога (agent с tools / chat без) |
| `/status` | Текущие настройки |
| `/reset` | Очистить историю |
| `/workdir`, `/clearwd` | Просмотр/очистка sandbox-папки |

`/img` принимает флаги `-s WxH` (размер) и `-q hd|standard|low|medium|high|auto`
(качество, для dall-e-3 / gpt-image-1). Примеры: `/img -s 1792x1024 закат`,
`/img -q hd кот в очках`. Модель и дефолтный размер настраиваются через
`IMAGE_MODEL` (по умолчанию `dall-e-3`) и `IMAGE_SIZE` в `.env`.

## Где взять API-ключ (включая бесплатные варианты)

**Бесплатно или с щедрым free-tier** (требуется ваша регистрация):

| Провайдер | Бот-id | Бесплатно | Где получить |
|---|---|---|---|
| Cerebras (Llama 3.3-70b, Qwen, gpt-oss) — самый быстрый | `cerebras` | Free API из playground | https://cloud.cerebras.ai |
| SambaNova Cloud (DeepSeek-R1, Llama-4) | `sambanova` | $5 стартовых + free-tier с rate-limit | https://cloud.sambanova.ai/apis |
| HuggingFace Inference Router | `huggingface` | Free-tier на месяц | https://huggingface.co/settings/tokens |
| Google Gemini (через AI Studio) | `google` | Бесплатный tier | https://aistudio.google.com/app/apikey |
| Groq (Llama, Mixtral) | `groq` | Free-tier | https://console.groq.com/keys |
| Mistral La Plateforme | `mistral` | Бесплатный tier | https://console.mistral.ai/api-keys/ |
| OpenRouter (300+ моделей, есть `:free`) | `openrouter` | `:free` варианты бесплатны | https://openrouter.ai/keys |

**Платные / агрегаторы**:

| Провайдер | Бот-id | Где получить |
|---|---|---|
| OpenAI (GPT-4o, GPT-5, dall-e-3) | `openai` | https://platform.openai.com/api-keys |
| Anthropic Claude | `anthropic` | https://console.anthropic.com/settings/keys |
| DeepSeek | `deepseek` | https://platform.deepseek.com/api_keys |
| xAI Grok | `xai` | https://console.x.ai |
| Together AI | `together` | https://api.together.ai/settings/api-keys |
| AceData Cloud (агрегатор GPT/Claude/Gemini) | `acedata` | https://platform.acedata.cloud |

> **Важно:** ключи нужно регистрировать самому — это ваши учётные записи.
> «Анонимных бесплатных» ключей не существует, а сайты, раздающие чужие
> рабочие ключи (g4f-style прокси) — нелегальны и работают нестабильно;
> в этот бот они не подключаются.

После регистрации:
```
/setkey cerebras csk-...
/provider cerebras
/model llama-3.3-70b
/chat Привет, как дела?
```

## Быстрый старт (локально)

Требования: Python 3.11+, опционально Docker (для sandbox).

```bash
git clone https://github.com/jeremmyshoes/telegram-ai-coder-bot.git
cd telegram-ai-coder-bot

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env

# 1. Получите токен у @BotFather и впишите в TELEGRAM_BOT_TOKEN
# 2. Сгенерируйте ENCRYPTION_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# вставьте результат в ENCRYPTION_KEY
# 3. Узнайте свой Telegram user_id (например через @userinfobot)
#    и впишите в ALLOWED_USER_IDS=123456789  (личный бот → только вы)

python -m bot
```

Затем в Telegram:

```
/start
/setkey openai sk-proj-...
/provider openai
/model gpt-4o
Привет! Создай tmp.py с print("hello") и запусти его.
```

В agent-режиме модель сама вызовет `write_file` и `bash` и пришлёт результат.

## Деплой

### Вариант A. VPS + Docker Compose (рекомендуется)

Подойдёт любой VPS с Ubuntu 22.04/24.04 (Hetzner CX11/CX21, Timeweb, Selectel,
DigitalOcean, Vultr — от 200 ₽/$3 в месяц).

```bash
# на сервере
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker

git clone https://github.com/jeremmyshoes/telegram-ai-coder-bot.git /srv/telegram-ai-coder-bot
cd /srv/telegram-ai-coder-bot
cp .env.example .env

# Заполните .env, обязательно установите:
#   TELEGRAM_BOT_TOKEN=...
#   ENCRYPTION_KEY=...   (Fernet)
#   ALLOWED_USER_IDS=ваш_telegram_id
#   HOST_DATA_DIR=/srv/telegram-ai-coder-bot/data

# Подтянуть sandbox-образ заранее (ускоряет первый вызов bash):
docker pull python:3.12-slim

# Запустить
docker compose up -d --build
docker compose logs -f bot
```

Бот переживёт перезагрузку (`restart: unless-stopped`). Все данные — в
`./data/` (БД и рабочие папки пользователей). Бэкап = бэкап этой папки.

> **Важно**: бот пробрасывает в контейнер `/var/run/docker.sock`, чтобы поднимать
> sandbox-контейнеры на хосте. Это даёт ему права root на хосте — поэтому
> используйте отдельный VPS, никогда не давайте доступ посторонним и
> обязательно настройте `ALLOWED_USER_IDS`.

### Вариант B. VPS + systemd (без Docker для sandbox)

Если не хочется ставить Docker и достаточно простой изоляции через `subprocess`:

```bash
sudo useradd -m -s /bin/bash botuser
sudo -u botuser git clone https://github.com/jeremmyshoes/telegram-ai-coder-bot.git /opt/telegram-ai-coder-bot
cd /opt/telegram-ai-coder-bot

sudo -u botuser python3 -m venv .venv
sudo -u botuser .venv/bin/pip install -r requirements.txt
sudo -u botuser cp .env.example .env
sudo -u botuser nano .env   # заполнить (DISABLE_DOCKER_SANDBOX=1)

sudo cp deploy/systemd.service /etc/systemd/system/telegram-ai-coder-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-ai-coder-bot
sudo journalctl -fu telegram-ai-coder-bot
```

В этом режиме команды выполняются в `bash` от имени `botuser` без изоляции.
Если на сервере ничего важного нет — нормально, но безопаснее вариант A.

### Вариант C. Railway / Render / Fly.io (без Docker-sandbox)

PaaS-платформы не дают доступа к docker daemon, поэтому sandbox упадёт в
fallback на `subprocess`. Это безопасно (внутри контейнера платформы), но менее
изолированно. Установите `DISABLE_DOCKER_SANDBOX=1`.

**Railway** (просто):
1. Создайте проект из этого репозитория, Railway сам соберёт Dockerfile.
2. В Variables добавьте все переменные из `.env.example`.
3. Создайте Volume и подмонтируйте на `/app/data`.

**Fly.io**:
```bash
fly launch --copy-config --no-deploy
fly volumes create bot_data --size 1
fly secrets set TELEGRAM_BOT_TOKEN=... ENCRYPTION_KEY=... ALLOWED_USER_IDS=...
fly deploy
```

(см. `deploy/fly.toml` как пример)

**Render**:
- New → Web Service → Docker.
- Health check можно отключить (бот polling, не http).
- Persistent Disk на `/app/data`.

### Вариант D. Локальный запуск (для разработки)

```bash
python -m bot
```

## Как настроить под себя

### Получить токен бота
1. В Telegram напишите [@BotFather](https://t.me/BotFather)
2. `/newbot` → имя → username (`*_bot`)
3. Скопируйте token → `TELEGRAM_BOT_TOKEN` в `.env`
4. Дополнительно: `/setprivacy` → Disable (необязательно), `/setcommands`:
   ```
   start - Старт
   help - Справка
   provider - Выбрать провайдера
   model - Выбрать модель
   models - Список моделей
   setkey - Сохранить API ключ
   keys - Мои ключи
   mode - Режим agent/chat
   status - Текущие настройки
   reset - Сбросить историю
   workdir - Рабочая папка
   ```

### Узнать свой user_id
- Напишите [@userinfobot](https://t.me/userinfobot) → он ответит вашим id.
- Впишите в `.env`: `ALLOWED_USER_IDS=123456789` (через запятую — несколько).

### Получить API-ключи

| Провайдер | Где взять ключ |
|---|---|
| OpenAI | https://platform.openai.com/api-keys |
| OpenRouter | https://openrouter.ai/keys (один ключ → 300+ моделей) |
| Anthropic | https://console.anthropic.com/settings/keys |
| DeepSeek | https://platform.deepseek.com/api_keys |
| Groq | https://console.groq.com/keys |
| xAI | https://console.x.ai |
| Mistral | https://console.mistral.ai/api-keys |
| Together | https://api.together.ai/settings/api-keys |
| Google AI Studio | https://aistudio.google.com/app/apikey |

### Использование локальной модели (Ollama / LM Studio / vLLM)

```
/setkey custom dummy http://192.168.1.10:11434/v1
/provider custom
/model llama3.1:70b
```

`api_key` для локальных серверов часто игнорируется — передайте любую строку.

## Архитектура

```
bot/
├── __main__.py        Точка входа (polling, запуск)
├── config.py          Settings из .env
├── crypto.py          Fernet-шифрование ключей
├── db.py              SQLite (aiosqlite): users, api_keys, messages
├── agent.py           Agent loop: до N итераций tool-calling
├── providers/
│   ├── base.py        Унифицированный Message / ToolCall / LLMProvider
│   ├── openai_compat.py   OpenAI/OpenRouter/Groq/DeepSeek/Mistral/xAI/...
│   ├── anthropic.py   Native Anthropic (адаптер OpenAI tool format → Claude)
│   └── registry.py    Реестр пресетов (id → base_url)
├── tools/
│   ├── sandbox.py     DockerSandbox / SubprocessSandbox
│   └── registry.py    bash, read_file, write_file, edit_file, ls
└── handlers/
    ├── commands.py    /start, /setkey, /model, ...
    ├── chat.py        Текстовые сообщения → Agent
    └── files.py       Загруженные файлы → workspaces/<user_id>/
```

## Безопасность

- API-ключи шифруются Fernet (`ENCRYPTION_KEY`) и хранятся в SQLite.
  Сообщение пользователя с ключом удаляется из чата сразу после `/setkey`.
- При смене `ENCRYPTION_KEY` все старые ключи становятся нечитаемыми → их
  придётся ввести заново. Сохраните ключ в надёжном месте.
- `ALLOWED_USER_IDS` — единственная защита от посторонних. Не оставляйте пустым
  на публичном сервере.
- `bash` инструмент даёт модели возможность выполнять произвольный код. В
  Docker-режиме — внутри одноразового контейнера с лимитами CPU/RAM/сети, но
  всё равно: не давайте доступ людям, которым не доверяете.
- На VPS используйте отдельного пользователя/VM, бэкапьте `data/`.

## FAQ

**Можно ли использовать одного бота на нескольких людей?**
Технически да — у каждого user_id свои ключи, история и workdir. Просто
добавьте всех в `ALLOWED_USER_IDS`. Но имейте в виду пункты безопасности выше.

**Как добавить новый провайдер?**
Если он OpenAI-совместимый — допишите в `bot/providers/registry.py` запись
`PROVIDER_PRESETS[...] = ProviderPreset(...)` с `base_url`. Если протокол
свой — реализуйте класс по образцу `AnthropicProvider`.

**Поддерживается ли streaming?**
Сейчас — нет. Прогресс показывается через апдейты редактируемого сообщения с
логом инструментов. PR welcome.

**Что с лимитами Telegram?**
Сообщения режутся на части по 4000 символов. Очень длинные ответы можно
прислать как файл — допишите вспомогательный helper при необходимости.

## Разработка

```bash
pip install -r requirements.txt
pip install ruff
ruff check bot
python -m compileall bot
```

## Лицензия

MIT.
