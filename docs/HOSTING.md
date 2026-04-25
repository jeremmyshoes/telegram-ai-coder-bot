# Подробная инструкция по хостингу

Этот документ — пошаговое руководство для самых популярных вариантов размещения
бота. Если просто хочется быстро запустить — см. **README.md** → *Быстрый старт*.

## Сравнение вариантов

| Вариант | Цена/мес | Изоляция bash | Сложность | Когда выбрать |
|---|---|---|---|---|
| VPS + Docker Compose | $3–5 | Docker container (полная) | ★★ | Личное использование, нужен sandbox |
| VPS + systemd | $3–5 | Только разные пользователи | ★★ | Минимализм, нет Docker |
| Railway | $5 | subprocess (общая VM) | ★ | Быстро без терминала |
| Fly.io | $0–2 | subprocess | ★★ | Free tier, многорегиональность |
| Render | $7 | subprocess | ★ | Простой UI |
| Свой компьютер | бесплатно | по выбору | ★ | Тестирование, не для прода |

---

## 1. Hetzner Cloud (или любой VPS) + Docker Compose

Рекомендованный вариант для личного бота.

### 1.1 Создать сервер

[Hetzner Cloud](https://www.hetzner.com/cloud) — самый дешёвый европейский
провайдер. Подойдёт CX22 (≈ €4.5/мес, 2vCPU/4ГБ RAM/40ГБ SSD).

Альтернативы: Selectel, Timeweb, DigitalOcean, Vultr, Linode.

При создании:
- ОС: **Ubuntu 24.04 LTS**
- Тип: 2 vCPU / 4 ГБ RAM
- SSH ключ: добавьте свой

### 1.2 Подключиться

```bash
ssh root@<IP>
```

Создайте отдельного пользователя для безопасности:

```bash
adduser deploy
usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
ufw allow OpenSSH
ufw enable
```

Дальше всё под `deploy`.

### 1.3 Поставить Docker

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
docker run --rm hello-world
```

### 1.4 Развернуть бота

```bash
sudo mkdir -p /srv/telegram-ai-coder-bot
sudo chown $USER:$USER /srv/telegram-ai-coder-bot
git clone https://github.com/jeremmyshoes/telegram-ai-coder-bot.git /srv/telegram-ai-coder-bot
cd /srv/telegram-ai-coder-bot
cp .env.example .env

# Обязательные переменные:
nano .env
# TELEGRAM_BOT_TOKEN=...
# ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# ALLOWED_USER_IDS=ваш_user_id
# HOST_DATA_DIR=/srv/telegram-ai-coder-bot/data
```

ENCRYPTION_KEY можно сгенерировать прямо в shell:
```bash
docker run --rm python:3.12-slim python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null
```
(если не хочется ставить Python на хост — он не нужен).

```bash
mkdir -p data
docker pull python:3.12-slim   # sandbox-образ
docker compose up -d --build
docker compose logs -f bot
```

В Telegram:
- Найдите своего бота по username из BotFather.
- `/start` → должна прийти справка.
- `/setkey openai sk-...` → `/provider openai` → `/model gpt-4o` → пишите.

### 1.5 Обновление

```bash
cd /srv/telegram-ai-coder-bot
git pull
docker compose up -d --build
```

### 1.6 Бэкап

```bash
tar czf bot-backup-$(date +%F).tgz data/
# отправьте куда-нибудь, напр. в S3 / на свой ноут
```

---

## 2. VPS + systemd (без Docker для sandbox)

Если категорически не хочется ставить Docker:

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo useradd -m -s /bin/bash botuser
sudo -u botuser bash <<'EOF'
git clone https://github.com/jeremmyshoes/telegram-ai-coder-bot.git ~/bot
cd ~/bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
sed -i 's/^DISABLE_DOCKER_SANDBOX=$/DISABLE_DOCKER_SANDBOX=1/' .env
EOF

# заполните .env под пользователем botuser
sudo -u botuser nano /home/botuser/bot/.env

# поправьте пути в systemd unit под /home/botuser/bot
sudo cp /home/botuser/bot/deploy/systemd.service /etc/systemd/system/telegram-ai-coder-bot.service
sudo sed -i 's|/opt/telegram-ai-coder-bot|/home/botuser/bot|g' /etc/systemd/system/telegram-ai-coder-bot.service

sudo systemctl daemon-reload
sudo systemctl enable --now telegram-ai-coder-bot
sudo journalctl -fu telegram-ai-coder-bot
```

`DISABLE_DOCKER_SANDBOX=1` отключает попытку запустить sandbox в Docker и
переключает на `SubprocessSandbox` — bash от имени `botuser`.

---

## 3. Railway (быстрый деплой без терминала)

1. https://railway.app → New Project → Deploy from GitHub Repo.
2. Выберите `jeremmyshoes/telegram-ai-coder-bot`.
3. Railway автоматически найдёт `Dockerfile` и соберёт.
4. **Variables** (settings → variables):
   - `TELEGRAM_BOT_TOKEN`
   - `ENCRYPTION_KEY`
   - `ALLOWED_USER_IDS`
   - `DISABLE_DOCKER_SANDBOX=1`
   - `DATA_DIR=/data`
   - `DB_PATH=/data/bot.db`
   - `WORKSPACES_DIR=/data/workspaces`
5. Storage → Add Volume → Mount path: `/data`, Size: 1 ГБ.
6. Deploy.

Логи — во вкладке Logs. Цена: ≈ $5/мес после free tier.

## 4. Fly.io

```bash
curl -L https://fly.io/install.sh | sh
fly auth signup   # или login
cd telegram-ai-coder-bot
fly launch --copy-config --no-deploy
fly volumes create bot_data --size 1 --region fra
fly secrets set \
  TELEGRAM_BOT_TOKEN=... \
  ENCRYPTION_KEY=... \
  ALLOWED_USER_IDS=...
fly deploy
fly logs
```

(см. `deploy/fly.toml` как стартовую точку)

---

## 5. На своём компьютере (для тестов)

Если просто хочется попробовать — никакого хостинга не нужно:

```bash
git clone https://github.com/jeremmyshoes/telegram-ai-coder-bot.git
cd telegram-ai-coder-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните .env (TELEGRAM_BOT_TOKEN, ENCRYPTION_KEY, ALLOWED_USER_IDS, HOST_DATA_DIR=...)
python -m bot
```

Бот работает пока запущен процесс.

Чтобы фоном через `screen` / `tmux`:
```bash
screen -S bot
python -m bot
# Ctrl+A, D — отсоединиться. screen -r bot — вернуться.
```

---

## Что делать, если что-то не работает

- **Бот не отвечает на /start**:
  - Проверьте `TELEGRAM_BOT_TOKEN` (без пробелов).
  - Проверьте, что ваш user_id в `ALLOWED_USER_IDS`. Без этого — «Доступ запрещён».
- **`Не настроены провайдер/модель/ключ`** — выполните по очереди:
  - `/setkey <provider> <api_key>`, `/provider <id>`, `/model <id>`.
- **`Docker не найден`** в логах — установите Docker или поставьте
  `DISABLE_DOCKER_SANDBOX=1`.
- **Sandbox-команды зависают** — увеличьте `SANDBOX_TIMEOUT`, проверьте, что
  образ `python:3.12-slim` подтянут (`docker pull python:3.12-slim`).
- **`docker: permission denied while trying to connect to … docker.sock`** —
  ваш пользователь не в группе `docker`, либо в compose неправильный путь к
  сокету. Исправляется `sudo usermod -aG docker $USER && newgrp docker`.
- **Ошибка `cannot resolve …`** в sandbox — у `SANDBOX_NETWORK` стоит `none`,
  поставьте `bridge`.
