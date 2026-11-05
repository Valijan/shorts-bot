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
AUTO_LEN = {"mlbb": "short", "pubg": "short", "movie": "long"}  # игры 15–30 с — норма для Shorts


def lengths_for(st):
    key = st.get("length", "auto")
    if key == "auto":
        key = AUTO_LEN.get(st.get("content"), "mid")
    lmin, lmax = LENGTHS.get(key, LENGTHS["mid"])
    need = int(st.get("camp_min") or 0)  # условие кампании «от N секунд»
    if need > lmin:
        lmin, lmax = need, max(lmax, need + 10)
    return lmin, lmax
GAMES = ("mlbb", "pubg")


class Cancelled(Exception):
    pass


def _use_llm():
    return bool(cfg.ANTHROPIC_API_KEY)


def style_of(st: dict, lay: dict | None = None) -> dict:
    words = st.get("sub_words", "auto")
    one = words == "1" or (words == "auto" and st.get("content") in GAMES)
    t = st.get("title", "hook")
    style = {"color": st.get("sub_color", "yellow"), "size": st.get("sub_size", "m"),
             "pos": st.get("sub_pos", "bottom"), "title": t if t in ("on", "off", "hook") else "hook",
             "mark": st.get("mark", ""), "words": 1 if one else 3,
             "censor": st.get("censor", "off") == "on", "anim": st.get("sub_anim", "pop"),
             "camp": st.get("camp_label", "")}
    if lay and lay.get("layout") == "stream":   # камера сверху, полоса с ником, геймплей
        top = lay["cam_h"] + lay["bar_h"]
        below_bar = lay["bar_h"] and st.get("camp_label")
        style.update(banner_y=lay["cam_h"] + (lay["bar_h"] if below_bar else 0), sub_y=1360)
        if lay["bar_h"]:
            style["bar_y"] = lay["cam_h"]
    elif lay:                                   # камера во весь экран
        style.update(banner_y=330 if st.get("camp_label") else 300, sub_y=1320)
    return style


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
        tr = ai.transcribe(src, lambda p: progress(f"📝 Распознаю речь… {int(p * 100)}%"), content)
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
    if content in GAMES and _use_llm() and st.get("vision", "on") == "on":
        progress("👁 ИИ ищет килы на экране…")
        try:
            vis = ai.vision_events(src, score, content)
            if vis:
                events = events + vis
                score = ai.activity(dur, loud, motion, events)
        except Exception as e:
            log.warning("vision events failed: %s", e)
    return {"info": info, "dur": dur, "segments": segments, "lang": lang, "cuts": cuts,
            "loud": loud, "motion": motion, "events": events, "score": score}


# ---------- 2. Выбор моментов ----------

def _overlaps(c, used):
    return any(not (c["end"] <= a or c["start"] >= b) for a, b in used)


def choose(A: dict, st: dict, n: int, used=(), lengths=None) -> list[dict]:
    dur, segments, cuts = A["dur"], A["segments"], A["cuts"]
    content = st.get("content", "auto")
    lmin, lmax = lengths or lengths_for(st)
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
        seg_src, c = segments, dict(c)
        keep = _keep_intervals(c, segments, st, info)
        if keep:
            progress(f"✂️ Убираю паузы {i}/{len(clips)}…")
            try:
                cut = d / "cut.mp4"
                media.jumpcut(raw, keep, cut)
                cut.replace(raw)
                seg_src, c = _remap(c, segments, keep)
            except Exception as e:
                log.warning("jumpcut failed: %s", e)

        seg_in = [s for s in seg_src if s["e"] > c["start"] and s["s"] < c["end"]]
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
        lay = _game_layout(raw, c, info, st)
        subs.build_ass(d / "subs.ass", seg_in, c["start"], c["end"], c.get("title", ""), word_level,
                       overlay=review.overlay_words(inserts), mute=review.windows(inserts),
                       style=style_of(st, lay), labels=c.get("labels"))
        local_cuts = c.get("cuts") or ([] if "orig_start" in c else
                                       [x - c["start"] for x in cuts if c["start"] < x < c["end"]])
        out = _frame(raw, d, local_cuts, c["end"] - c["start"], info, st, lay)
        if inserts:
            mixed = d / "mixed.mp4"
            review.mix(out, inserts, mixed)
            out = mixed
        out = _finish(out, d, c, st, info, progress, i, len(clips))
        if st.get("censor") == "on" and info["audio"]:
            bad = [(w["s"] - c["start"], w["e"] - c["start"]) for sg in seg_in for w in sg.get("words", [])
                   if subs.SWEAR.search(w["w"])] if word_level else []
            if bad:
                try:
                    bp = d / "beep.mp4"
                    media.beep(out, bp, bad)
                    out = bp
                except Exception as e:
                    log.warning("beep failed: %s", e)
        out = _teaser(out, d, c, st, info)
        if st.get("loop", "auto") == "on" or (st.get("loop", "auto") == "auto" and st.get("content") in GAMES):
            try:
                lp = d / "loop.mp4"
                media.loopify(out, lp)
                out = lp
            except Exception as e:
                log.warning("loop failed: %s", e)
        if st.get("pbar", "auto") == "on" or (st.get("pbar", "auto") == "auto" and st.get("content") in GAMES):
            try:
                pb = d / "pbar.mp4"
                media.progress_bar(out, pb, media.probe(out)["duration"])
                out = pb
            except Exception as e:
                log.warning("progress bar failed: %s", e)
        final = d / f"short_{i}.mp4"
        out.replace(final)
        raw.unlink(missing_ok=True)
        r = {"path": final, "title": c.get("title", ""), "start": c.get("orig_start", c["start"]),
             "end": c.get("orig_end", c["end"]),
             "type": c.get("type", ""), "content": content, "index": i, "total": len(clips),
             "text": c.get("text", "")}
        if on_clip:
            on_clip(r)  # отправляем сразу и удаляем, чтобы не копить на диске
            final.unlink(missing_ok=True)
        results.append(r)
    return results


