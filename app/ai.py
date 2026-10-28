"""Распознавание речи (Whisper), выбор моментов (Claude или алгоритм), перевод."""
import json
import logging
import re

import numpy as np

from . import config as cfg

log = logging.getLogger(__name__)
_whisper = None

LANG_NAMES = {"ru": "русский", "uz": "узбекский (латиница)", "en": "английский"}


# ---------- Речь ----------

def transcribe(path, progress=None) -> dict:
    """GPU, а если видеокарта не поддерживается — автоматически CPU."""
    global _whisper
    try:
        return _transcribe(path, progress)
    except Exception as e:
        if cfg.WHISPER_DEVICE == "cpu":
            raise
        log.warning("GPU whisper failed (%s), switching to CPU", e)
        cfg.WHISPER_DEVICE, cfg.WHISPER_COMPUTE, _whisper = "cpu", "int8", None
        if cfg.WHISPER_MODEL.startswith("large"):
            cfg.WHISPER_MODEL = "small"
        return _transcribe(path, progress)


def _load_audio(path) -> np.ndarray:
    """Звук 16 кГц моно через ffmpeg (без PyAV — у него ломается совместимость)."""
    import subprocess

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "s16le", "-"], capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _transcribe(path, progress=None) -> dict:
    global _whisper
    from faster_whisper import WhisperModel

    if _whisper is None:
        _whisper = WhisperModel(
            cfg.WHISPER_MODEL, device=cfg.WHISPER_DEVICE, compute_type=cfg.WHISPER_COMPUTE,
            download_root=str(cfg.DATA_DIR / "models"),
        )
    audio = _load_audio(path)
    segments, info = _whisper.transcribe(
        audio, word_timestamps=True, vad_filter=True, beam_size=1,
    )
    out = []
    for s in segments:
        words = [{"s": w.start, "e": w.end, "w": w.word.strip()} for w in (s.words or []) if w.word.strip()]
        out.append({"s": s.start, "e": s.end, "t": s.text.strip(), "words": words})
        if progress and info.duration:
            progress(min(s.end / info.duration, 1.0))
    return {"lang": info.language, "segments": out}


# ---------- Claude ----------

def _claude(prompt: str, max_tokens: int = 4000) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=cfg.CLAUDE_MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _json(text: str):
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    return json.loads(m.group(0)) if m else None


def pick_llm(segments, n, lmin, lmax, title_lang) -> list[dict]:
    lines = [f"[{s['s']:.1f}-{s['e']:.1f}] {s['t']}" for s in segments if s["t"]]
    transcript = "\n".join(lines)
    if len(transcript) > 400_000:  # очень длинное видео — берём равномерно
        k = len(transcript) / 400_000
        transcript = "\n".join(lines[:: int(np.ceil(k))])
    prompt = f"""Ты — опытный монтажёр YouTube Shorts. Ниже расшифровка видео с таймкодами в секундах.

Выбери до {n} лучших фрагментов для вертикальных шортсов. Требования:
- длина каждого фрагмента от {lmin} до {lmax} секунд;
- фрагмент понятен без контекста, начинается с сильного «крючка» (первые 2–3 секунды цепляют);
- кульминация, эмоция, юмор, конфликт, неожиданный поворот или ценная мысль;
- не обрывать на середине фразы: начало = начало реплики, конец = конец реплики;
- фрагменты не пересекаются; сортируй от самого сильного к слабому.

Для каждого дай цепляющий заголовок ({LANG_NAMES.get(title_lang, 'русский')} язык, до 45 символов, без кавычек и хэштегов).

Ответь ТОЛЬКО JSON без пояснений:
{{"clips":[{{"start":12.3,"end":48.0,"title":"...","score":9}}]}}

РАСШИФРОВКА:
{transcript}"""
    data = _json(_claude(prompt)) or {}
    clips = []
    for c in data.get("clips", []):
        try:
            clips.append({"start": float(c["start"]), "end": float(c["end"]),
                          "title": str(c.get("title", ""))[:60], "score": float(c.get("score", 5))})
        except (KeyError, ValueError, TypeError):
            continue
    return clips


