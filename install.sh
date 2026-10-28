#!/usr/bin/env bash
# Установка Shorts Bot одной командой (Ubuntu, x86 или ARM)
set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "▶ Ставлю Docker…"
  curl -fsSL https://get.docker.com | sudo sh
fi

if [ ! -f .env ]; then
  echo "▶ Настройка бота"
  read -rp "Токен бота (от @BotFather): " T
  read -rp "API ID (my.telegram.org): " I
  read -rp "API Hash (my.telegram.org): " H
  read -rp "Твой Telegram ID (Enter — пропустить): " U
  read -rp "Ключ Claude API (Enter — пропустить): " K
  cp .env.example .env
  sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$T|; s|^TELEGRAM_API_ID=.*|TELEGRAM_API_ID=$I|; \
s|^TELEGRAM_API_HASH=.*|TELEGRAM_API_HASH=$H|; s|^ALLOWED_USERS=.*|ALLOWED_USERS=$U|; \
s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$K|" .env
  echo "▶ Переключаю бота на локальный сервер Telegram…"
  curl -s "https://api.telegram.org/bot$T/logOut" >/dev/null || true
fi

mkdir -p data
echo "▶ Собираю и запускаю (5–10 минут)…"
sudo docker compose up -d --build
echo "✅ Готово! Открой бота в Telegram и напиши /start"
echo "Логи: sudo docker compose logs -f bot"
