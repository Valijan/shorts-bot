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

VOCAB = {
    "mlbb": "Mobile Legends, MLBB, Savage, Maniac, Triple Kill, Double Kill, Wiped Out, Shut Down, "
            "Лорд, Черепаха, ульта, гэнк, лес, роум, мид, голд, фарм, Фанни, Лин, Госу, Ланселот, "
            "Хаябуса, Чоу, Лейла, Мия, Алукард, Кагура, Бенедетта, Джулиан, эмблема, сборка",
    "pubg": "PUBG Mobile, нокнул, хэдшот, клатч, вайп, сквад, AWM, M416, Kar98, лут, ред зона, "
            "Erangel, Miramar, Chicken Dinner, топ 1",
}


def transcribe(path, progress=None, content: str = "auto") -> dict:
    """GPU, а если видеокарта не поддерживается — автоматически CPU."""
    global _whisper
    try:
        return _transcribe(path, progress, content)
    except Exception as e:
        if cfg.WHISPER_DEVICE == "cpu":
            raise
        log.warning("GPU whisper failed (%s), switching to CPU", e)
        cfg.WHISPER_DEVICE, cfg.WHISPER_COMPUTE, _whisper = "cpu", "int8", None
        if cfg.WHISPER_MODEL.startswith("large"):
            cfg.WHISPER_MODEL = "small"
        return _transcribe(path, progress, content)


def _load_audio(path) -> np.ndarray:
    """Звук 16 кГц моно через ffmpeg (без PyAV — у него ломается совместимость)."""
    import subprocess

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "s16le", "-"], capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _transcribe(path, progress=None, content: str = "auto") -> dict:
    global _whisper
    from faster_whisper import WhisperModel

    if _whisper is None:
        _whisper = WhisperModel(
            cfg.WHISPER_MODEL, device=cfg.WHISPER_DEVICE, compute_type=cfg.WHISPER_COMPUTE,
            download_root=str(cfg.DATA_DIR / "models"),
        )
    audio = _load_audio(path)
    vocab = VOCAB.get(content)
    extra = {"initial_prompt": vocab, "hotwords": vocab} if vocab else {}
    try:
        segments, info = _whisper.transcribe(
            audio, word_timestamps=True, vad_filter=True, beam_size=1, **extra)
    except TypeError:  # старая faster-whisper без hotwords
        segments, info = _whisper.transcribe(
            audio, word_timestamps=True, vad_filter=True, beam_size=1,
            **({"initial_prompt": vocab} if vocab else {}))
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


GOAL = {
    "auto": """Выбери до {n} лучших фрагментов для вертикальных шортсов. Требования:""",
    "movie": """Это ФИЛЬМ или сериал. Выбери до {n} самых сильных сцен для шортсов:
культовые фразы, кульминации, драки/погони, сюжетные повороты, смешные и трогательные моменты.
Не бери начальные логотипы и финальные титры. Сцена должна быть законченной.
Требования:""",
    "mlbb": """Это геймплей Mobile Legends: Bang Bang. Ищи до {n} моментов трёх типов:
 kill — убийства, особенно мульти-киллы (Double/Triple Kill, Maniac, Savage), Wiped Out, Shut Down,
        выигранные тимфайты, кражи Лорда/Черепахи, камбэки;
 tip — стример объясняет фишку: сборку, эмблемы, комбо, позиционирование, как контрить;
 funny — смешные моменты: реакция, фейлы, шутки, троллинг, неожиданности.
Клип начинается за 2–4 с до начала драки и заканчивается сразу после последнего килла/панчлайна.
Требования:""",
    "pubg": """Это геймплей PUBG Mobile. Ищи до {n} моментов трёх типов:
 kill — убийства и нокауты, хэдшоты, сквад-вайпы, клатчи 1 против нескольких, Winner Winner Chicken Dinner;
 tip — стример объясняет фишку: раскладка, чувствительность, отдача, ротации, лут, тактика;
 funny — смешные моменты: реакция, фейлы, троллинг, неожиданности.
Клип начинается за 2–4 с до перестрелки и заканчивается сразу после последнего килла/панчлайна.
Требования:""",
}


