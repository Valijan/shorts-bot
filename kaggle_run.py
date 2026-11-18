"""Запуск Shorts Bot в ноутбуке Kaggle (бесплатная видеокарта T4, до 12 ч за сессию).

В ноутбуке Kaggle (Settings: Accelerator = GPU T4, Internet = On) одна ячейка:

    !git clone -q https://github.com/Valijan/shorts-bot /kaggle/working/shorts-bot || git -C /kaggle/working/shorts-bot pull -q
    !python /kaggle/working/shorts-bot/kaggle_run.py

Токен бота берётся из Kaggle Secrets (Add-ons → Secrets): BOT_TOKEN, по желанию ALLOWED_USERS
и ANTHROPIC_API_KEY. Чтобы бот работал без открытой вкладки — «Save Version» → «Save & Run All».
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path("/tmp/shorts-data")          # не в /kaggle/working, чтобы клипы не попадали в вывод ноутбука
LIMIT_H = float(os.getenv("KAGGLE_HOURS", "11.7"))   # Kaggle гасит сессию через 12 ч


def sh(cmd: str):
    print("▶", cmd[:120], flush=True)
    subprocess.run(cmd, shell=True, check=False)


def secret(name: str) -> str:
    if os.getenv(name):
        return os.environ[name]
    try:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret(name) or ""
    except Exception:
        return ""


def main():
    token = secret("BOT_TOKEN")
    if not token:
        sys.exit("❌ Нет BOT_TOKEN. Add-ons → Secrets → Add secret: BOT_TOKEN = токен от @BotFather "
                 "(и включи галочку у секрета для этого ноутбука).")
    sh("apt-get -qq update >/dev/null 2>&1; apt-get -qq install -y ffmpeg fonts-dejavu-core >/dev/null 2>&1")
    fonts = Path("/usr/share/fonts/truetype/montserrat")
    if not (fonts / "Montserrat-Black.ttf").exists():
        fonts.mkdir(parents=True, exist_ok=True)
        for w in ("Black", "ExtraBold"):
            sh(f"curl -fsSL -o {fonts}/Montserrat-{w}.ttf "
               f"https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-{w}.ttf")
        sh("fc-cache -f >/dev/null 2>&1")
    sh(f"pip install -q -r {ROOT}/requirements.txt nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*' "
       "'opencv-python-headless>=4.9,<5'")

    import importlib
    importlib.invalidate_caches()
    import nvidia.cublas.lib as a
    import nvidia.cudnn.lib as b
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{a.__path__[0]}:{b.__path__[0]}:" + env.get("LD_LIBRARY_PATH", "")
    env.update(BOT_TOKEN=token, ALLOWED_USERS=secret("ALLOWED_USERS"),
               ANTHROPIC_API_KEY=secret("ANTHROPIC_API_KEY"),
               WHISPER_MODEL=os.getenv("WHISPER_MODEL", "large-v3"), WHISPER_DEVICE="cuda",
               WHISPER_COMPUTE="float16", DATA_DIR=str(DATA), BOT_API_URL="")
    DATA.mkdir(parents=True, exist_ok=True)

    stop_at = time.time() + LIMIT_H * 3600
    print(f"✅ Запускаю бота. Работает, пока идёт эта сессия (до {LIMIT_H:g} ч).", flush=True)
    while time.time() < stop_at:   # упал — поднимаем снова
        p = subprocess.Popen([sys.executable, "-m", "app.bot"], cwd=ROOT, env=env)
        try:
            p.wait(timeout=max(1, stop_at - time.time()))
        except subprocess.TimeoutExpired:
            p.terminate()
            break
        print(f"⚠️ Бот завершился с кодом {p.returncode}, перезапуск через 10 с", flush=True)
        time.sleep(10)
    print("⏹ Время сессии Kaggle вышло — запусти ноутбук снова.", flush=True)


if __name__ == "__main__":
    main()
