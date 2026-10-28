"""Режим «Обзор»: ИИ-комментарий к фрагменту + озвучка + приглушение оригинала."""
import asyncio
import logging
import subprocess
from pathlib import Path

from . import ai
from . import config as cfg

log = logging.getLogger(__name__)

VOICES = {
    "ru": "ru-RU-DmitryNeural",
    "uz": "uz-UZ-SardorNeural",
    "en": "en-US-GuyNeural",
}
DUCK = 0.12  # громкость оригинала под голосом


def write_text(segments, start, end, lang) -> dict:
    """{"intro": "...", "outro": "..."} — короткий комментарий от лица автора канала."""
    text = " ".join(s["t"] for s in segments if s["e"] > start and s["s"] < end).strip()
    prompt = f"""Ты — автор канала с короткими обзорами и разборами видео. Вот реплики из фрагмента ({end - start:.0f} сек):

«{text[:4000] or '(без речи — опирайся на то, что это яркая сцена)'}»

Напиши ОЗВУЧКУ от первого лица на языке: {ai.LANG_NAMES.get(lang, 'русский')}.
- intro: цепляющая подводка, которая объясняет контекст или интригует (до 15 слов). Не пересказывай фрагмент дословно.
- outro: твоё мнение или вывод + вопрос зрителю для комментариев (до 15 слов).
Живая разговорная речь, без хэштегов и эмодзи.
Ответь ТОЛЬКО JSON: {{"intro":"...","outro":"..."}}"""
    data = ai._json(ai._claude(prompt, 400)) or {}
    return {"intro": str(data.get("intro", "")).strip(), "outro": str(data.get("outro", "")).strip()}


async def _tts(text: str, voice: str, out: Path):
    import edge_tts

    comm = edge_tts.Communicate(text, voice, rate="+8%", boundary="WordBoundary")
    words = []
    with open(out, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                s = chunk["offset"] / 1e7
                words.append({"s": s, "e": s + chunk["duration"] / 1e7, "w": chunk["text"]})
    return words


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    return float(out.strip() or 0)


def speak(text: str, lang: str, out: Path):
    """Озвучка -> (длительность, слова с таймингами относительно начала озвучки)."""
    words = asyncio.run(_tts(text, VOICES.get(lang, VOICES["ru"]), out))
    dur = _duration(out)
    if not words:  # нет таймингов — распределяем по длине слов
        toks, t = text.split(), 0.0
        total = sum(len(x) + 1 for x in toks) or 1
        for x in toks:
            d = dur * (len(x) + 1) / total
            words.append({"s": t, "e": t + d, "w": x})
            t += d
    return dur, words


def prepare(segments, start, end, lang, workdir: Path):
    """Готовит озвучку для клипа. Возвращает список вставок:
    [{"at": сек от начала клипа, "dur": сек, "file": Path, "words": [...]}]"""
    clip_dur = end - start
    texts = write_text(segments, start, end, lang)
    inserts = []
    if texts["intro"]:
        f = workdir / "intro.mp3"
        d, w = speak(texts["intro"], lang, f)
        inserts.append({"at": 0.3, "dur": d, "file": f, "words": w})
    if texts["outro"]:
        f = workdir / "outro.mp3"
        d, w = speak(texts["outro"], lang, f)
        used = sum(i["dur"] for i in inserts)
        if used + d < clip_dur * 0.6:  # озвучка не должна съедать весь клип
            inserts.append({"at": max(0.0, clip_dur - d - 0.4), "dur": d, "file": f, "words": w})
    return inserts


def overlay_words(inserts):
    """Слова озвучки во времени клипа — для субтитров."""
    return [{"s": i["at"] + w["s"], "e": i["at"] + w["e"], "w": w["w"]}
            for i in inserts for w in i["words"]]


def windows(inserts):
    return [(i["at"] - 0.15, i["at"] + i["dur"] + 0.15) for i in inserts]


def mix(video: Path, inserts, out: Path):
    """Подмешивает озвучку и приглушает оригинал под ней (видео не перекодируется)."""
    if not inserts:
        video.rename(out)
        return
    cond = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in windows(inserts))
    args, parts, labels = ["-i", str(video)], [], []
    for n, ins in enumerate(inserts, 1):
        args += ["-i", str(ins["file"])]
        ms = int(ins["at"] * 1000)
        parts.append(f"[{n}:a]aresample=48000,volume=1.6,adelay={ms}|{ms}[v{n}]")
        labels.append(f"[v{n}]")
    graph = (f"[0:a]aresample=48000,volume='if({cond},{DUCK},1)':eval=frame[orig];"
             + ";".join(parts) + ";"
             + f"[orig]{''.join(labels)}amix=inputs={len(labels) + 1}:normalize=0:duration=first[a]")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", *args, "-filter_complex", graph,
                        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", str(out)], capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("voice mix failed: %s", r.stderr[-500:])
        video.rename(out)
    else:
        video.unlink(missing_ok=True)
