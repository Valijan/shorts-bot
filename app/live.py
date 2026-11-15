"""Нарезка прямого эфира: запись потока кусками и отбор только по-настоящему ярких моментов.

Поток пишется без перекодирования (качество как в эфире, видеокарта не занята).
Каждый готовый кусок проходит обычный конвейер; чтобы момент на стыке кусков не потерялся,
к куску приклеивается хвост предыдущего.
"""
import os
import subprocess
from pathlib import Path

from . import config as cfg

CHUNK = cfg._int("LIVE_CHUNK", 180)          # длина куска, с: клип приходит через ~CHUNK + монтаж
OVERLAP = 20                                  # хвост прошлого куска, с
MAX_HOURS = cfg._int("LIVE_MAX_HOURS", 3)     # страховка: эфир сам остановится, чтобы не сжечь баланс
MAX_BACKLOG = 3                               # если монтаж не успевает — старые куски пропускаются
PER_CHUNK = 2                                 # максимум клипов с одного куска
MODE = os.getenv("LIVE_MODE", "auto").lower()   # auto | full | light
PRE, POST = 28, 14                            # лёгкий режим: окно вокруг момента, с
VISION_FRAMES = 12                            # кадров на кусок для ИИ (в обычном режиме 36 на всё видео)


def resolve(url: str) -> dict:
    """Адрес потока. Ошибка, если по ссылке не прямой эфир."""
    from yt_dlp import YoutubeDL

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "b[height<=1080]/bv*[height<=1080]+ba/b"}
    if cfg.YTDLP_COOKIES and Path(cfg.YTDLP_COOKIES).exists():
        opts["cookiefile"] = cfg.YTDLP_COOKIES
    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    if not info.get("is_live"):
        raise RuntimeError("По ссылке сейчас нет прямого эфира. Запись стрима пришли обычной ссылкой.")
    fmts = info.get("requested_formats") or [info]
    return {"inputs": [(f["url"], f.get("http_headers") or {}) for f in fmts],
            "title": info.get("title") or "", "channel": info.get("uploader") or info.get("channel") or ""}


def record_cmd(inputs, outdir: Path, chunk: int = CHUNK, limit: float | None = None) -> list[str]:
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    for u, headers in inputs:
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        cmd += ["-i", u]
    for i in range(len(inputs)):
        cmd += ["-map", f"{i}:v?" if len(inputs) > 1 and i == 0 else (f"{i}:a?" if len(inputs) > 1 else "0")]
    if limit:
        cmd += ["-t", str(int(limit))]
    cmd += ["-c", "copy", "-f", "segment", "-segment_time", str(chunk), "-reset_timestamps", "1",
            str(outdir / "chunk_%05d.ts")]
    return cmd


def start(inputs, outdir: Path, chunk: int = CHUNK, limit: float | None = None) -> subprocess.Popen:
    outdir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(record_cmd(inputs, outdir, chunk, limit),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ready_chunks(outdir: Path, done: set, finished: bool) -> list[Path]:
    """Дописанные куски: все, кроме последнего (он ещё пишется), а после конца записи — все."""
    files = sorted(p for p in outdir.glob("chunk_*.ts") if p.name not in done)
    return files if finished else files[:-1]


def make_window(chunk: Path, prev_tail: Path | None, workdir: Path) -> tuple[Path, float]:
    """Кусок с приклеенным хвостом предыдущего. Возвращает (файл, длина хвоста в секундах)."""
    from .media import probe, run

    workdir.mkdir(parents=True, exist_ok=True)
    if not prev_tail or not prev_tail.exists():
        return chunk, 0.0
    lst = workdir / "win.txt"
    lst.write_text(f"file '{prev_tail.resolve()}'\nfile '{chunk.resolve()}'\n")
    win = workdir / "window.mp4"
    try:
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", "-movflags", "+faststart", str(win)])
        return win, probe(prev_tail)["duration"]
    except Exception:
        return chunk, 0.0


def save_tail(chunk: Path, dst: Path, seconds: int = OVERLAP):
    """Последние секунды куска — для склейки со следующим."""
    from .media import run

    try:
        run(["ffmpeg", "-y", "-v", "error", "-sseof", f"-{seconds}", "-i", str(chunk),
             "-c", "copy", str(dst)])
    except Exception:
        dst.unlink(missing_ok=True)


def worth_it(clips: list[dict], events: list[dict], overlap: float, used_llm: bool) -> list[dict]:
    """В эфире шлём не «лучшее из куска», а только то, что действительно зацепит:
    внутри есть событие (кил, WASTED, смешной момент) или ИИ оценил момент на 7+."""
    out = []
    for c in clips:
        if c["end"] <= overlap + 1:  # целиком в хвосте — уже было в прошлом куске
            continue
        strong = any(e["kind"] != "talk" and e["w"] >= 3 and c["start"] <= e["t"] <= c["end"] for e in events)
        if strong or (used_llm and c.get("score", 0) >= 7):
            out.append(c)
    return out[:PER_CHUNK]


def light() -> bool:
    """Лёгкий режим — для сервера без видеокарты: сначала дешёвый поиск всплесков (чат, громкость),
    и только вокруг них — распознавание речи и монтаж. Тихие куски не обрабатываются вовсе."""
    return MODE == "light" or (MODE == "auto" and cfg.WHISPER_DEVICE == "cpu")


def loud_events(src: Path) -> list[dict]:
    """Резкие всплески громкости (крик, взрыв) — дёшево, без распознавания речи."""
    import numpy as np

    from .media import loudness_per_sec

    loud = loudness_per_sec(src)
    if len(loud) < 20:
        return []
    sm = np.convolve(loud, np.ones(3) / 3, mode="same")
    base = float(np.median(sm))
    out = []
    for i in np.argsort(sm)[::-1][:40]:
        if sm[i] < base + 8 or sm[i] < -30:  # заметно громче обычного и не тишина
            break
        if all(abs(int(i) - e["t"]) >= 25 for e in out):
            out.append({"t": float(i), "w": 3.0, "kind": "funny", "label": "громкий момент"})
    return out


def candidates(events: list[dict], overlap: float, dur: float) -> list[dict]:
    """Моменты, ради которых стоит обрабатывать кусок. Момент у самого конца куска оставляем
    следующему (там он будет с продолжением), момент в хвосте прошлого куска — уже был."""
    ok = [e for e in events if e["kind"] != "talk" and e["w"] >= 3 and max(0.0, overlap - 8) <= e["t"] < dur - 8]
    out = []
    for e in sorted(ok, key=lambda e: -e["w"]):
        if all(abs(e["t"] - x["t"]) >= 30 for x in out):
            out.append(e)
    return out[:PER_CHUNK]


def cut_window(src: Path, t: float, dur: float, dst: Path) -> tuple[Path, float]:
    """Вырезает окно вокруг момента без перекодирования. Возвращает (файл, начало окна в куске)."""
    from .media import run

    a = max(0.0, t - PRE)
    b = min(dur, t + POST)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.2f}", "-i", str(src), "-t", f"{b - a:.2f}",
         "-c", "copy", "-avoid_negative_ts", "make_zero", str(dst)])
    return dst, a
