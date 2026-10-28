"""Генерация субтитров ASS в стиле Shorts: крупно, 2–3 слова, текущее слово подсвечено."""
from . import config as cfg

HL = r"{\c&H00E5FF&}"  # жёлто-оранжевый (BGR)
RESET = r"{\c&HFFFFFF&}"


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _header() -> str:
    f = cfg.SUB_FONT
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{f},84,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,560,1
Style: Title,{f},66,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,8,80,80,230,1
Style: Mark,{f},40,&H60FFFFFF,&H000000FF,&H80000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,60,60,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _chunks_from_words(words, max_words=3, max_chars=16):
    chunks, cur = [], []
    for w in words:
        gap = cur and w["s"] - cur[-1]["e"] > 0.6
        long = cur and (len(cur) >= max_words or sum(len(x["w"]) + 1 for x in cur) + len(w["w"]) > max_chars)
        if gap or long:
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w["w"][-1:] in ".!?,…":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _words_from_segments(segments, max_words=3):
    """Для переведённого текста таймингов слов нет — распределяем по длине."""
    out = []
    for seg in segments:
        tokens = seg["t"].split()
        if not tokens:
            continue
        total = sum(len(t) + 1 for t in tokens)
        t = seg["s"]
        dur = seg["e"] - seg["s"]
        for tok in tokens:
            d = dur * (len(tok) + 1) / total
            out.append({"s": t, "e": t + d, "w": tok})
            t += d
    return out


def build_ass(path, segments, start: float, end: float, title: str = "",
              word_level: bool = True, upper: bool = True, overlay=None, mute=None):
    """segments — сегменты с абсолютным временем; start/end — границы клипа."""
    dur = end - start
    if word_level:
        words = [w for s in segments for w in s.get("words", [])]
    else:
        words = _words_from_segments(segments)
    words = [
        {"s": max(0.0, w["s"] - start), "e": min(dur, w["e"] - start), "w": w["w"].strip()}
        for w in words if w["e"] > start and w["s"] < end and w["w"].strip()
    ]
    if mute:  # во время озвучки показываем её текст вместо оригинальных реплик
        words = [w for w in words if not any(a < (w["s"] + w["e"]) / 2 < b for a, b in mute)]
    if overlay:
        words = sorted(words + [dict(w, voice=True) for w in overlay], key=lambda w: w["s"])
    lines = []
    if cfg.SHOW_TITLE and title:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Title,,0,0,0,,{_esc(title)}")
    if cfg.WATERMARK:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Mark,,0,0,0,,{_esc(cfg.WATERMARK)}")

    chunks = _chunks_from_words(words)
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else dur
        ch_end = min(nxt, ch[-1]["e"] + 0.35)
        texts = [_esc(w["w"].upper() if upper else w["w"]) for w in ch]
        for wi, w in enumerate(ch):
            w_end = ch[wi + 1]["s"] if wi + 1 < len(ch) else ch_end
            if w_end <= w["s"]:
                continue
            parts = [f"{HL}{t}{RESET}" if i == wi else t for i, t in enumerate(texts)]
            lines.append(f"Dialogue: 0,{_ts(w['s'])},{_ts(w_end)},Sub,,0,0,0,,{' '.join(parts)}")
    path.write_text(_header() + "\n".join(lines) + "\n", encoding="utf-8")
