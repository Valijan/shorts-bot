"""Догоняем эфир Twitch с самого начала.

Живой режим видит стрим только с момента, когда бот к нему подключился. Но у идущего эфира
Twitch уже есть запись (VOD), а у записи — сохранённый чат с отметками времени. Поэтому:
- находим запись текущего эфира и сколько он уже идёт;
- читаем весь чат записи как гость, без аккаунта;
- прогоняем его через тот же детектор всплесков, что и в живом режиме;
- каждый найденный момент докачиваем из записи (только нужные секунды, не весь стрим)
  и отдаём в монтаж так же, как живые клипы.
"""
import calendar
import json
import logging
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config as cfg
from .live import BOTS, SEG, UA, WARMUP, WIN, Detector

log = logging.getLogger(__name__)

GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # публичный ID сайта twitch.tv — работает без входа
TAIL = 120.0   # последние 2 минуты записи ещё не выложены — их уже ловит живой режим

Q_STREAM = ("query($l:String!){user(login:$l){stream{id createdAt archiveVideo{id}}"
            " videos(first:1,type:ARCHIVE,sort:TIME){edges{node{id createdAt}}}}}")
Q_STREAM_OLD = ("query($l:String!){user(login:$l){stream{id createdAt}"
                " videos(first:1,type:ARCHIVE,sort:TIME){edges{node{id createdAt}}}}}")
Q_CHAT = ("query($id:ID!,$o:Int){video(id:$id){comments(contentOffsetSeconds:$o){edges{node{"
          "id contentOffsetSeconds commenter{id login} message{fragments{text}}}}}}}")


