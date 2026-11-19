"""Сторож для бота на ПК: если бот завис (перестал отмечаться), убивает его —
start_bot.bat сам запустит бота заново. Запускается ботом как отдельный процесс."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

STALE = int(os.getenv("WATCHDOG_STALE", 300))   # 5 минут без отметки — бот считается зависшим
START = int(os.getenv("WATCHDOG_START", 120))


def alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259   # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def note(beat: Path, text: str):
    try:
        with open(beat.parent / "watchdog.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def main(pid: int, beat: Path):
    note(beat, f"сторож запущен, слежу за pid {pid}")
    time.sleep(START)   # даём боту запуститься
    last_tick = time.time()
    while alive(pid):
        time.sleep(min(30, STALE / 3))
        now = time.time()
        slept = now - last_tick > max(120, STALE)     # ПК спал — после пробуждения даём боту время отметиться
        last_tick = now
        if slept:
            time.sleep(90)
            continue
        try:
            age = now - beat.stat().st_mtime
        except OSError:
            age = 0
        if age > STALE:
            note(beat, f"бот завис ({int(age)} с без отметки) — перезапуск")
            try:
                os.kill(pid, signal.SIGTERM)   # на Windows это TerminateProcess
            except OSError as e:
                note(beat, f"не смог убить: {e}")
            if os.name == "nt" and alive(pid):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            return
    note(beat, "бот завершился — сторож выходит")


if __name__ == "__main__":
    main(int(sys.argv[1]), Path(sys.argv[2]))
