#!/usr/bin/env bash
# Shorts Bot на бесплатном сервере Oracle Cloud (Ampere/ARM, Ubuntu 22.04 или 24.04) — работает 24/7,
# нужен в первую очередь для живого режима (/live). Видеокарты нет: клипы из эфира короткие,
# процессора хватает. Запуск (в Termius, подключившись к серверу):
#   git clone https://github.com/Valijan/shorts-bot && cd shorts-bot && bash oracle.sh
# Обновление: cd ~/shorts-bot && git pull && bash oracle.sh
set -e
cd "$(dirname "$0")"
git pull -q 2>/dev/null || true

echo "▶ Ставлю ffmpeg, Python и шрифты…"
sudo apt-get update -qq
sudo apt-get install -y -qq git ffmpeg python3-venv python3-pip fonts-dejavu-core fontconfig curl >/dev/null
if ! fc-list | grep -q "Montserrat-Black"; then
  sudo mkdir -p /usr/share/fonts/truetype/montserrat
  for w in Black ExtraBold; do
    sudo curl -fsSL -o /usr/share/fonts/truetype/montserrat/Montserrat-$w.ttf \
      "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-$w.ttf" || true
  done
  sudo fc-cache -f >/dev/null 2>&1 || true
fi

echo "▶ Ставлю Python-библиотеки (3–6 минут)…"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
# OpenCV 5 без детектора лиц — ставим 4.x
.venv/bin/pip uninstall -y -q opencv-python opencv-contrib-python opencv-python-headless >/dev/null 2>&1 || true
.venv/bin/pip install -q "opencv-python-headless>=4.9,<5"

if [ ! -f .env ]; then
  echo "▶ Настройка бота"
  echo "  Совет: заведи в @BotFather ОТДЕЛЬНОГО бота для Oracle. Один токен нельзя запускать"
  echo "  на двух серверах сразу (RunPod и Oracle будут мешать друг другу)."
  read -rp "Токен бота: " T
  read -rp "Твой Telegram ID (Enter — пропустить): " U
  read -rp "Ключ Claude API (Enter — пропустить): " K
  cat > .env <<ENV
BOT_TOKEN=$T
ALLOWED_USERS=$U
ANTHROPIC_API_KEY=$K
CLAUDE_MODEL=claude-sonnet-5-5
# 2 ядра ARM: small — баланс; если клипы монтируются долго — поставь base
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE=int8
DATA_DIR=$PWD/data
SHOW_TITLE=1
WATERMARK=
# Живой режим
LIVE_QUALITY=1080p60,1080p,best
LIVE_PRE=20
LIVE_CHANNELS=2
ENV
fi
mkdir -p data

echo "▶ Делаю автозапуск (бот поднимется сам после перезагрузки и при сбое)…"
sudo tee /etc/systemd/system/shorts-bot.service >/dev/null <<UNIT
[Unit]
Description=Shorts Bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/.venv/bin/python -m app.bot
Environment=PATH=$PWD/.venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable -q shorts-bot
sudo systemctl restart shorts-bot
sleep 5
sudo journalctl -u shorts-bot -n 5 --no-pager || true
echo "✅ Бот запущен! Напиши ему /start, потом /live <канал>."
echo "Логи: sudo journalctl -u shorts-bot -f   ·   Перезапуск: sudo systemctl restart shorts-bot"
