#!/usr/bin/env bash
# Запуск Shorts Bot на RunPod (GPU). Использование:
#   cd /workspace && git clone https://github.com/Valijan/shorts-bot && cd shorts-bot && bash runpod.sh
set -e
mkdir -p /workspace
if [ -d "$(dirname "$0")/app" ]; then cd "$(dirname "$0")"; else
  cd /workspace && { [ -d shorts-bot ] || git clone -q https://github.com/Valijan/shorts-bot; } && cd shorts-bot
fi
git pull -q 2>/dev/null || true

echo "▶ Ставлю ffmpeg и шрифты…"
apt-get update -qq && apt-get install -y -qq ffmpeg fonts-dejavu-core fontconfig >/dev/null

echo "▶ Ставлю Python-библиотеки (1–3 минуты)…"
pip install -q --root-user-action=ignore -r requirements.txt nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
# OpenCV 5 без детектора лиц — ставим 4.x
pip uninstall -y -q opencv-python opencv-contrib-python opencv-python-headless >/dev/null 2>&1 || true
pip install -q --root-user-action=ignore "opencv-python-headless>=4.9,<5"
export LD_LIBRARY_PATH="$(python3 -c 'import nvidia.cublas.lib as a, nvidia.cudnn.lib as b; print(a.__path__[0]+":"+b.__path__[0])'):${LD_LIBRARY_PATH}"

if [ ! -f .env ]; then
  # Токен и ключи можно задать в RunPod: Edit Pod → Environment Variables
  T="${BOT_TOKEN:-}"; U="${ALLOWED_USERS:-}"; K="${ANTHROPIC_API_KEY:-}"
  if [ -z "$T" ]; then
    echo "▶ Настройка бота"
    read -rp "Токен бота (от @BotFather): " T
    read -rp "Твой Telegram ID (Enter — пропустить): " U
    read -rp "Ключ Claude API (Enter — пропустить): " K
  fi
  cat > .env <<ENV
BOT_TOKEN=$T
ALLOWED_USERS=$U
ANTHROPIC_API_KEY=$K
CLAUDE_MODEL=claude-sonnet-5-5
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE=float16
DATA_DIR=/workspace/data
SHOW_TITLE=1
WATERMARK=
ENV
fi

# ---- Свой сервер Bot API: файлы до 2 ГБ вместо 20 МБ ----
# Нужны TG_API_ID и TG_API_HASH (https://my.telegram.org → API development tools)
if [ -n "${TG_API_ID:-}" ] && [ -n "${TG_API_HASH:-}" ]; then
  BIN=/workspace/bin/telegram-bot-api
  if [ ! -x "$BIN" ]; then
    echo "▶ Собираю сервер Bot API (один раз, ~5–10 минут)…"
    apt-get install -y -qq make git zlib1g-dev libssl-dev gperf cmake g++ >/dev/null
    rm -rf /tmp/tba && git clone -q --recursive --depth 1 https://github.com/tdlib/telegram-bot-api /tmp/tba
    mkdir -p /tmp/tba/build /workspace/bin && cd /tmp/tba/build
    cmake -DCMAKE_BUILD_TYPE=Release .. >/dev/null && cmake --build . --target telegram-bot-api -j"$(nproc)" >/dev/null
    cp telegram-bot-api "$BIN" && cd - >/dev/null && rm -rf /tmp/tba
  fi
  TOKEN="${BOT_TOKEN:-$(grep -oP '^BOT_TOKEN=\K.*' .env 2>/dev/null)}"
  if [ ! -f /workspace/.tg_logged_out ] && [ -n "$TOKEN" ]; then
    # один раз «выписываем» бота из облачного API, чтобы он работал через свой сервер
    curl -s "https://api.telegram.org/bot${TOKEN}/logOut" >/dev/null && touch /workspace/.tg_logged_out
  fi
  mkdir -p /workspace/tba-data
  pkill -f telegram-bot-api 2>/dev/null || true
  nohup "$BIN" --local --api-id="$TG_API_ID" --api-hash="$TG_API_HASH" \
    --dir=/workspace/tba-data --http-port=8081 > /workspace/tba.log 2>&1 &
  sleep 3
  grep -q '^BOT_API_URL=' .env 2>/dev/null && sed -i 's|^BOT_API_URL=.*|BOT_API_URL=http://127.0.0.1:8081|' .env \
    || echo "BOT_API_URL=http://127.0.0.1:8081" >> .env
  echo "✅ Свой Bot API запущен — бот принимает файлы до 2 ГБ"
fi

pkill -f "python3 -m app.bot" 2>/dev/null || true
nohup python3 -m app.bot > /workspace/bot.log 2>&1 &
sleep 5
tail -n 5 /workspace/bot.log
echo "✅ Бот запущен! Напиши ему /start в Telegram."
echo "Логи: tail -f /workspace/bot.log   ·   Не забудь остановить pod, когда закончишь."