def pick_llm(segments, n, lmin, lmax, title_lang, content="auto", signals="") -> list[dict]:
    lines = [f"[{s['s']:.1f}-{s['e']:.1f}] {s['t']}" for s in segments if s["t"]]
    transcript = "\n".join(lines) or "(речи почти нет — опирайся на сигналы)"
    if len(transcript) > 400_000:  # очень длинное видео — берём равномерно
        k = len(transcript) / 400_000
        transcript = "\n".join(lines[:: int(np.ceil(k))])
    goal = GOAL.get(content, GOAL["auto"]).format(n=n)
    sig = f"\n\nСИГНАЛЫ (секунда: громкость/действие/события, выше = ярче):\n{signals}" if signals else ""
    prompt = f"""Ты — опытный монтажёр YouTube Shorts. Ниже расшифровка видео с таймкодами в секундах.

{goal}
- длина каждого фрагмента от {lmin} до {lmax} секунд;
- фрагмент понятен без контекста, начинается с сильного «крючка» (первые 2–3 секунды цепляют);
- кульминация, эмоция, юмор, конфликт, неожиданный поворот или ценная мысль;
- не обрывать на середине фразы: начало = начало реплики, конец = конец реплики;
- фрагменты не пересекаются; сортируй от самого сильного к слабому.

Для каждого дай заголовок-крючок для плашки в начале ролика ({LANG_NAMES.get(title_lang, 'русский')} язык,
до 40 символов, лучше вопрос или интрига, без хэштегов; самое сильное слово выдели звёздочками, напр. «iPhone *худшая* платформа?»)
и тип: kill, tip, funny или best.

Ответь ТОЛЬКО JSON без пояснений:
{{"clips":[{{"start":12.3,"end":48.0,"title":"...","type":"best","score":9}}]}}

РАСШИФРОВКА:
{transcript}{sig}"""
    data = _json(_claude(prompt)) or {}
    clips = []
    for c in data.get("clips", []):
        try:
            clips.append({"start": float(c["start"]), "end": float(c["end"]),
                          "title": str(c.get("title", ""))[:60], "score": float(c.get("score", 5)),
                          "type": str(c.get("type", "best"))})
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


# ---------- Игры: события по словам ----------

# (регулярка, вес, тип, подпись)
_EV = {
    "mlbb": [
        (r"savage|сэвидж|севидж|сав[еэ]дж", 10, "kill", "SAVAGE!"),
        (r"maniac|маньяк", 8, "kill", "MANIAC!"),
        (r"wiped? ?out|вайп", 6, "kill", "WIPED OUT"),
        (r"triple ?kill|трипл", 6, "kill", "TRIPLE KILL"),
        (r"legendary|godlike|unstoppable|rampage|легендар", 4, "kill", "LEGENDARY"),
        (r"double ?kill|дабл", 4, "kill", "DOUBLE KILL"),
        (r"shut ?down|шат ?даун", 3, "kill", "SHUT DOWN"),
        (r"first blood|перв\w* кровь", 3, "kill", "FIRST BLOOD"),
        (r"\blord\b|лорд|turtle|черепах", 2, "kill", "Лорд"),
        (r"victory|побед", 2, "kill", "VICTORY"),
    ],
    "pubg": [
        (r"winner|chicken|чикен|топ ?1\b|топ[- ]один", 8, "kill", "CHICKEN DINNER"),
        (r"squad ?wipe|вайп|всех убил|выкосил|клатч|clutch|\bace\b|эйс", 7, "kill", "CLUTCH"),
        (r"head ?shot|хэд ?шот|хедшот|в голову|ван ?шот|one ?shot", 4, "kill", "HEADSHOT"),
        (r"нокнул|кнокнул|knock|упал|минус|убил|killed|kill", 3, "kill", "KILL"),
    ],
}
_COMMON = [
    (r"ах[аx]+|ха-?ха|хах|лол|\blol\b|ору|угар|ржу|смех|laugh|хи-?хи", 4, "funny", "😂"),
    (r"капец|жесть|офиг|бля|блин|what the|wtf|ничего себе|вау|wow|omg", 2, "funny", "😱"),
    (r"совет|лайфхак|фишк|запомни|секрет|сборк|билд|эмблем|комбо|combo|\btip|trick|"
     r"чувствительн|раскладк|отдач", 3, "tip", "💡"),
]


ANNOUNCER_EN = re.compile(r"savage|maniac|triple|double|wiped|shut ?down|first blood|legendary|godlike|"
                          r"unstoppable|rampage|winner|chicken", re.I)


def game_events(segments, content: str) -> list[dict]:
    rules = _EV.get(content, []) + _COMMON
    out = []
    for seg in segments:
        n_words = len(seg["t"].split())
        items = seg.get("words") or [{"s": seg["s"], "e": seg["e"], "w": seg["t"]}]
        text_hits = set()
        for w in items:
            low = w["w"].lower()
            for rx, wt, kind, label in rules:
                if label not in text_hits and re.search(rx, low):
                    text_hits.add(label)
                    # диктор игры говорит коротко и по-английски; русское слово в длинной фразе —
                    # это стример обсуждает («маньяка не дали»), а не сам момент
                    if kind == "kill" and (n_words > 4 or not ANNOUNCER_EN.search(low)):
                        out.append({"t": float(w["s"]), "w": wt * 0.3, "kind": "talk", "label": label})
                    else:
                        out.append({"t": float(w["s"]), "w": wt, "kind": kind, "label": label})
        low = seg["t"].lower()  # фразы из нескольких слов («double kill»)
        for rx, wt, kind, label in rules:
            if label not in text_hits and re.search(rx, low):
                strong = kind != "kill" or (n_words <= 4 and ANNOUNCER_EN.search(low))
                out.append({"t": float(seg["s"]), "w": wt if strong else wt * 0.3,
                            "kind": kind if strong else "talk", "label": label})
    return out


