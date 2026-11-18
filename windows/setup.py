"""Установка Shorts Bot на домашний ПК (Windows). Запускается из install.bat.

Что делает: скачивает бота, ставит библиотеки в отдельное окружение, ffmpeg и шрифты,
спрашивает токен бота, включает автозапуск вместе с Windows и сразу запускает бота.
Повторный запуск безопасен: обновит бота и ничего не сломает.
"""
import io
import os
import re
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

DIR = Path.home() / "shorts-bot"
REPO_ZIP = "https://codeload.github.com/Valijan/shorts-bot/zip/refs/heads/main"
FFMPEG_ZIPS = ["https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
               "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"]
FONT = "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-{}.ttf"
KEEP = {".env", "data", "venv", "ffmpeg", "fonts", "bot.log", ".git"}
OWNER = "6749215117"


def step(t):
    print(f"\n▶ {t}", flush=True)


def get(url, timeout=600):
    return urllib.request.urlopen(url, timeout=timeout).read()


def main():
    DIR.mkdir(exist_ok=True)
    step("Скачиваю бота с GitHub…")
    with zipfile.ZipFile(io.BytesIO(get(REPO_ZIP))) as z:
        top = z.namelist()[0].split("/")[0]
        for name in z.namelist():
            rel = name[len(top) + 1:]
            if not rel or rel.split("/")[0] in KEEP:
                continue
            dst = DIR / rel
            if name.endswith("/"):
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(z.read(name))

    py = DIR / "venv" / "Scripts" / "python.exe"
    if not py.exists():
        step("Создаю окружение Python…")
        subprocess.run([sys.executable, "-m", "venv", str(DIR / "venv")], check=True)
    step("Ставлю библиотеки (5–10 минут, нужен интернет)…")
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"])
    subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(DIR / "requirements.txt")], check=True)

    ff = DIR / "ffmpeg" / "bin"
    if not (ff / "ffmpeg.exe").exists():
        step("Скачиваю ffmpeg (~100 МБ)…")
        ff.mkdir(parents=True, exist_ok=True)
        for url in FFMPEG_ZIPS:
            try:
                data = get(url, 900)
            except Exception as e:
                print("не скачалось, пробую другой источник:", e)
                continue
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for name in z.namelist():
                    if "/bin/" in name and name.endswith(".exe"):
                        (ff / Path(name).name).write_bytes(z.read(name))
            break
        if not (ff / "ffmpeg.exe").exists():
            raise RuntimeError("не удалось скачать ffmpeg")

    fonts = DIR / "fonts"
    fonts.mkdir(exist_ok=True)
    for w in ("Black", "ExtraBold"):
        f = fonts / f"Montserrat-{w}.ttf"
        if not f.exists():
            try:
                f.write_bytes(get(FONT.format(w), 60))
            except Exception as e:
                print("шрифт не скачался:", e)

    env = DIR / ".env"
    pre = Path.home() / ".config" / "shorts-bot.env"   # настройки, заранее положенные Claude
    if not env.exists() and pre.exists():
        env.write_bytes(pre.read_bytes())
        pre.unlink()
    quiet = env.exists()   # настройки уже лежат (их положил Claude) — ничего не спрашиваем
    if not env.exists():
        step("Настройка бота")
        token = ""
        while not re.fullmatch(r"\d{6,}:[\w-]{30,}", token):
            token = input("Вставь токен бота от @BotFather и нажми Enter: ").strip()
        users = input(f"Telegram ID владельца [{OWNER}] (Enter — оставить): ").strip() or OWNER
        env.write_text("\n".join([
            f"BOT_TOKEN={token}", f"ALLOWED_USERS={users}", "ANTHROPIC_API_KEY=",
            "WHISPER_MODEL=small", "WHISPER_DEVICE=cpu", "WHISPER_COMPUTE=int8",
            f"DATA_DIR={(DIR / 'data').as_posix()}", f"FONTS_DIR={fonts.as_posix()}",
            "LIVE_QUALITY=720p60,720p,best", "SHOW_TITLE=1", "WATERMARK=", ""]), encoding="utf-8")

    start = DIR / "start_bot.bat"
    start.write_text("\r\n".join([
        "@echo off", "chcp 65001 >nul", 'cd /d "%~dp0"',
        'set "PATH=%~dp0ffmpeg\\bin;%~dp0venv\\Scripts;%PATH%"',
        "set SB_SUPERVISED=1", 'set "SB_LOG=%~dp0bot.log"', "set PYTHONIOENCODING=utf-8", "set PYTHONUTF8=1",
        ":loop",
        '"%~dp0venv\\Scripts\\python.exe" -m app.selfupdate >> "%~dp0bot.log" 2>&1',
        '"%~dp0venv\\Scripts\\python.exe" -m app.bot >> "%~dp0bot.log" 2>&1',
        "if %errorlevel%==5 goto :eof",
        "timeout /t 10 /nobreak >nul", "goto loop", ""]), encoding="utf-8")

    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    vbs = startup / "ShortsBot.vbs"
    vbs.write_text(f'CreateObject("WScript.Shell").Run """{start}""", 0, False\r\n', encoding="utf-8")
    step("Автозапуск включён: бот будет стартовать вместе с Windows (без окон).")

    ans = "" if quiet else input("\nОтключить спящий режим, пока ПК от сети? Иначе бот уснёт вместе с ПК. [Д/н]: ").strip().lower()
    if ans in ("", "д", "да", "y", "yes"):
        for k in ("standby-timeout-ac", "hibernate-timeout-ac"):
            subprocess.run(["powercfg", "/change", k, "0"])
        print("Сон при питании от сети отключён.")

    subprocess.Popen(["wscript", str(vbs)])
    print("\n✅ Готово! Бот запущен. Через минуту он напишет тебе в Telegram «Бот запущен на ПК».")
    print("Управление — только из Telegram: /live, /stop, /update, /log. Это окно можно закрыть.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n❌ Ошибка установки:", e)
        print("Сфотографируй это окно и пришли в чат с Claude.")
