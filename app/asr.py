"""Распознавание речи в отдельном процессе.

На Windows CTranslate2 при загрузке модели иногда намертво виснет, удерживая GIL, —
тогда замирал весь бот (и меню, и /cancel). Отдельный процесс можно просто убить и
перезапустить, а бот при этом остаётся живым.
Протокол: дочерний процесс печатает строки «L» (модель загружена), «P 0.42» (прогресс),
«OK» (готово, результат в json-файле) или «ERR текст»."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

LOAD_TIMEOUT = int(os.getenv("ASR_LOAD_TIMEOUT", 600))   # загрузка модели
STEP_TIMEOUT = int(os.getenv("ASR_STEP_TIMEOUT", 900))   # тишина между кусками речи


class ChildCrash(RuntimeError):
    pass


_BROKEN = False   # дочерний процесс на этом ПК не работает — сразу распознаём внутри бота


def run(path, progress=None, content="auto", log=None, inproc=None) -> dict:
    """Запуск в дочернем процессе с таймаутами; при зависании — повторная попытка,
    при падении процесса — распознавание внутри бота (как раньше)."""
    global _BROKEN
    if _BROKEN and inproc:
        return inproc()
    last_err = ""
    for attempt in (1, 2):
        try:
            return _run_once(path, progress, content, log, threads=0 if attempt == 1 else 2)
        except TimeoutError as e:
            last_err = str(e)
            if log:
                log.warning("asr: %s — попытка %d, перезапускаю распознавание", e, attempt)
        except ChildCrash as e:
            if log:
                log.error("asr: %s", e)
            if inproc:
                _BROKEN = True
                log and log.warning("asr: перехожу на распознавание внутри бота")
                return inproc()
            raise
    raise RuntimeError(f"распознавание речи зависло ({last_err})")


def _run_once(path, progress, content, log, threads: int) -> dict:
    out = Path(str(path) + ".asr.json")
    out.unlink(missing_ok=True)
    env = {**os.environ, "ASR_THREADS": str(threads)}
    p = subprocess.Popen([sys.executable, "-m", "app.asr", str(path), content, str(out)],
                         cwd=Path(__file__).resolve().parent.parent, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace",
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines: list[str] = []
    ev = threading.Event()

    def reader():
        for ln in p.stdout:
            lines.append(ln.rstrip())
            ev.set()
        ev.set()

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    loaded, last, seen, tail = False, time.time(), 0, []
    try:
        while True:
            ev.wait(5)
            ev.clear()
            while seen < len(lines):
                ln = lines[seen]
                seen += 1
                last = time.time()
                if ln == "L":
                    loaded = True
                    if log:
                        log.info("asr: модель загружена")
                elif ln.startswith("P ") and progress:
                    progress(float(ln[2:]))
                elif ln == "OK":
                    p.wait(30)
                    return json.loads(out.read_text(encoding="utf-8"))
                elif ln.startswith("ERR "):
                    raise RuntimeError("распознавание: " + ln[4:])
                else:
                    tail = (tail + [ln])[-15:]
            if p.poll() is not None:
                rt.join(10)   # дочитываем всё, что процесс успел напечатать перед выходом
                if seen < len(lines):
                    continue
                raise ChildCrash(f"процесс распознавания завершился (код {p.returncode}): "
                                 + " | ".join(tail[-6:]))
            if progress:
                progress(None)   # даёт боту шанс отменить задачу
            limit = STEP_TIMEOUT if loaded else LOAD_TIMEOUT
            if time.time() - last > limit:
                raise TimeoutError(("загрузка модели" if not loaded else "распознавание") +
                                   f" не двигается {limit} с")
    finally:
        if p.poll() is None:
            p.kill()
        out.unlink(missing_ok=True)


def _model():
    from faster_whisper import WhisperModel
    from . import config as cfg
    kw = dict(device=cfg.WHISPER_DEVICE, compute_type=cfg.WHISPER_COMPUTE,
              download_root=str(cfg.DATA_DIR / "models"))
    th = int(os.getenv("ASR_THREADS", "0") or 0)
    if th:
        kw["cpu_threads"] = th
    try:   # модель уже скачана — грузим без обращения к интернету
        return WhisperModel(cfg.WHISPER_MODEL, local_files_only=True, **kw)
    except Exception:
        return WhisperModel(cfg.WHISPER_MODEL, **kw)


def _child(path, content, out):
    from . import ai
    ai._whisper = _model()
    print("L", flush=True)
    res = ai._transcribe(path, lambda f: print(f"P {f:.4f}", flush=True), content)
    Path(out).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print("OK", flush=True)


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()   # если упадёт внутри C-библиотеки — хотя бы будет видно где
    print("S", flush=True)
    try:
        _child(sys.argv[1], sys.argv[2], sys.argv[3])
    except Exception as e:
        print("ERR " + f"{type(e).__name__}: {e}".replace("\n", " "), flush=True)
        sys.exit(1)