def _keep_intervals(c, segments, st, info):
    """Куски клипа без пауз (время от начала клипа). None — резать нечего."""
    mode = st.get("jumpcut", "auto")
    if mode == "off" or not info["audio"] or c.get("labels") or st.get("review") == "on":
        return None
    L = c["end"] - c["start"]
    words = sorted((max(0.0, w["s"] - c["start"]), min(L, w["e"] - c["start"]))
                   for s in segments for w in (s.get("words") or [])
                   if w["e"] > c["start"] and w["s"] < c["end"])
    speech = sum(b - a for a, b in words)
    if not words or (mode == "auto" and speech < L * 0.35):
        return None  # почти без речи — это экшен, не трогаем
    protect = [(t - 2.0, t + 2.0) for t in c.get("fx_t", [])]  # вокруг килов ничего не режем
    pad, gap_min = 0.15, 0.6
    keep, cur = [], [0.0, min(L, words[0][1] + pad)]
    for a, b in words[1:]:
        gap = (cur[1], a - pad)
        in_fx = any(not (gap[1] <= p or gap[0] >= q) for p, q in protect)
        if gap[1] - gap[0] >= gap_min and not in_fx:
            keep.append(tuple(cur))
            cur = [max(0.0, a - pad), min(L, b + pad)]
        else:
            cur[1] = min(L, max(cur[1], b + pad))
    cur[1] = L if L - cur[1] < 1.0 else cur[1] + 0.3
    keep.append(tuple(cur))
    removed = L - sum(b - a for a, b in keep)
    return keep if removed >= 1.0 else None


def _remap(c, segments, keep):
    """Пересчёт времени после вырезания пауз: клип начинается с 0."""
    def m(t):
        acc = 0.0
        for a, b in keep:
            if t <= a:
                return acc
            if t <= b:
                return acc + t - a
            acc += b - a
        return acc
    L = sum(b - a for a, b in keep)
    st0 = c["start"]
    segs = []
    for s in segments:
        if s["e"] <= c["start"] or s["s"] >= c["end"]:
            continue
        ws = [{**w, "s": m(w["s"] - st0), "e": m(w["e"] - st0)} for w in s.get("words") or []]
        ws = [w for w in ws if w["e"] > w["s"]]
        segs.append({**s, "s": m(s["s"] - st0), "e": m(s["e"] - st0), "words": ws})
    c2 = {**c, "orig_start": c["start"], "orig_end": c["end"], "start": 0.0, "end": L,
          "fx_t": [m(t) for t in c.get("fx_t", [])]}
    return segs, c2


def _teaser(out, d, c, st, info):
    mode = st.get("teaser", "auto")
    if mode == "off" or not info["audio"] or c.get("labels"):
        return out
    if mode == "auto" and st.get("content") not in GAMES:
        return out
    t = max(c.get("fx_t") or [0.0], default=0.0)
    if c.get("fx_t"):
        t = c["fx_t"][-1] if c.get("fx_big", 0) < 6 else c["fx_t"][0]
    if t < 4.0:  # яркий момент и так в начале
        return out
    try:
        dst = d / "teaser.mp4"
        media.teaser(out, t + 0.3, dst)
        return dst
    except Exception as e:
        log.warning("teaser failed: %s", e)
        return out


def _game_layout(raw, c, info, st):
    if st.get("mode") != "game" or info["w"] <= info["h"]:
        return None
    try:
        lay = gamecam.plan(raw, c["end"] - c["start"], info["w"], info["h"], st.get("content", "mlbb"),
                           use_ai=st.get("director", "on") == "on", events=c.get("fx_t", []),
                           bar=bool(st.get("mark") or st.get("camp_label")))
        log.info("gamecam: %s, ai=%s, shots=%d", lay["layout"], lay["ai"], len(lay["shots"]))
        return lay
    except Exception as e:
        log.warning("gamecam plan failed: %s", e)
        return None


def _frame(raw, d, local_cuts, dur, info, st, lay=None):
    out = d / "framed.mp4"
    if lay:
        try:
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

def compile_top(src: Path, workdir: Path, st: dict, A: dict, progress, k=5, piece=(6, 22)):
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
