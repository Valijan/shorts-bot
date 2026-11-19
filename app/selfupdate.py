"""Самообновление бота с GitHub (для домашнего ПК под Windows, где нет git).

Запускается из start_bot.bat перед каждым стартом бота: скачивает свежий архив репозитория
и раскладывает файлы поверх, не трогая настройки (.env), данные, venv, ffmpeg и шрифты.
Если поменялся requirements.txt — доустанавливает библиотеки.
"""
import io
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ZIP = "https://codeload.github.com/Valijan/shorts-bot/zip/refs/heads/main"
ROOT = Path(__file__).resolve().parent.parent
KEEP = {".env", "data", "venv", "ffmpeg", "fonts", "bot.log", ".git"}


def _rotate_log():
    log = ROOT / "bot.log"
    if log.exists() and log.stat().st_size > 5_000_000:
        old = ROOT / "bot.old.log"
        old.unlink(missing_ok=True)
        log.rename(old)


def update() -> bool:
    if (ROOT / ".git").exists() and shutil.which("git"):
        r = subprocess.run(["git", "-C", str(ROOT), "pull", "-q"], capture_output=True, text=True)
        print("git pull:", r.returncode, r.stderr.strip()[:200])
        return r.returncode == 0
    try:
        data = urllib.request.urlopen(REPO_ZIP, timeout=60).read()
    except Exception as e:
        print("обновление пропущено (нет связи с GitHub):", e)
        return False
    old_req = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        top = z.namelist()[0].split("/")[0]
        for name in z.namelist():
            rel = name[len(top) + 1:]
            if not rel or rel.split("/")[0] in KEEP:
                continue
            dst = ROOT / rel
            if name.endswith("/"):
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(z.read(name))
    if (ROOT / "requirements.txt").read_bytes() != old_req:
        print("requirements.txt изменился — доустанавливаю библиотеки")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")])
    print("обновлено с GitHub")
    return True


def repair_cv2():
    """opencv-python и opencv-python-headless пишут в одну папку cv2; если версии разные
    (5.x от scenedetect), cv2 оказывается сломан — поиск лица и раскладка стрима не работают."""
    ok = subprocess.run([sys.executable, "-c", "import cv2; cv2.CascadeClassifier; cv2.FaceDetectorYN"],
                        capture_output=True).returncode == 0
    if ok:
        return
    print("cv2 сломан — переустанавливаю OpenCV 4.x")
    pip = [sys.executable, "-m", "pip"]
    subprocess.run(pip + ["uninstall", "-y", "-q", "opencv-python", "opencv-python-headless"])
    subprocess.run(pip + ["install", "-q", "--no-cache-dir", "opencv-python-headless>=4.9,<5", "opencv-python>=4.9,<5"])


if __name__ == "__main__":
    _rotate_log()
    update()
    try:
        repair_cv2()
    except Exception as e:
        print("проверка cv2 не удалась:", e)