def translate(segments, target: str) -> list[dict]:
    """Переводит тексты сегментов, тайминги сохраняются."""
    out = []
    for i in range(0, len(segments), 60):
        batch = segments[i:i + 60]
        payload = {str(j): s["t"] for j, s in enumerate(batch)}
        prompt = (
            f"Переведи реплики на {LANG_NAMES[target]} язык. Живо и коротко, как субтитры. "
            f"Сохрани ключи. Ответь ТОЛЬКО JSON-объектом.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        res = _json(_claude(prompt)) or {}
        for j, s in enumerate(batch):
            out.append({**s, "t": str(res.get(str(j), s["t"])), "words": []})
    return out


# ---------- Алгоритм без ИИ ----------

def pick_heuristic(duration, loud, cuts, segments, n, lmin, lmax) -> list[dict]:
    L = int(min(lmax, max(lmin, (lmin + lmax) / 2)))
    if duration <= L + 1:
        return [{"start": 0.0, "end": duration, "title": "", "score": 5}]
    secs = int(duration) + 1
    loud = np.resize(loud, secs) if len(loud) else np.zeros(secs)
    z = (loud - np.median(loud)) / (np.std(loud) + 1e-6)
    speech = np.zeros(secs)
    for s in segments:
        for w in s.get("words", []) or [s]:
            a, b = int(w["s"]), int(w["e"]) + 1
            speech[a:b] = 1
    cut = np.zeros(secs)
    for c in cuts:
        cut[min(int(c), secs - 1)] += 1

    # у длинных видео пропускаем начало и титры
    lo = int(duration * 0.02) if duration > 600 else 0
    hi = int(duration * 0.92) if duration > 600 else int(duration)
    cands = []
    for st in range(lo, max(lo + 1, hi - L), 3):
        sl = slice(st, st + L)
        score = (np.mean(np.clip(z[sl], -2, 3))
                 + 0.6 * np.percentile(z[sl], 90) / 3
                 + 0.8 * np.mean(speech[sl])
                 + 0.5 * min(cut[sl].sum() / (L / 4), 1.5))
        cands.append((score, st))
    cands.sort(reverse=True)
    picked = []
    for score, st in cands:
        if all(abs(st - p["start"]) >= L + 5 for p in picked):
            picked.append({"start": float(st), "end": float(st + L), "title": "", "score": float(score)})
        if len(picked) >= n:
            break
    return picked


# ---------- Подгонка границ ----------

def snap(clips, segments, cuts, duration, lmin, lmax) -> list[dict]:
    """Двигает границы к началу/концу реплик и склейкам, чтобы не резать слова."""
    starts = sorted({s["s"] for s in segments} | set(cuts))
    ends = sorted({s["e"] for s in segments} | set(cuts))
    out = []
    for c in clips:
        s, e = max(0.0, c["start"]), min(duration, c["end"])
        near_s = [x for x in starts if abs(x - s) <= 3]
        if near_s:
            s = min(near_s, key=lambda x: abs(x - s))
        near_e = [x for x in ends if abs(x - e) <= 4 and lmin <= x - s <= lmax]
        if near_e:
            e = min(near_e, key=lambda x: abs(x - e)) + 0.25
        e = min(duration, max(e, s + min(lmin, duration - s)), s + lmax)
        if e - s >= 5:
            out.append({**c, "start": max(0.0, s - 0.1), "end": e})
    # убираем пересечения
    out.sort(key=lambda c: -c.get("score", 0))
    final = []
    for c in out:
        if all(c["end"] <= f["start"] or c["start"] >= f["end"] for f in final):
            final.append(c)
    return final


def split_sequential(duration, segments, cuts, lmin, lmax) -> list[dict]:
    """Режет видео подряд на части ~lmax секунд, конец части — на паузе в речи."""
    ends = sorted({s["e"] for s in segments} | set(cuts))
    words = [(w["s"], w["e"]) for s in segments for w in (s.get("words") or [])]

    def inside_word(t):
        return any(a < t < b for a, b in words)

    out, t, i = [], 0.0, 1
    while duration - t > 3:
        target = t + lmax
        if target >= duration:
            e = duration
        else:
            cands = [x for x in ends if t + lmin <= x <= target]
            e = max(cands) if cands else target
            if not cands and inside_word(e):  # нет паузы — режем перед словом
                starts = [a for a, b in words if t + lmin <= a <= target]
                e = max(starts) if starts else target
        out.append({"start": t, "end": e, "title": f"Часть {i}", "score": 0})
        t, i = e, i + 1
    return out
