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

pkill -f "python3 -m app.bot" 2>/dev/null || true
nohup python3 -m app.bot > /workspace/bot.log 2>&1 &
sleep 5
tail -n 5 /workspace/bot.log
echo "✅ Бот запущен! Напиши ему /start в Telegram."
echo "Логи: tail -f /workspace/bot.log   ·   Не забудь остановить pod, когда закончишь."
