"""Сторож для бота на ПК: если бот завис (перестал отмечаться), убивает его —
start_bot.bat сам запустит бота заново. Запускается ботом как отдельный процесс."""
import os
import signal
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


def main(pid: int, beat: Path):
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
            with open(beat.parent / "watchdog.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} бот завис ({int(age)} с без отметки) — перезапуск\n")
            try:
                os.kill(pid, signal.SIGTERM)   # на Windows это TerminateProcess
            except OSError:
                pass
            return


if __name__ == "__main__":
    main(int(sys.argv[1]), Path(sys.argv[2]))
