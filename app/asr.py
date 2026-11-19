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


# Варианты запуска CTranslate2: на некоторых ПК стандартный падает (0xC0000005) при загрузке модели.
VARIANTS = [
    {},
    {"CT2_FORCE_CPU_ISA": "AVX2", "ASR_COMPUTE": "float32"},
    {"CT2_FORCE_CPU_ISA": "GENERIC", "ASR_COMPUTE": "float32", "ASR_THREADS": "4"},
    {"ASR_MODEL": "base", "CT2_FORCE_CPU_ISA": "AVX2", "ASR_COMPUTE": "float32"},
]
_NOSPEECH = False   # ни один вариант не работает — режем без речи (по чату и звуку), бот живёт


def _vfile():
    from . import config as cfg
    return cfg.DATA_DIR / "asr-variant.txt"


def run(path, progress=None, content="auto", log=None, inproc=None) -> dict:
    """Распознавание в дочернем процессе. Падение или зависание убивает только его;
    перебираем варианты запуска и запоминаем рабочий. Внутри бота не запускаем никогда —
    падение CTranslate2 там роняет весь бот."""
    global _NOSPEECH
    if _NOSPEECH:
        return {"lang": "en", "segments": []}
    try:
        start = int(_vfile().read_text().strip())
    except Exception:
        start = 0
    order = [i for i in range(len(VARIANTS)) if i >= start] + [i for i in range(start)]
    errs = []
    for i in order:
        try:
            res = _run_once(path, progress, content, log, VARIANTS[i])
            if i != start:
                try:
                    _vfile().write_text(str(i))
                except OSError:
                    pass
                log and log.warning("asr: рабочий вариант %d %s — запомнил", i, VARIANTS[i])
            return res
        except (TimeoutError, ChildCrash) as e:
            errs.append(f"{i}: {e}")
            log and log.error("asr: вариант %d %s не сработал: %s", i, VARIANTS[i], e)
    _NOSPEECH = True
    log and log.error("asr: распознавание речи не работает на этом ПК — режу без речи до перезапуска")
    return {"lang": "en", "segments": []}


def _run_once(path, progress, content, log, variant: dict) -> dict:
    out = Path(str(path) + ".asr.json")
    out.unlink(missing_ok=True)
    env = {**os.environ, **variant}
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
    kw = dict(device=cfg.WHISPER_DEVICE, compute_type=os.getenv("ASR_COMPUTE") or cfg.WHISPER_COMPUTE,
              download_root=str(cfg.DATA_DIR / "models"))
    name = os.getenv("ASR_MODEL") or cfg.WHISPER_MODEL
    th = int(os.getenv("ASR_THREADS", "0") or 0)
    if th:
        kw["cpu_threads"] = th
    try:   # модель уже скачана — грузим без обращения к интернету
        return WhisperModel(name, local_files_only=True, **kw)
    except Exception:
        return WhisperModel(name, **kw)


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