def _z(a, n):
    a = np.resize(np.asarray(a, dtype=float), n) if len(a) else np.zeros(n)
    return np.clip((a - np.median(a)) / (np.std(a) + 1e-6), -2, 4)


def activity(duration, loud, motion, events) -> np.ndarray:
    """Яркость каждой секунды: громкость + действие на экране + события."""
    n = int(duration) + 1
    sc = 0.9 * _z(loud, n) + 0.7 * _z(motion, n)
    k = np.exp(-0.5 * (np.arange(-6, 7) / 2.5) ** 2)
    ev = np.zeros(n)
    for e in events:
        i = min(int(e["t"]), n - 1)
        ev[i] += e["w"]
    sc += np.convolve(ev, k, mode="same") / 2
    return sc


def signals_text(score, events, limit=60) -> str:
    """Краткая сводка для ИИ: самые яркие секунды и найденные события."""
    top = sorted(np.argsort(score)[-limit:])
    lines = [f"{t}s: {score[t]:.1f}" for t in top]
    lines += [f"{e['t']:.0f}s: событие {e['label']} ({e['kind']})" for e in events[:150]]
    return "\n".join(lines)


MULTI = {"DOUBLE KILL": 2, "TRIPLE KILL": 3, "MANIAC!": 4, "SAVAGE!": 5, "WIPED OUT": 5,
         "LEGENDARY": 3, "CLUTCH": 4, "CHICKEN DINNER": 3}
LOOKBACK = {1: 4, 2: 7, 3: 10, 4: 14, 5: 18}  # сколько секунд обычно длится серия из N килов


def chain_span(events, ev):
    """Начало и конец всей серии килов, которая закончилась событием ev."""
    n = MULTI.get(ev["label"], 1)
    start = ev["t"] - LOOKBACK.get(n, 4)
    kills = sorted((e["t"] for e in events if e["kind"] == "kill" and e["t"] < ev["t"] - 0.3), reverse=True)
    first = ev["t"]
    for t in kills:  # предыдущие килы этой же серии (паузы < 12 с)
        if first - t < 12:
            first = t
        else:
            break
    return max(0.0, min(start, first - 2.5)), ev["t"] + 2.5


def pick_game(duration, score, events, n, lmin, lmax) -> list[dict]:
    """Без ИИ: сначала серии килов целиком (от первого до последнего), потом пики экшена."""
    L = int(min(lmax, max(lmin, (lmin + lmax) / 2)))
    if duration <= L + 1:
        return [{"start": 0.0, "end": duration, "title": "", "score": 5, "type": "kill"}]
    picked = []

    def free(a, b):
        return all(b <= p["start"] or a >= p["end"] for p in picked)

    kills = sorted((e for e in events if e["kind"] == "kill"), key=lambda e: -e["w"])
    for ev in kills:
        a, b = chain_span(events, ev)
        if b - a < lmin:  # короткая — добираем контекст до минимума
            pad = (lmin - (b - a)) / 2
            a, b = a - pad, b + pad
        if b - a > lmax:  # слишком длинная — держим финал серии
            a = b - lmax
        a, b = max(0.0, a), min(duration, b)
        if b - a >= min(lmin, 5) and free(a, b):
            picked.append({"start": float(a), "end": float(b), "title": ev["label"],
                           "score": float(ev["w"]) + 10, "type": "kill"})
        if len(picked) >= n:
            return picked

    sm = np.convolve(score, np.ones(5) / 5, mode="same")
    for peak in np.argsort(sm)[::-1]:
        peak = int(peak)
        s = max(0, peak - int(L * 0.45))
        e = min(duration, s + L)
        if not free(s, e):
            continue
        inside = [x for x in events if s <= x["t"] <= e and x["kind"] != "talk"]
        best = max(inside, key=lambda x: (x["w"], x["kind"] == "kill")) if inside else None
        picked.append({"start": float(s), "end": float(e),
                       "title": best["label"] if best and best["kind"] == "kill" else "",
                       "score": float(sm[peak]), "type": best["kind"] if best else "kill"})
        if len(picked) >= n:
            break
    return picked


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


# ---------- Текст для публикации ----------

_BASE_TAGS = {
    "mlbb": "#mlbb #mobilelegends #mobilelegendsbangbang #mlbbshorts #shorts",
    "pubg": "#pubgmobile #pubg #pubgm #pubgshorts #shorts",
    "movie": "#фильм #кино #моменты #shorts",
    "auto": "#shorts #viral",
}
_TYPE_WORD = {"kill": "килл-момент", "tip": "полезный совет", "funny": "смешной момент"}


