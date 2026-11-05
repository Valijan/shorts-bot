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


COLORS = {"yellow": "&H00E5FF&", "green": "&H66FF33&", "cyan": "&HFFE500&", "pink": "&HB04DFF&"}
SIZES = {"s": 68, "m": 84, "l": 102}
# позиция: (выравнивание ASS, отступ по вертикали)
POS = {"bottom": (2, 560), "center": (5, 0), "top": (8, 430)}


_FONT = None


def font() -> str:
    """Жирный шрифт как у топовых шортсов (Montserrat Black), если установлен."""
    global _FONT
    if _FONT is None:
        import subprocess
        try:
            out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
        except Exception:
            out = ""
        _FONT = "Montserrat Black" if "Montserrat Black" in out else cfg.SUB_FONT
    return _FONT


def _header(style=None) -> str:
    st = style or {}
    f = font()
    size = SIZES.get(st.get("size", "m"), 84)
    if st.get("words") == 1:
        size = int(size * 1.3)
    al, mv = POS.get(st.get("pos", "bottom"), (2, 560))
    if st.get("sub_y") and st.get("pos", "bottom") == "bottom":
        mv = 1920 - st["sub_y"]
    hook_v = st.get("banner_y", 300) + 34
    bar_v = st.get("bar_y", 0) + 14
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{f},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,{al},70,70,{mv},1
Style: Title,{f},66,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,8,80,80,230,1
Style: Mark,{f},40,&H60FFFFFF,&H000000FF,&H80000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,60,60,90,1
Style: Hook,{f},90,&H00000000,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,1,0,0,8,50,50,{hook_v},1
Style: Box,{f},20,&H00FFFFFF,&H000000FF,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Bar,{f},56,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,8,40,40,{bar_v},1

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
              word_level: bool = True, upper: bool = True, overlay=None, mute=None,
              style=None, labels=None):
    """style: {color,size,pos,title(bool),mark}; labels: [(s,e,text)] — надписи-заголовки по времени."""
    st = style or {}
    hl = r"{\c" + COLORS.get(st.get("color", "yellow"), COLORS["yellow"]) + "}"
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
    show_title = st.get("title", "hook")
    if show_title in (True, "on") and title and not labels:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Title,,0,0,0,,{_esc(title)}")
    hook_end = 0.0
    elif_hook = show_title == "hook" and title and not labels
    if elif_hook and not show_title in (True, "on"):
        hook_end = min(dur, 4.5)
        y = st.get("banner_y", 300)
        txt = hook_text(title)
        h = 280 if "\\N" in txt else 170
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(hook_end)},Box,,0,0,0,,"
                     f"{{\\pos(0,{y})\\p1}}m 0 0 l 1080 0 l 1080 {h} l 0 {h}{{\\p0}}")
        lines.append(f"Dialogue: 3,{_ts(0)},{_ts(hook_end)},Hook,,0,0,0,,{txt}")
    for a, b, text in labels or []:
        lines.append(f"Dialogue: 1,{_ts(a)},{_ts(b)},Title,,0,0,0,,{_esc(text)}")
    mark = st.get("mark") or cfg.WATERMARK
    if mark and st.get("bar_y"):  # полоса с ником под веб-камерой
        nick = _esc(mark.lstrip("@").upper())
        lines.append(f"Dialogue: 1,{_ts(hook_end)},{_ts(dur)},Bar,,0,0,0,,{{\\c&HFF4D9F&}}▶ {{\\c&HFFFFFF&}}{nick}")
    elif mark:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Mark,,0,0,0,,{_esc(mark)}")

    one = st.get("words") == 1
    chunks = [[w] for w in words] if one else _chunks_from_words(words)
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else dur
        ch_end = min(nxt, ch[-1]["e"] + 0.35)
        texts = [_esc(w["w"].upper() if upper else w["w"]) for w in ch]
        for wi, w in enumerate(ch):
            w_end = ch[wi + 1]["s"] if wi + 1 < len(ch) else ch_end
            if w_end <= w["s"]:
                continue
            parts = texts if one else [f"{hl}{t}{RESET}" if i == wi else t for i, t in enumerate(texts)]
            lines.append(f"Dialogue: 0,{_ts(w['s'])},{_ts(w_end)},Sub,,0,0,0,,{' '.join(parts)}")
    path.write_text(_header(st) + "\n".join(lines) + "\n", encoding="utf-8")


def hook_text(title: str) -> str:
    """Плашка-крючок: 2 строки, главное слово красным (*слово* или самое длинное)."""
    t = _esc(title.strip()).upper()
    marked = None
    if "*" in t:
        parts = t.split("*")
        if len(parts) >= 3:
            marked = parts[1].strip()
        t = t.replace("*", "")
    words = t.split()
    if not words:
        return ""
    if not marked:
        marked = max(words, key=lambda w: len(w.strip("?!.,")))
    # перенос на 2 строки по середине длины
    half, acc, cut = len(t) / 2, 0, len(words)
    for i, w in enumerate(words):
        acc += len(w) + 1
        if acc >= half:
            cut = i + 1
            break
    out = []
    for i, w in enumerate(words):
        word = f"{{\\c&H2020E8&}}{w}{{\\c&H000000&}}" if w.strip("?!.,") == marked.strip("?!.,") else w
        out.append(word + ("\\N" if i + 1 == cut and cut < len(words) else " "))
    return "".join(out).strip()
