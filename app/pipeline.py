"""Конвейер: анализ видео -> выбор моментов -> монтаж шортсов.

Этапы разделены, чтобы бот мог показать найденные моменты до монтажа,
дорезать «ещё 3» без повторного анализа и собрать «Топ-5».
"""
import logging
from pathlib import Path

import numpy as np

from . import ai, fx, gamecam, media, reframe, review, subs
from . import config as cfg

log = logging.getLogger(__name__)

LENGTHS = {"short": (15, 30), "mid": (30, 45), "long": (45, 59)}
GAMES = ("mlbb", "pubg")


class Cancelled(Exception):
    pass


def _use_llm():
    return bool(cfg.ANTHROPIC_API_KEY)


def style_of(st: dict) -> dict:
    return {"color": st.get("sub_color", "yellow"), "size": st.get("sub_size", "m"),
            "pos": st.get("sub_pos", "bottom"), "title": st.get("title", "on") == "on",
            "mark": st.get("mark", "")}


# ---------- 1. Анализ ----------

def analyze(src: Path, st: dict, progress) -> dict:
    content = st.get("content", "auto")
    sub_lang = st.get("subs", "orig")
    seq = st.get("pick", "best") == "seq"
    progress("🔍 Анализирую видео…")
    info = media.probe(src)
    dur = info["duration"]
    segments, lang = [], None
    if info["audio"] and (sub_lang != "none" or (_use_llm() and not seq) or content in GAMES):
        progress("📝 Распознаю речь… 0%")
        tr = ai.transcribe(src, lambda p: progress(f"📝 Распознаю речь… {int(p * 100)}%"))
        segments, lang = tr["segments"], tr["lang"]
    progress("🎞 Ищу смены сцен и пики звука…")
    cuts = media.detect_cuts(src)
    loud = media.loudness_per_sec(src) if info["audio"] else np.zeros(1)
    motion = np.zeros(1)
    if content != "auto" and not seq:
        progress("⚡ Ищу экшен: движение, вспышки, килл-моменты…")
        motion = media.motion_per_sec(src)
    events = ai.game_events(segments, content) if content != "auto" else []
    score = ai.activity(dur, loud, motion, events)
    return {"info": info, "dur": dur, "segments": segments, "lang": lang, "cuts": cuts,
            "loud": loud, "motion": motion, "events": events, "score": score}


# ---------- 2. Выбор моментов ----------

def _overlaps(c, used):
    return any(not (c["end"] <= a or c["start"] >= b) for a, b in used)


def choose(A: dict, st: dict, n: int, used=(), lengths=None) -> list[dict]:
    dur, segments, cuts = A["dur"], A["segments"], A["cuts"]
    content = st.get("content", "auto")
    lmin, lmax = lengths or LENGTHS.get(st.get("length", "mid"), (30, 45))
    used = list(used)

    if st.get("ranges"):  # ручные таймкоды
        clips = [{"start": max(0.0, a), "end": min(dur, b), "title": "", "score": 0, "type": ""}
                 for a, b in st["ranges"] if b - a >= 3 and a < dur]
        return _attach(clips, A)
    if st.get("pick") == "seq":
        clips = ai.split_sequential(dur, segments, cuts, lmin, lmax)
        clips = [c for c in clips if not _overlaps(c, used)]
        return _attach(clips[:n] if n else clips, A)

    n = n or 5
    want = n + len(used) + 2
    sub_lang, lang = st.get("subs", "orig"), A["lang"]
    title_lang = sub_lang if sub_lang in ai.LANG_NAMES else (lang if lang in ai.LANG_NAMES else "ru")
    game = content in GAMES
    clips = []
    if _use_llm() and (segments or game) and not lengths:
        try:
            signals = ai.signals_text(A["score"], A["events"]) if content != "auto" else ""
            clips = ai.pick_llm(segments, want, lmin, lmax, title_lang, content, signals)
        except Exception as e:  # не падаем — уходим на алгоритм
            log.warning("LLM pick failed: %s", e)
    if not clips:
        if game or lengths:
            clips = ai.pick_game(dur, A["score"], A["events"], want, lmin, lmax)
        elif content == "movie":
            clips = ai.pick_heuristic(dur, A["score"], cuts, segments, want, lmin, lmax)
        else:
            clips = ai.pick_heuristic(dur, A["loud"], cuts, segments, want, lmin, lmax)
    if not game and not lengths:
        clips = ai.snap(clips, segments, cuts, dur, lmin, lmax)
    clips = [c for c in clips if not _overlaps(c, used)][:n]
    clips.sort(key=lambda c: c["start"])
    return _attach(clips, A)


