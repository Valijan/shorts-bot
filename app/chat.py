"""Чат Twitch-записи как сигнал: где зрители «взорвались», там и момент для шортса.

Скачивать весь чат 10-часового эфира долго, поэтому берём пробы: раз в STEP секунд
запрашиваем страницу сообщений с этой секунды и по ней оцениваем скорость чата.
Работает только для ссылок twitch.tv/videos/<id>. Любая ошибка — просто нет сигнала.
"""
import json
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger(__name__)

GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # публичный ID веб-плеера Twitch
QUERY_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
STEP = 20          # шаг проб, секунды
DELAY = 6          # чат реагирует на момент с опозданием
MAX_PROBES = 2400  # потолок запросов на одно видео

VOD_RE = re.compile(r"twitch\.tv/(?:[^/\s]+/v(?:ideo)?|videos)/(\d+)", re.I)
LAUGH = re.compile(r"lul|kekw|omegalul|lmao|lol|хаха|ахах|ахха|пхах|ору|ржу|😂|🤣|icant|pepelaugh", re.I)
HYPE = re.compile(r"pog|клип|clip|wtf|omg|ааа+|ооо+|что\?+|нифига|жесть|капец|имба|красав|"
                  r"\bw\b|🔥|monkas|holy|gg|ez|\bf\b", re.I)


def vod_id(url: str | None) -> str | None:
    m = VOD_RE.search(url or "")
    return m.group(1) if m else None


def _page(vid: str, offset: int) -> list[tuple[int, str]]:
    """Сообщения чата, начиная с секунды offset: [(секунда, текст), ...]."""
    body = json.dumps({
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": {"videoID": vid, "contentOffsetSeconds": int(offset)},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": QUERY_HASH}},
    }).encode()
    req = urllib.request.Request(GQL, data=body, headers={
        "Client-ID": CLIENT_ID, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    edges = (((data.get("data") or {}).get("video") or {}).get("comments") or {}).get("edges") or []
    out = []
    for e in edges:
        node = e.get("node") or {}
        frags = ((node.get("message") or {}).get("fragments")) or []
        out.append((int(node.get("contentOffsetSeconds") or 0),
                    "".join(f.get("text") or "" for f in frags)))
    return out


def _probe(vid: str, t: int):
    for _ in range(2):
        try:
            return t, _page(vid, t)
        except Exception as e:  # сеть моргнула — одна повторная попытка
            err = e
    log.debug("chat probe %s failed: %s", t, err)
    return t, None


def estimate(t: int, page, step: int = STEP) -> tuple[float, float, float]:
    """(сообщений за окно, доля смеха, доля хайпа) по одной пробе."""
    msgs = [(o, x) for o, x in (page or []) if o >= t]
    if not msgs:
        return 0.0, 0.0, 0.0
    inside = [(o, x) for o, x in msgs if o < t + step]
    last = msgs[-1][0]
    if last >= t + step:          # страница перекрыла всё окно — считаем точно
        count = float(len(inside))
    else:                         # страница кончилась раньше — достраиваем по скорости
        count = len(msgs) * step / max(1.0, last - t + 1)
    pool = inside or msgs
    n = len(pool)
    return (count, sum(1 for _, x in pool if LAUGH.search(x)) / n,
            sum(1 for _, x in pool if HYPE.search(x)) / n)


def build(samples: list[tuple[int, float, float, float]], duration: float, offset: float = 0.0,
          step: int = STEP, top: int = 40):
    """Пробы -> (массив «сообщений в секунду» по секундам видео, события-пики чата).

    samples: (секунда записи, сообщений за окно, доля смеха, доля хайпа).
    offset: с какой секунды записи начинается скачанный кусок.
    """
    n = int(duration) + 1
    rate = np.zeros(n)
    if not samples:
        return rate, []
    counts = np.array([s[1] for s in samples], dtype=float)
    for t, c, _, _ in samples:
        a = int(t - offset - DELAY)
        rate[max(0, a):max(0, min(n, a + step))] = c / step
    med, sd = float(np.median(counts)), float(np.std(counts)) + 1e-6
    if counts.max() < 3 or sd < 0.5:   # чат почти мёртвый — сигнала нет
        return rate, []
    events = []
    for i in np.argsort(counts)[::-1][:top]:
        t, c, laugh, hype = samples[int(i)]
        z = (c - med) / sd
        if z < 1.5:
            break
        funny = laugh >= 0.25 and laugh >= hype
        events.append({"t": max(0.0, float(t - offset - DELAY)), "w": float(min(8.0, 2 + z)),
                       "kind": "funny" if funny else "chat",
                       "label": "ЧАТ 😂" if funny else "ЧАТ 🔥"})
    return rate, sorted(events, key=lambda e: e["t"])


def signal(url: str | None, duration: float, offset: float = 0.0, progress=None):
    """Главная функция: (rate по секундам, события) или (None, []), если чата нет."""
    vid = vod_id(url)
    if not vid:
        return None, []
    step = STEP
    while duration / step > MAX_PROBES:
        step *= 2
    times = [int(offset) + i * step for i in range(int(duration // step) + 1)]
    samples, failed, done = [], 0, 0
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for t, page in pool.map(lambda x: _probe(vid, x), times):
                done += 1
                if page is None:
                    failed += 1
                    if failed > 25 and failed > done * 0.5:   # Twitch не отвечает — не мучаем
                        raise RuntimeError("слишком много ошибок чата")
                    continue
                samples.append((t, *estimate(t, page, step)))
                if progress and done % 50 == 0:
                    progress(done / len(times))
    except Exception as e:
        log.warning("twitch chat failed: %s", e)
        return None, []
    if len(samples) < max(3, len(times) // 4):
        return None, []
    rate, events = build(samples, duration, offset, step)
    return rate, events