def publish_text(clip: dict, segments, content: str, lang: str = "ru", must_tags: str = "") -> dict:
    """Название, описание и хэштеги для YouTube Shorts / TikTok / Reels."""
    res = _publish_text(clip, segments, content, lang)
    if must_tags:  # обязательные теги кампании — всегда первыми
        rest = [t for t in res["tags"].split() if t.lower() not in must_tags.lower().split()]
        res["tags"] = " ".join(must_tags.split() + rest)
    return res


def _publish_text(clip: dict, segments, content: str, lang: str = "ru") -> dict:
    tags = _BASE_TAGS.get(content, _BASE_TAGS["auto"])
    fallback_title = clip.get("title") or {"mlbb": "Этот момент в MLBB 🔥", "pubg": "Жёсткий момент в PUBG Mobile",
                                           "movie": "Лучший момент фильма"}.get(content, "Смотри до конца 👀")
    base = {"title": fallback_title[:90],
            "desc": f"{_TYPE_WORD.get(clip.get('type'), 'лучший момент').capitalize()}. "
                    f"Подписывайся, чтобы не пропустить новые! 🔔",
            "tags": tags}
    if not cfg.ANTHROPIC_API_KEY:
        return base
    said = " ".join(s["t"] for s in segments
                    if s["e"] > clip["start"] and s["s"] < clip["end"])[:1500]
    prompt = f"""Ты SMM-специалист. Напиши текст для публикации короткого вертикального видео
(YouTube Shorts, TikTok, Reels). Тематика: {content}. Тип момента: {clip.get('type', '')}.
Рабочий заголовок: {clip.get('title', '')}. Что звучит в ролике: {said or '(почти без речи)'}
Язык: {LANG_NAMES.get(lang, 'русский')}.
Ответь ТОЛЬКО JSON: {{"title":"до 70 символов, цепляет, 1 эмодзи","desc":"1–2 предложения + призыв подписаться",
"tags":"8–12 хэштегов через пробел, популярные и нишевые, обязательно #shorts"}}"""
    try:
        data = _json(_claude(prompt, 600)) or {}
        return {"title": str(data.get("title") or base["title"])[:100],
                "desc": str(data.get("desc") or base["desc"])[:500],
                "tags": str(data.get("tags") or tags)[:300]}
    except Exception as e:
        log.warning("publish_text failed: %s", e)
        return base



def vision_events(src, score, content: str, k: int = 36) -> list[dict]:
    """ИИ смотрит кадры в самые яркие секунды и находит баннеры килов на экране."""
    import base64
    import subprocess

    if not cfg.ANTHROPIC_API_KEY or len(score) < 5:
        return []
    order = [int(t) for t in np.argsort(score)[::-1]]
    picks = []
    for t in order:
        if all(abs(t - p) >= 4 for p in picks):
            picks.append(t)
        if len(picks) >= k:
            break
    picks.sort()
    blocks = []
    for i, t in enumerate(picks):
        img = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t + 0.5:.2f}", "-i", str(src),
                              "-frames:v", "1", "-vf", "scale=512:-2", "-f", "image2", "-c:v", "mjpeg",
                              "-q:v", "6", "-"], capture_output=True).stdout
        if img:
            blocks += [{"type": "text", "text": f"Кадр {i} (t={t}s)"},
                       {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(img).decode()}}]
    if not blocks:
        return []
    game = {"mlbb": "Mobile Legends: Bang Bang", "pubg": "PUBG Mobile"}.get(content, content)
    blocks.append({"type": "text", "text": f"""Это кадры геймплея {game}.
Для каждого кадра определи, есть ли на экране баннер/надпись убийства (в MLBB: «Убил», Double/Triple Kill,
Maniac, Savage, Wiped Out, Shut Down; в PUBG: «вы убили/нокаут», kill feed с вашим ником).
Ответь ТОЛЬКО JSON: {{"kills":[{{"i":0,"label":"SAVAGE","w":10}}]}} — только кадры с убийством;
w: 3 обычный кил, 4 double, 6 triple, 8 maniac, 10 savage, 7 wiped out/клатч."""})
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    msg = client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=1500,
                                 messages=[{"role": "user", "content": blocks}])
    data = _json("".join(b.text for b in msg.content if getattr(b, "type", "") == "text")) or {}
    out = []
    for e in data.get("kills", []):
        try:
            i = int(e["i"])
            out.append({"t": float(picks[i]) + 0.5, "w": float(e.get("w", 3)), "kind": "kill",
                        "label": str(e.get("label", "KILL"))[:20].upper()})
        except (KeyError, ValueError, IndexError, TypeError):
            continue
    return out
