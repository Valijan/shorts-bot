"""Полный цикл: видео -> список готовых шортсов."""
import logging
from pathlib import Path

from . import ai, media, subs
from . import config as cfg

log = logging.getLogger(__name__)

LENGTHS = {"short": (15, 30), "mid": (30, 45), "long": (45, 59)}


def process(src: Path, workdir: Path, st: dict, progress, on_clip=None) -> list[dict]:
    """st: {"clips": int, "subs": "orig|ru|uz|en|none", "mode": "auto|crop|blur", "length": key}"""
    n = int(st.get("clips", 5))
    seq = st.get("pick", "best") == "seq"
    lmin, lmax = LENGTHS.get(st.get("length", "mid"), (30, 45))
    sub_lang = st.get("subs", "orig")
    use_llm = bool(cfg.ANTHROPIC_API_KEY)

    progress("🔍 Анализирую видео…")
    info = media.probe(src)
    dur = info["duration"]

    segments, lang = [], None
    if info["audio"] and (sub_lang != "none" or (use_llm and not seq)):
        progress("📝 Распознаю речь… 0%")
        tr = ai.transcribe(src, lambda p: progress(f"📝 Распознаю речь… {int(p * 100)}%"))
        segments, lang = tr["segments"], tr["lang"]

    progress("🎞 Ищу смены сцен и пики звука…")
    cuts = media.detect_cuts(src)
    loud = media.loudness_per_sec(src) if info["audio"] else []

    if seq:
        clips = ai.split_sequential(dur, segments, cuts, lmin, lmax)
        if n:
            clips = clips[:n]
    else:
        clips = _pick_best(dur, loud, cuts, segments, n or 5, lmin, lmax, sub_lang, lang, use_llm)
    if not clips:
        raise RuntimeError("Не удалось найти подходящие фрагменты")
    return _render_all(src, workdir, st, info, clips, segments, cuts, lang, sub_lang, use_llm,
                       progress, on_clip)


def _pick_best(dur, loud, cuts, segments, n, lmin, lmax, sub_lang, lang, use_llm):
    progress = lambda *_: None
    title_lang = sub_lang if sub_lang in ai.LANG_NAMES else (lang if lang in ai.LANG_NAMES else "ru")
    clips = []
    if use_llm and segments:
        try:
            clips = ai.pick_llm(segments, n, lmin, lmax, title_lang)
        except Exception as e:  # не падаем — уходим на алгоритм
            log.warning("LLM pick failed: %s", e)
    if not clips:
        clips = ai.pick_heuristic(dur, loud, cuts, segments, n, lmin, lmax)
    clips = ai.snap(clips, segments, cuts, dur, lmin, lmax)[:n]
    clips.sort(key=lambda c: c["start"])
    return clips


def _render_all(src, workdir, st, info, clips, segments, cuts, lang, sub_lang, use_llm,
                progress, on_clip):
    results = []
    for i, c in enumerate(clips, 1):
        progress(f"🎬 Монтирую клип {i}/{len(clips)}…")
        d = workdir / f"clip{i}"
        d.mkdir(exist_ok=True)
        raw = d / "raw.mp4"
        media.extract(src, c["start"], c["end"], raw)

        # субтитры
        seg_in = [s for s in segments if s["e"] > c["start"] and s["s"] < c["end"]]
        word_level = True
        if sub_lang == "none":
            seg_in = []
        elif sub_lang != "orig" and sub_lang != lang and seg_in:
            if use_llm:
                try:
                    seg_in = ai.translate(seg_in, sub_lang)
                    word_level = False
                except Exception as e:
                    log.warning("translate failed: %s", e)
        subs.build_ass(d / "subs.ass", seg_in, c["start"], c["end"], c.get("title", ""), word_level)

        # кадрирование
        local_cuts = [x - c["start"] for x in cuts if c["start"] < x < c["end"]]
        mode = st.get("mode", "auto")
        shots = None
        if mode in ("auto", "crop") and info["w"] > info["h"]:
            ratio, shots = media.face_track(raw, local_cuts, info["w"], info["h"])
            if mode == "auto":
                mode = "crop" if ratio >= 0.45 else "blur"
        out = d / f"short_{i}.mp4"
        media.render(raw, out, "subs.ass", mode, info["w"], info["h"], shots)
        raw.unlink(missing_ok=True)
        r = {"path": out, "title": c.get("title", ""), "start": c["start"],
             "end": c["end"], "mode": mode, "index": i, "total": len(clips)}
        if on_clip:
            on_clip(r)  # отправляем сразу и удаляем, чтобы не копить на диске
            out.unlink(missing_ok=True)
        else:
            results.append(r)
    return results if not on_clip else [None] * len(clips)