def _attach(clips, A):
    """Добавляет к клипу моменты для эффектов (килы, а если их нет — пик экшена)."""
    sc = A["score"]
    for c in clips:
        ev = [e["t"] - c["start"] for e in A["events"]
              if e["kind"] == "kill" and e["w"] >= 3 and c["start"] <= e["t"] <= c["end"] - 0.3]
        if not ev and len(sc) > 1:
            a, b = int(c["start"]), max(int(c["start"]) + 1, int(c["end"]))
            seg = sc[a:b]
            if len(seg) and seg.max() > 1.5:
                ev = [float(np.argmax(seg)) + 0.3]
        c["fx_t"] = sorted(ev)
        c["fx_big"] = max((e["w"] for e in A["events"] if c["start"] <= e["t"] <= c["end"]
                           and e["kind"] == "kill"), default=0)
    return clips


# ---------- 3. Монтаж ----------

def render(src: Path, workdir: Path, st: dict, A: dict, clips: list[dict], progress,
           on_clip=None, cancel=lambda: False) -> list[dict]:
    content = st.get("content", "auto")
    if content in GAMES and st.get("mode", "auto") == "auto":
        st = {**st, "mode": "game"}  # игровой макет: весь кадр сверху + крупно герой снизу
    info, segments, cuts, lang = A["info"], A["segments"], A["cuts"], A["lang"]
    sub_lang = st.get("subs", "orig")
    use_llm = _use_llm()
    results = []
    for i, c in enumerate(clips, 1):
        if cancel():
            raise Cancelled()
        progress(f"🎬 Монтирую клип {i}/{len(clips)}…")
        d = workdir / f"clip_{int(c['start'])}_{int(c['end'])}"
        d.mkdir(exist_ok=True)
        raw = d / "raw.mp4"
        media.extract(src, c["start"], c["end"], raw)

        seg_in = [s for s in segments if s["e"] > c["start"] and s["s"] < c["end"]]
        word_level = True
        if sub_lang == "none":
            seg_in = []
        elif sub_lang != "orig" and sub_lang != lang and seg_in and use_llm:
            try:
                seg_in = ai.translate(seg_in, sub_lang)
                word_level = False
            except Exception as e:
                log.warning("translate failed: %s", e)
        inserts = []
        if st.get("review") == "on" and use_llm:
            progress(f"🎙 Пишу и озвучиваю комментарий {i}/{len(clips)}…")
            vlang = sub_lang if sub_lang in ai.LANG_NAMES else (lang if lang in ai.LANG_NAMES else "ru")
            try:
                inserts = review.prepare(segments, c["start"], c["end"], vlang, d)
            except Exception as e:
                log.warning("review failed: %s", e)
        subs.build_ass(d / "subs.ass", seg_in, c["start"], c["end"], c.get("title", ""), word_level,
                       overlay=review.overlay_words(inserts), mute=review.windows(inserts),
                       style=style_of(st), labels=c.get("labels"))
        local_cuts = c.get("cuts") or [x - c["start"] for x in cuts if c["start"] < x < c["end"]]
        out = _frame(raw, d, local_cuts, c["end"] - c["start"], info, st, c.get("fx_t", []))
        if inserts:
            mixed = d / "mixed.mp4"
            review.mix(out, inserts, mixed)
            out = mixed
        out = _finish(out, d, c, st, info, progress, i, len(clips))
        final = d / f"short_{i}.mp4"
        out.replace(final)
        raw.unlink(missing_ok=True)
        r = {"path": final, "title": c.get("title", ""), "start": c["start"], "end": c["end"],
             "type": c.get("type", ""), "content": content, "index": i, "total": len(clips),
             "text": c.get("text", "")}
        if on_clip:
            on_clip(r)  # отправляем сразу и удаляем, чтобы не копить на диске
            final.unlink(missing_ok=True)
        results.append(r)
    return results