def _gql(query: str, variables: dict, timeout: int = 20) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GQL, data=body, headers={
        "Client-ID": CLIENT_ID, "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if data.get("errors"):
        raise RuntimeError(str(data["errors"])[:300])
    return data.get("data") or {}


def _ts(iso: str | None) -> float:
    try:
        return float(calendar.timegm(time.strptime((iso or "")[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0


def hms(t: float) -> str:
    t = int(max(0, t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def pick_vod(user: dict, now: float | None = None) -> dict | None:
    """Из ответа Twitch: запись идущего эфира {'id', 'elapsed'} или None (эфира или записи нет)."""
    st = (user or {}).get("stream")
    if not st:
        return None
    started = _ts(st.get("createdAt"))
    vid = (st.get("archiveVideo") or {}).get("id")
    if not vid and started:
        # запись создаётся одновременно с эфиром: берём последнюю, если она началась тогда же
        for e in (user.get("videos") or {}).get("edges") or []:
            n = e.get("node") or {}
            if abs(_ts(n.get("createdAt")) - started) < 900:
                vid = n.get("id")
    if not vid or not started:
        return None
    return {"id": str(vid), "elapsed": max(0.0, (now or time.time()) - started)}


def current_vod(login: str) -> dict | None:
    try:
        user = _gql(Q_STREAM, {"l": login}).get("user")
    except RuntimeError:
        user = _gql(Q_STREAM_OLD, {"l": login}).get("user")
    return pick_vod(user or {})


def parse_comments(video: dict) -> tuple[list[tuple[float, str, str, str]], float]:
    """([(секунда записи, id сообщения, автор, текст)], секунда последнего сообщения в ответе)."""
    out, last = [], -1.0
    for e in ((video or {}).get("comments") or {}).get("edges") or []:
        n = e.get("node") or {}
        t = float(n.get("contentOffsetSeconds") or 0)
        last = max(last, t)
        c = n.get("commenter") or {}
        login = (c.get("login") or "").lower()
        text = "".join(f.get("text") or "" for f in (n.get("message") or {}).get("fragments") or [])
        if login in BOTS or text.startswith("!"):
            continue  # боты и команды — не реакция зрителей
        out.append((t, str(n.get("id")), str(c.get("id") or login or n.get("id")), text))
    return out, last


def _vod_range(fetch, a: float, b: float, stop, report) -> list[tuple]:
    """Чат записи на отрезке [a, b): [(секунда, id, автор, текст)]."""
    out, off, errors = [], a, 0
    while off < b and not (stop and stop.is_set()):
        try:
            msgs, last = parse_comments(fetch(off))
            errors = 0
        except Exception as e:
            errors += 1
            if errors >= 5:
                raise RuntimeError(f"Twitch не отдаёт чат записи: {e}") from None
            time.sleep(2 * errors)
            continue
        out.extend(m for m in msgs if a <= m[0] < b)
        if last < 0:
            off += 60                      # тут тишина — смотрим дальше
        elif int(last) <= int(off):
            off = int(off) + 1             # вся страница в одной секунде
        else:
            off = last
        report(min(off, b) - a)
    return out


def vod_chat(vid: str, upto: float, stop: threading.Event | None = None, fetch=None,
             progress=None, workers: int = 8) -> list[tuple]:
    """Весь чат записи от начала до секунды upto: [(секунда, автор, текст)].
    Twitch отдаёт чат маленькими страницами, поэтому запись делим на куски и читаем их
    параллельно — для эфира в несколько часов это минуты вместо получаса."""
    from concurrent.futures import ThreadPoolExecutor
    fetch = fetch or (lambda off: _gql(Q_CHAT, {"id": vid, "o": int(off)}).get("video") or {})
    n = max(1, min(workers, int(upto // 300)))
    edges = [upto * i / n for i in range(n)] + [upto + 1]
    done = [0.0] * n
    lock = threading.Lock()

    def part(i):
        def report(x):
            with lock:
                done[i] = x
                if progress:
                    progress(min(1.0, sum(done) / max(1.0, upto)))
        return _vod_range(fetch, edges[i], edges[i + 1], stop, report)

    with ThreadPoolExecutor(n) as ex:
        parts = list(ex.map(part, range(n)))
    out, seen = [], set()
    for chunk in parts:
        for t, mid, user, text in chunk:
            if mid not in seen and t <= upto:
                seen.add(mid)
                out.append((t, user, text))
    return sorted(out)


def find_moments(msgs: list[tuple], upto: float, **det) -> list[dict]:
    """Тот же детектор, что в живом режиме, но по сохранённому чату. Время — секунды записи.
    У каждого момента — mood: что писал чат (смешно, хайп, фейл…); скучные всплески отброшены."""
    import bisect
    from .chatmood import classify
    d = Detector(**det)
    out, lo, hi, t = [], 0, 0, SEG
    while t <= upto:
        while hi < len(msgs) and msgs[hi][0] <= t:
            hi += 1
        while lo < hi and msgs[lo][0] <= t - WIN:
            lo += 1
        c = d.step(t, hi - lo, len({m[1] for m in msgs[lo:hi]}))
        if c and c["end"] <= upto:
            out.append(c)
        t += SEG
    times = [m[0] for m in msgs]
    keep = []
    for c in out:
        a, b = bisect.bisect_left(times, c["start"]), bisect.bisect_right(times, c["end"] + 5)
        c["mood"] = classify([m[2] for m in msgs[a:b] if len(m) > 2])
        if not c["mood"]["skip"]:
            keep.append(c)
    return keep


def strongest(moments: list[dict], limit: int) -> list[dict]:
    """Если моментов слишком много — оставляем самые сильные (во сколько раз выше нормы), по порядку."""
    if limit <= 0 or len(moments) <= limit:
        return moments
    from .chatmood import rank
    top = sorted(moments, key=rank, reverse=True)[:limit]
    return sorted(top, key=lambda m: m["start"])


def _start_time(path: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def grab(vid: str, a: float, b: float, out: Path) -> bool:
    """Докачивает из записи кусок [a, b] (секунды записи) без перекодирования."""
    raw = out.with_suffix(".ts")
    # streamlink начинает с границы куска записи (до 10 с раньше) — берём с запасом и подрезаем
    cmd = ["streamlink", "--loglevel", "error", "--force", "-o", str(raw),
           "--hls-start-offset", hms(a), "--hls-duration", hms(b - a + 25),
           f"https://www.twitch.tv/videos/{vid}", cfg.LIVE_QUALITY]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raw.unlink(missing_ok=True)
        return False
    if not raw.exists() or raw.stat().st_size == 0:
        log.warning("grab %s %s: %s", vid, hms(a), (r.stderr or r.stdout)[-300:])
        raw.unlink(missing_ok=True)
        return False
    # в кусках записи время идёт от начала эфира: по нему видно, сколько лишнего в начале
    st = _start_time(raw)
    skip = a - st if st is not None and 0 <= a - st < 30 else 0.0
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{skip:.2f}", "-i", str(raw),
                        "-t", f"{b - a:.2f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                        "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True)
    raw.unlink(missing_ok=True)
    if r.returncode != 0:
        log.warning("grab cut failed: %s", r.stderr[-300:])
        return False
    return out.exists() and out.stat().st_size > 0


class Backfill(threading.Thread):
    """Разбирает уже прошедшую часть эфира, пока живой режим следит за текущей."""

    def __init__(self, login: str, label: str, workdir: Path, stop: threading.Event,
                 on_clip, on_event, busy=None, **det):
        super().__init__(daemon=True, name=f"back-{login}")
        self.login, self.label, self.workdir, self.stop = login, label, workdir, stop
        self.on_clip, self.on_event = on_clip, on_event
        self.busy = busy or (lambda: 0)   # сколько клипов уже ждёт монтажа
        self.det = det
        self.found = self.sent = 0
        self.state = ""

    def run(self):
        try:
            self._run()
        except Exception as e:
            log.exception("backfill %s", self.login)
            self.state = "начало эфира разобрать не вышло"
            self.on_event(f"⚠️ <b>{self.label}</b>: не получилось разобрать начало эфира ({str(e)[:150]}). "
                          "Живой режим работает дальше.")

    def _run(self):
        vod = None
        for _ in range(3):   # запись появляется через минуту-другую после старта эфира
            vod = current_vod(self.login)
            if vod or self.stop.wait(20):
                break
        if self.stop.is_set():
            return
        if not vod:
            self.state = "записи эфира нет — только с текущего момента"
            self.on_event(f"⏪ <b>{self.label}</b>: у этого эфира нет записи на Twitch (стример отключил "
                          "сохранение трансляций), поэтому начало стрима недоступно. Режу с текущего момента.")
            return
        upto = vod["elapsed"] - TAIL
        if upto < WARMUP + 60:
            return   # эфир только начался — догонять нечего
        self.state = "читаю чат с начала эфира"
        self.on_event(f"⏪ <b>{self.label}</b>: эфир идёт уже {hms(vod['elapsed'])}. "
                      "Читаю чат с самого начала и ищу моменты…")
        def prog(f):
            self.state = f"читаю чат с начала эфира: {int(f * 100)}%"
        msgs = vod_chat(vod["id"], upto, self.stop, progress=prog)
        if self.stop.is_set():
            return
        if not msgs:
            self.state = "чат записи пуст"
            self.on_event(f"⏪ <b>{self.label}</b>: чат записи пуст — моменты в начале эфира найти не по чему.")
            return
        all_m = find_moments(msgs, upto, **self.det)
        moments = strongest(all_m, cfg.LIVE_BACKFILL_MAX)
        self.found = len(moments)
        if not moments:
            self.state = "в начале эфира всплесков не было"
            self.on_event(f"⏪ <b>{self.label}</b>: за первые {hms(upto)} всплесков в чате не было.")
            return
        extra = f" (из {len(all_m)} взял самые сильные)" if len(all_m) > len(moments) else ""
        self.on_event(f"⏪ <b>{self.label}</b>: за первые {hms(upto)} нашёл моментов: {len(moments)}{extra}. "
                      "Монтирую по порядку.")
        for m in moments:
            while self.busy() > 2 and not self.stop.wait(5):
                pass   # не заваливаем очередь: живые моменты не должны ждать долго
            if self.stop.is_set():
                return
            self.state = f"догоняю начало: {self.sent}/{self.found}"
            out = self.workdir / f"back_{int(m['start'])}.mp4"
            if grab(vod["id"], m["start"], m["end"], out):
                self.sent += 1
                try:
                    self.on_clip(out, {**m, "at": m["start"]})
                except Exception:
                    log.exception("on_clip")
        self.state = f"начало эфира разобрано: {self.sent} клипов"


if __name__ == "__main__":   # проверка на сервере: python3 -m app.backfill [канал] [grab]
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "-" else None
    if not name:
        top = _gql("query{streams(first:8){edges{node{broadcaster{login}}}}}", {})
        for e in top["streams"]["edges"]:
            name = e["node"]["broadcaster"]["login"]
            if current_vod(name):
                break
    v = current_vod(name)
    print("channel:", name, "vod:", v)
    if v:
        t0 = time.time()
        up = min(v["elapsed"] - TAIL, 1800)
        ms = vod_chat(v["id"], up)
        print(f"chat: {len(ms)} msgs in first {hms(up)} ({time.time() - t0:.1f}s)")
        mm = find_moments(ms, up)
        print("moments:", [(hms(m["start"]), round(m["end"] - m["start"]), m["users"], round(m["norm"])) for m in mm])
        if "grab" in sys.argv and mm:
            o = Path("/tmp/back_test.mp4")
            t0 = time.time()
            print("grab:", grab(v["id"], mm[0]["start"], mm[0]["end"], o), f"{time.time() - t0:.1f}s")
            print(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
                                  "-of", "csv=p=0", str(o)], capture_output=True, text=True).stdout)