def _frame(raw, d, local_cuts, dur, info, st, events=()):
    out = d / "framed.mp4"
    if st.get("mode") == "game" and info["w"] > info["h"]:
        try:
            lay = gamecam.plan(raw, dur, info["w"], info["h"], st.get("content", "mlbb"),
                               use_ai=st.get("director", "on") == "on", events=events)
            log.info("gamecam: %s, ai=%s, shots=%d", lay["layout"], lay["ai"], len(lay["shots"]))
            gamecam.render(raw, out, "subs.ass", lay, info["w"], info["h"])
            return out
        except Exception as e:
            log.warning("gamecam failed: %s", e)
    try:
        shots = reframe.plan(raw, local_cuts, dur, info["w"], info["h"], st.get("mode", "auto"))
        reframe.render(raw, out, "subs.ass", shots, info["w"], info["h"])
    except Exception as e:  # запасной простой режим, чтобы клип всё равно получился
        log.warning("smart reframe failed: %s", e)
        media.render(raw, out, "subs.ass", "blur", info["w"], info["h"])
    return out


def _finish(out, d, c, st, info, progress, i, total):
    """Эффекты на килах и фоновая музыка."""
    fx_mode = st.get("fx", "auto")
    want_fx = fx_mode == "on" or (fx_mode == "auto" and st.get("content") in GAMES)
    if want_fx and c.get("fx_t"):
        progress(f"💥 Эффекты {i}/{total}…")
        try:
            dst = d / "fx.mp4"
            big = c["fx_t"][0] if c.get("fx_big", 0) >= 6 else None
            fx.punch(out, dst, c["fx_t"], slowmo_at=big, has_audio=info["audio"])
            out = dst
        except Exception as e:
            log.warning("fx failed: %s", e)
    if st.get("music") == "on":
        try:
            dst = d / "music.mp4"
            if fx.add_music(out, dst, has_audio=info["audio"]):
                out = dst
        except Exception as e:
            log.warning("music failed: %s", e)
    return out


# ---------- Топ-5 ----------

def compile_top(src: Path, workdir: Path, st: dict, A: dict, progress, k=5, piece=(7, 11)):
    """Один шортс из k лучших моментов: #5 … #1 (лучший в конце)."""
    parts = choose(A, {**st, "ranges": None, "pick": "best"}, k, lengths=piece)
    if not parts:
        raise RuntimeError("Не нашёл ярких моментов для подборки")
    parts.sort(key=lambda c: c.get("score", 0))  # слабый первым, лучший — последним
    d = workdir / "top"
    d.mkdir(exist_ok=True)
    listing, t, segs, labels, cuts_all, fx_t = [], 0.0, [], [], [], []
    for j, c in enumerate(parts):
        p = d / f"p{j}.mp4"
        media.extract(src, c["start"], c["end"], p)
        listing.append(f"file '{p.name}'")
        L = c["end"] - c["start"]
        num = len(parts) - j
        labels.append((t, t + L, f"#{num} {c.get('title') or ''}".strip()))
        for s in A["segments"]:
            if s["e"] > c["start"] and s["s"] < c["end"]:
                sh = t - c["start"]
                segs.append({**s, "s": s["s"] + sh, "e": s["e"] + sh,
                             "words": [{**w, "s": w["s"] + sh, "e": w["e"] + sh}
                                       for w in s.get("words", [])
                                       if c["start"] <= w["s"] < c["end"]]})
        fx_t += [t + x for x in c.get("fx_t", [])]
        if j:
            cuts_all.append(t)
        t += L
    (d / "list.txt").write_text("\n".join(listing))
    joined = workdir / "top_src.mp4"
    media.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
               "-c", "copy", str(joined)], cwd=d)
    A2 = {**A, "segments": segs, "events": [], "cuts": cuts_all, "score": np.zeros(int(t) + 1)}
    clip = {"start": 0.0, "end": t, "title": "", "labels": labels, "cuts": cuts_all,
            "fx_t": fx_t, "fx_big": 0, "type": "kill", "text": "Топ-5 моментов"}
    res = render(joined, workdir, {**st, "fx": "on" if st.get("fx") != "off" else "off"}, A2,
                 [clip], progress)
    res[0]["title"] = "🏆 Топ-5 моментов"
    return res[0]


def process(src: Path, workdir: Path, st: dict, progress, on_clip=None) -> list[dict]:
    """Совместимость: всё сразу."""
    A = analyze(src, st, progress)
    n = int(st.get("clips", 5))
    clips = choose(A, st, n)
    if not clips:
        raise RuntimeError("Не удалось найти подходящие фрагменты")
    return render(src, workdir, st, A, clips, progress, on_clip)
