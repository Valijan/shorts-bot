"""Живой режим: пишем эфир Twitch или YouTube кусками, читаем чат как гость и режем всплески.

Как это работает:
- Streamlink отдаёт поток, ffmpeg без перекодирования режет его на куски по 2 с.
  На диске лежат только последние несколько минут, старые куски удаляются.
- Чат читаем анонимно, без аккаунта и токена: Twitch — как гость IRC (justinfan…),
  YouTube — так же, как его читает страница эфира в браузере.
- Для YouTube можно дать ссылку на канал: бот сам находит текущий эфир через yt-dlp.
- Каждые 2 с считаем, сколько разных людей написали за последние 10 с.
  Всплеск — когда это намного выше обычного уровня ИМЕННО ЭТОГО стрима
  (медиана за последние 10 минут). Поэтому у крупного стримера, где чат всегда
  летит, весь эфир не превращается в один сплошной всплеск.
- Клип берём с запасом: начало всплеска минус 20 с (чат реагирует с опозданием,
  а сам момент случился раньше) и до пика плюс несколько секунд.
"""
import collections
import json
import logging
import random
import re
import shutil
import socket
import ssl
import statistics
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config as cfg

log = logging.getLogger(__name__)

SEG = 2.0         # длина куска записи и шаг детектора, с
WIN = 10.0        # окно подсчёта чата, с
HISTORY = 600.0   # по скольким секундам считаем «норму» стрима
WARMUP = 180.0    # первые 3 минуты нормы ещё нет — не режем
KEEP = 420.0      # сколько секунд записи держим на диске

# боты и сервисы, которые пишут в чат сами по себе
BOTS = {"nightbot", "streamelements", "moobot", "fossabot", "streamlabs", "wizebot", "botrixoficial",
        "sery_bot", "soundalerts", "commanderroot", "pokemoncommunitygame", "kofistreambot",
        "blerp", "own3d", "streamstickers", "lolrankbot", "creatisbot", "deepbot"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
_TW_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?(?:\?.*)?$")
_NAME_RE = re.compile(r"^@?([A-Za-z0-9_]{3,25})$")
_YT_VID = re.compile(r"(?:youtube\.com/(?:watch\?(?:\S*&)?v=|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
_YT_CH = re.compile(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.\-]{3,30}|channel/UC[\w-]{22}|c/[\w.\-]+|user/[\w.\-]+)")


def _twitch(login: str) -> dict:
    login = login.lower()
    return {"platform": "twitch", "key": login, "label": f"Twitch {login}",
            "url": f"https://twitch.tv/{login}", "fixed": False}


def _youtube_channel(path: str) -> dict:
    name = path.split("/")[-1]
    return {"platform": "youtube", "key": name.lstrip("@").lower(),
            "label": f"YouTube {name if name.startswith('@') else '@' + name}",
            "url": f"https://www.youtube.com/{path}/live", "fixed": False}


def parse_source(text: str) -> dict | None:
    """Что смотреть. Twitch: 'канал', 'twitch.tv/канал', 'twitch канал'.
    YouTube: ссылка на эфир (watch?v=, /live/, youtu.be), ссылка на канал (youtube.com/@ник) или 'yt ник'."""
    words = (text or "").strip().split()
    if len(words) == 2 and words[0].lower() in ("yt", "youtube", "ютуб"):
        m = re.fullmatch(r"@?([\w.\-]{3,30})", words[1])
        return _youtube_channel("@" + m.group(1)) if m else None
    if len(words) == 2 and words[0].lower() in ("tw", "twitch", "твич"):
        m = _NAME_RE.match(words[1])
        return _twitch(m.group(1)) if m else None
    if len(words) != 1:
        return None
    t = words[0]
    m = _YT_VID.search(t)
    if m:
        vid = m.group(1)
        return {"platform": "youtube", "key": vid.lower(), "label": f"YouTube {vid}",
                "url": f"https://www.youtube.com/watch?v={vid}", "fixed": True}
    m = _YT_CH.match(t)
    if m:
        return _youtube_channel(m.group(1))
    m = _TW_RE.match(t) or _NAME_RE.match(t)
    return _twitch(m.group(1)) if m else None


def parse_channel(text: str) -> str:
    """Совместимость: имя канала Twitch."""
    p = parse_source(text)
    return p["key"] if p and p["platform"] == "twitch" else ""


# ---------- YouTube: куки, поиск эфира, разбор чата ----------

def yt_cookies() -> list[str]:
    """Куки YouTube из cookies.txt (тот же файл, что для yt-dlp) — если сервер просит «я не робот»."""
    p = cfg.YTDLP_COOKIES
    out = []
    if p and Path(p).exists():
        for line in Path(p).read_text(errors="ignore").splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            f = line.split("\t")
            if not line.startswith("#") and len(f) >= 7 and ("youtube.com" in f[0] or "google.com" in f[0]):
                out.append(f"{f[5]}={f[6]}")
    return out


def _http(url: str, data: bytes | None = None, timeout: int = 20) -> str:
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
         "Cookie": "; ".join(["SOCS=CAI", "CONSENT=YES+1"] + yt_cookies())}
    if data is not None:
        h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yt_resolve(url: str) -> tuple[str | None, str]:
    """(id идущего эфира или None, статус: live / offline / upcoming / ended / текст ошибки)."""
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    if cfg.YTDLP_COOKIES and Path(cfg.YTDLP_COOKIES).exists():
        opts["cookiefile"] = cfg.YTDLP_COOKIES
    try:
        with YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:
        msg = str(e)
        if re.search(r"not currently live|will begin|Premieres in|is offline", msg, re.I):
            return None, "offline"
        return None, msg[-200:]
    if not info or info.get("_type") == "playlist":
        return None, "offline"
    status = info.get("live_status")
    if status == "is_live" or info.get("is_live"):
        return info.get("id"), "live"
    if status == "is_upcoming":
        return None, "upcoming"
    return None, "ended" if status in ("was_live", "post_live", "not_live") else "offline"


def _json_after(text: str, marker: str):
    i = text.find(marker)
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text, text.index("{", i))[0]
    except ValueError:
        return None


def yt_initial(html: str) -> tuple[str | None, str, str | None]:
    """Со страницы чата: (ключ API, версия клиента, токен продолжения для «Чат» — всех сообщений)."""
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    ver = (re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html)
           or re.search(r'"clientVersion":"([^"]+)"', html))
    data = _json_after(html, 'window["ytInitialData"]') or _json_after(html, "ytInitialData") or {}
    lc = (data.get("contents") or {}).get("liveChatRenderer") or {}
    cont = None
    try:  # первый пункт меню — «Топ-чат» (отфильтрованный), последний — «Чат» (все сообщения)
        items = lc["header"]["liveChatHeaderRenderer"]["viewSelector"]["sortFilterSubMenuRenderer"]["subMenuItems"]
        cont = items[-1]["continuation"]["reloadContinuationData"]["continuation"]
    except (KeyError, IndexError, TypeError):
        pass
    if not cont:
        cont = _continuation(lc.get("continuations", []))[0]
    return (key.group(1) if key else None), (ver.group(1) if ver else "2.20241101.00.00"), cont


def _continuation(items) -> tuple[str | None, int]:
    for c in items or []:
        for v in c.values():
            if isinstance(v, dict) and v.get("continuation"):
                return v["continuation"], int(v.get("timeoutMs") or 1000)
    return None, 1000


def yt_actions(data: dict) -> tuple[list[tuple[float, str]], str | None, int]:
    """Из ответа get_live_chat: ([(время, id автора)], следующий токен или None, пауза в мс)."""
    lcc = (data.get("continuationContents") or {}).get("liveChatContinuation")
    if not lcc:
        return [], None, 1000
    msgs = []
    for a in lcc.get("actions", []):
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        text = "".join(x.get("text", "") for x in (r.get("message") or {}).get("runs", []))
        name = ((r.get("authorName") or {}).get("simpleText") or "").lstrip("@").lower()
        if text.startswith("!") or name in BOTS:
            continue
        try:
            ts = int(r.get("timestampUsec") or 0) / 1e6
        except ValueError:
            ts = 0.0
        msgs.append((ts or time.time(), r.get("authorExternalChannelId") or name))
    cont, wait = _continuation(lcc.get("continuations"))
    return msgs, cont, wait


# ---------- Чат ----------

class Waiting(Exception):
    """Эфир ещё не найден — подождём, это не ошибка."""


class Chat(threading.Thread):
    """Анонимное подключение к IRC-чату Twitch. Хранит сообщения за последние полминуты."""

    def __init__(self, channel: str, stop: threading.Event):
        super().__init__(daemon=True, name=f"chat-{channel}")
        self.channel, self.stop = channel, stop
        self.msgs = collections.deque()   # (время, id пользователя)
        self.lock = threading.Lock()
        self.connected = False

    def run(self):
        delay = 2
        while not self.stop.is_set():
            try:
                self._session()
                delay = 2
            except Waiting:
                delay = 5
            except Exception as e:
                log.warning("chat %s: %s", self.channel, e)
            self.connected = False
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 60)

    def _session(self):
        host = "irc.chat.twitch.tv"
        raw = socket.create_connection((host, 6697), timeout=30)
        s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        nick = f"justinfan{random.randint(10000, 99999)}"
        s.sendall((f"CAP REQ :twitch.tv/tags twitch.tv/commands\r\nPASS SCHMOOPIIE\r\n"
                   f"NICK {nick}\r\nJOIN #{self.channel}\r\n").encode())
        s.settimeout(1.0)
        buf, last = b"", time.time()
        try:
            while not self.stop.is_set():
                try:
                    data = s.recv(65536)
                except socket.timeout:
                    if time.time() - last > 400:  # Twitch шлёт PING раз в ~5 минут
                        raise ConnectionError("чат молчит слишком долго")
                    continue
                if not data:
                    raise ConnectionError("сервер закрыл соединение")
                last = time.time()
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for ln in lines:
                    reply = self.handle(ln.decode("utf-8", "replace"))
                    if reply:
                        s.sendall(reply.encode())
        finally:
            s.close()

    def handle(self, ln: str, now: float | None = None) -> str | None:
        """Разбор одной строки IRC. Возвращает ответ серверу, если он нужен."""
        if ln.startswith("PING"):
            return "PONG" + ln[4:] + "\r\n"
        tags = {}
        if ln.startswith("@"):
            t, _, ln = ln.partition(" ")
            tags = dict(kv.partition("=")[::2] for kv in t[1:].split(";"))
        parts = ln.split(" ", 3)
        if len(parts) < 2:
            return None
        cmd = parts[1]
        if cmd in ("001", "ROOMSTATE"):
            self.connected = True
        elif cmd == "RECONNECT":
            raise ConnectionError("Twitch попросил переподключиться")
        elif cmd == "PRIVMSG" and len(parts) == 4:
            login = parts[0].lstrip(":").split("!")[0].lower()
            text = parts[3].split(":", 1)[1] if ":" in parts[3] else ""
            if login in BOTS or text.startswith("!"):
                return None  # боты и команды вроде !discord — не реакция зрителей
            with self.lock:
                self.msgs.append((now or time.time(), tags.get("user-id") or login))
        return None

    def window(self, t_end: float, win: float = WIN) -> tuple[int, int]:
        """(сообщений, разных людей) за последние win секунд."""
        with self.lock:
            while self.msgs and self.msgs[0][0] < t_end - 30:
                self.msgs.popleft()
            users = [u for ts, u in self.msgs if t_end - win < ts <= t_end]
        return len(users), len(set(users))


class YTChat(Chat):
    """Чат YouTube-эфира без аккаунта: тот же запрос, которым его читает страница в браузере."""

    def __init__(self, name: str, stop: threading.Event, vid_fn):
        super().__init__(name, stop)
        self.vid_fn = vid_fn   # id текущего эфира (ищет запись) или None

    def _session(self):
        vid = self.vid_fn()
        if not vid:
            raise Waiting()
        key, ver, cont = yt_initial(_http(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}"))
        if not cont:
            raise ConnectionError("чат эфира недоступен (выключен или эфир закончился)")
        api = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false"
        if key:
            api += f"&key={key}"
        self.connected = True
        while not self.stop.is_set() and self.vid_fn() == vid:
            body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en"}},
                    "continuation": cont}
            msgs, cont, wait = yt_actions(json.loads(_http(api, json.dumps(body).encode())))
            if not cont:
                raise ConnectionError("чат закрылся")
            now = time.time()
            with self.lock:
                self.msgs.extend((min(ts, now), u) for ts, u in msgs)
            self.stop.wait(min(max(wait / 1000, 1.0), 3.0))


# ---------- Детектор всплесков ----------

class Detector:
    """Всплеск = активность чата заметно выше обычной для этого стрима."""

    def __init__(self, ratio=2.0, z=4.0, min_score=6.0, pre=20.0, post=8.0, lmin=20.0, lmax=59.0,
                 cooldown=20.0):
        self.ratio, self.z, self.min_score = ratio, z, min_score
        self.pre, self.post, self.lmin, self.lmax, self.cooldown = pre, post, lmin, lmax, cooldown
        self.hist = collections.deque()   # (время, балл)
        self.ep = None                    # текущий всплеск
        self.hot_run = 0                  # сколько шагов подряд выше порога
        self.last_end = 0.0
        self.now = {"score": 0.0, "msgs": 0, "users": 0, "norm": 0.0, "hot": 0.0, "ready": False}

    def reset(self):
        self.hist.clear()
        self.ep = None
        self.hot_run = 0

    @staticmethod
    def score(msgs: int, users: int) -> float:
        # главное — сколько РАЗНЫХ людей пишут; спам одного человека почти не считается
        return users + 0.3 * max(0, msgs - users)

    def thresholds(self) -> tuple[float, float, float]:
        vals = [s for _, s in self.hist]
        med = statistics.median(vals)
        mad = max(1.0, statistics.median(abs(v - med) for v in vals) * 1.4826)
        hot = max(self.min_score, med * self.ratio, med + self.z * mad)   # порог всплеска
        warm = max(self.min_score * 0.6, med * 1.3, med + 1.5 * mad)       # «чат оживился»
        return med, hot, warm

    def step(self, t: float, msgs: int, users: int, armed: bool = True) -> dict | None:
        sc = self.score(msgs, users)
        ready = bool(self.hist) and t - self.hist[0][0] >= WARMUP
        out = None
        if ready:
            med, hot, warm = self.thresholds()
            self.now = {"score": sc, "msgs": msgs, "users": users, "norm": med, "hot": hot, "ready": True}
            self.hot_run = self.hot_run + 1 if sc >= hot else 0
            if self.ep is None:
                # 3 шага подряд (6 с) выше порога — случайный всплеск из 2–3 сообщений не считается
                if armed and self.hot_run >= 3 and t >= self.last_end + self.cooldown:
                    # откатываемся назад, пока чат был выше «оживился» — там начало реакции
                    t_w = t
                    for ht, hs in reversed(self.hist):
                        if hs < warm or t - ht > 20:
                            break
                        t_w = ht
                    onset = t_w - WIN / 2
                    start = max(onset - self.pre, self.last_end)  # не залезаем на прошлый клип
                    self.ep = {"start": start, "peak_t": t, "peak": sc, "msgs": msgs, "users": users,
                               "norm": med, "below": 0}
            else:
                ep = self.ep
                if sc > ep["peak"]:
                    ep.update(peak=sc, peak_t=t, msgs=msgs, users=users)
                ep["below"] = ep["below"] + 1 if sc < warm else 0
                too_long = t + self.post - ep["start"] >= self.lmax
                if ep["below"] >= 2 or too_long or not armed:
                    out = self._close(ep)
        else:
            self.now = {"score": sc, "msgs": msgs, "users": users, "norm": 0.0, "hot": 0.0, "ready": False}
        self.hist.append((t, sc))
        while self.hist and t - self.hist[0][0] > HISTORY:
            self.hist.popleft()
        return out

    def _close(self, ep: dict) -> dict:
        start = ep["start"]
        end = min(start + self.lmax, max(ep["peak_t"] + self.post, start + self.lmin))
        self.ep, self.last_end = None, end
        return {"start": start, "end": end, "peak": ep["peak"], "msgs": ep["msgs"],
                "users": ep["users"], "norm": ep["norm"]}


# ---------- Запись эфира ----------

class Recorder(threading.Thread):
    """Streamlink → ffmpeg → куски по 2 с. Знает, какому времени соответствует каждый кусок."""

    def __init__(self, name: str, workdir: Path, stop: threading.Event, url_fn, cmd_fn,
                 on_state=None, src_cmd: list[str] | None = None):
        super().__init__(daemon=True, name=f"rec-{name}")
        self.name_, self.workdir, self.stop = name, workdir, stop
        self.url_fn = url_fn      # адрес эфира или None, если эфира сейчас нет
        self.cmd_fn = cmd_fn      # (адрес, число неудач подряд) → команда, которая пишет поток в stdout
        self.on_state = on_state or (lambda live: None)
        self.src_cmd = src_cmd    # для тестов: своя команда вместо streamlink
        self.segs = collections.deque()   # (начало, конец по часам сервера, файл)
        self.lock = threading.Lock()
        self.live = False
        self.fails = 0
        self.procs = []

    def run(self):
        while not self.stop.is_set():
            try:
                got, found = self._record()
            except Exception as e:
                log.warning("record %s: %s", self.name_, e)
                got, found = False, False
            if self.live:
                self.live = False
                self.on_state(False)
            self.fails = 0 if got or not found else self.fails + 1
            # эфир есть, но запись не пошла — пробуем снова быстро; эфира нет — проверяем раз в 30 с
            if self.stop.wait(5 if got or found else 30):
                break

    def kill(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()

    def _record(self) -> tuple[bool, bool]:
        """(записали ли что-то, был ли эфир)."""
        if self.src_cmd:
            src = self.src_cmd
        else:
            url = self.url_fn()
            if not url:
                return False, False
            src = self.cmd_fn(url, self.fails)
        d = self.workdir / f"rec_{int(time.time())}"
        d.mkdir(parents=True, exist_ok=True)
        ff = ["ffmpeg", "-v", "error", "-fflags", "+genpts", "-i", "pipe:0",
              "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
              "-f", "segment", "-segment_time", str(SEG), "-reset_timestamps", "1",
              "-segment_list", str(d / "list.csv"), "-segment_list_type", "csv", str(d / "s%06d.ts")]
        with open(d / "source.log", "w") as e1, open(d / "ffmpeg.log", "w") as e2:
            p1 = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=e1)
            p2 = subprocess.Popen(ff, stdin=p1.stdout, stderr=e2)
            p1.stdout.close()
            self.procs = [p1, p2]
            pos, got = 0, False
            try:
                while not self.stop.is_set() and p2.poll() is None:
                    pos, new = self._scan(d, pos)
                    if new and not got:
                        got, self.live = True, True
                        self.on_state(True)
                    self._prune()
                    time.sleep(0.5)
            finally:
                self.kill()
                p1.wait(5)
                p2.wait(5)
            self._scan(d, pos)
        if not got:
            log.info("record %s: %s", self.name_, (d / "source.log").read_text(errors="ignore")[-300:])
            shutil.rmtree(d, ignore_errors=True)
        return got, True

    def _scan(self, d: Path, pos: int) -> tuple[int, int]:
        """Читает новые строки списка кусков и ставит им время по часам сервера."""
        lst = d / "list.csv"
        if not lst.exists():
            return pos, 0
        with open(lst) as f:
            f.seek(pos)
            chunk = f.read()
        if not chunk.endswith("\n"):
            chunk = chunk[:chunk.rfind("\n") + 1]  # недописанную строку дочитаем в следующий раз
        if not chunk:
            return pos, 0
        rows = []
        for line in chunk.splitlines():
            try:
                name, a, b = line.rsplit(",", 2)
                rows.append((d / name.strip('"'), max(0.05, float(b) - float(a))))
            except ValueError:
                continue
        # кусок закрыт только что: последний кончается «сейчас», предыдущие — раньше на свою длину
        end = time.time()
        stamped = []
        for path, dur in reversed(rows):
            stamped.append((end - dur, end, path))
            end -= dur
        with self.lock:
            self.segs.extend(reversed(stamped))
        return pos + len(chunk.encode()), len(rows)

    def _prune(self):
        limit = time.time() - KEEP
        with self.lock:
            while self.segs and self.segs[0][1] < limit:
                _, _, p = self.segs.popleft()
                p.unlink(missing_ok=True)

    def covered_until(self) -> float:
        with self.lock:
            return self.segs[-1][1] if self.segs else 0.0

    def cut(self, a: float, b: float, out: Path) -> bool:
        """Склеивает куски, покрывающие [a, b] по часам сервера, без перекодирования."""
        with self.lock:
            parts = [p for s, e, p in self.segs if e > a and s < b and p.exists()]
        if not parts:
            return False
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True)
        lst.unlink(missing_ok=True)
        if r.returncode != 0:
            log.warning("cut failed: %s", r.stderr[-500:])
            return False
        return out.exists() and out.stat().st_size > 0


# ---------- Сессия: запись + чат + детектор ----------

class LiveSession:
    def __init__(self, source, root: Path, on_clip, on_event=None, src_cmd=None, busy=None, **det):
        src = parse_source(source) if isinstance(source, str) else source
        if not src:
            raise ValueError(f"не понял, что смотреть: {source}")
        self.src = src
        self.key, self.label = src["key"], src["label"]
        self.channel = self.label                    # для сообщений бота
        self.on_clip = on_clip                       # (путь к клипу, инфо о всплеске)
        self.on_event = on_event or (lambda text: None)
        self.stop_ev = threading.Event()
        self.dir = root / f"{src['platform']}_{self.key}"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        yt = src["platform"] == "youtube"
        opts = dict(ratio=cfg.LIVE_RATIO, z=cfg.LIVE_Z, min_score=cfg.LIVE_MIN_CHAT, pre=cfg.LIVE_PRE,
                    post=cfg.LIVE_YT_POST if yt else cfg.LIVE_POST, lmin=cfg.LIVE_MIN, lmax=cfg.LIVE_MAX,
                    cooldown=cfg.LIVE_COOLDOWN)
        opts.update(det)
        self.det = Detector(**opts)
        self.det_opts, self.busy, self.back = opts, busy, None
        self.yt_id, self.yt_status, self._ended_said = None, "", False
        if yt:
            self.chat = YTChat(self.key, self.stop_ev, lambda: self.yt_id)
            url_fn = self._yt_url
        else:
            self.chat = Chat(self.key, self.stop_ev)
            url_fn = lambda: src["url"]  # noqa: E731
        self.rec = Recorder(self.key, self.dir, self.stop_ev, url_fn, self._cmd, self._state, src_cmd)
        self.pending: list[dict] = []
        self.clips = 0
        self.started = time.time()
        self.thread = threading.Thread(target=self._loop, daemon=True, name=f"live-{self.key}")

    def _yt_url(self) -> str | None:
        vid, status = yt_resolve(self.src["url"])
        self.yt_id, self.yt_status = vid, status
        if not vid and status not in ("offline", "upcoming", "ended"):
            log.warning("youtube %s: %s", self.key, status)
        if status == "ended" and self.src["fixed"] and not self._ended_said:
            self._ended_said = True
            self.on_event(f"⚠️ {self.label}: этот эфир уже закончился. Дай ссылку на канал "
                          f"(youtube.com/@ник) — бот сам найдёт следующий эфир.")
        return f"https://www.youtube.com/watch?v={vid}" if vid else None

    def _cmd(self, url: str, fails: int) -> list[str]:
        yt = self.src["platform"] == "youtube"
        if yt and fails % 2 == 1 and shutil.which("yt-dlp"):
            # streamlink не смог — пробуем через yt-dlp (он лучше справляется с проверками YouTube)
            c = ["yt-dlp", "-q", "--no-warnings", "-f", "best[height<=1080]/best", "--no-part", "-o", "-", url]
            if cfg.YTDLP_COOKIES and Path(cfg.YTDLP_COOKIES).exists():
                c[1:1] = ["--cookies", cfg.YTDLP_COOKIES]
            return c
        c = ["streamlink", "--stdout", "--loglevel", "warning", "--stream-segment-threads", "2"]
        if yt:
            for ck in yt_cookies():
                c += ["--http-cookie", ck]
        else:
            c += ["--twitch-disable-ads", "--twitch-low-latency"]
        return c + [url, cfg.LIVE_QUALITY]

    def start(self):
        self.rec.start()
        self.chat.start()
        self.thread.start()

    def stop(self):
        self.stop_ev.set()
        self.rec.kill()

    def _state(self, live: bool):
        if live:
            self.on_event(f"▶️ <b>{self.label}</b> в эфире — записываю и слежу за чатом.")
            if self.back is None and cfg.LIVE_BACKFILL and self.src["platform"] == "twitch" and not self.rec.src_cmd:
                # эфир уже шёл до нас — параллельно разбираем его с самого начала по записи
                from .backfill import Backfill
                self.back = Backfill(self.key, self.label, self.dir, self.stop_ev, self._back_clip,
                                     self.on_event, self.busy, **self.det_opts)
                self.back.start()
        else:
            self.det.reset()
            self.on_event(f"⏹ Эфир <b>{self.label}</b> закончился. Клипов: {self.clips}. "
                          f"Жду следующий запуск.")

    def _back_clip(self, path: Path, info: dict):
        self.clips += 1
        self.on_clip(path, info)

    def _loop(self):
        nxt = time.time()
        while not self.stop_ev.is_set():
            nxt += SEG
            if self.stop_ev.wait(max(0.0, nxt - time.time())):
                break
            now = time.time()
            try:
                msgs, users = self.chat.window(now)
                c = self.det.step(now, msgs, users, armed=self.rec.live)
                if c:
                    self.pending.append(c)
                self._flush(now)
            except Exception:
                log.exception("live loop %s", self.channel)

    def _flush(self, now: float):
        upto = self.rec.covered_until()
        for c in list(self.pending):
            if upto >= c["end"] + 0.5:
                self.pending.remove(c)
                out = self.dir / f"clip_{int(c['start'])}.mp4"
                if self.rec.cut(c["start"], c["end"], out):
                    self.clips += 1
                    try:
                        self.on_clip(out, c)
                    except Exception:
                        log.exception("on_clip")
            elif now - c["end"] > 90:   # эфир оборвался, видео до конца всплеска так и не пришло
                self.pending.remove(c)

    def status(self) -> str:
        n = self.det.now
        rec = "🔴 пишу эфир" if self.rec.live else "⚪ жду начала стрима"
        if not self.rec.live and self.yt_status == "upcoming":
            rec = "⚪ эфир запланирован, жду начала"
        chat = "чат ✅" if self.chat.connected else "чат ⏳ подключаюсь"
        up = int((time.time() - self.started) // 60)
        if n["ready"]:
            level = (f"сейчас {n['users']} чел. / {n['msgs']} сообщ. за 10 с · "
                     f"норма {n['norm']:.0f} · всплеск от {n['hot']:.0f}")
        else:
            level = f"сейчас {n['users']} чел. за 10 с · считаю норму (первые 3 мин)"
        back = f"\n⏪ {self.back.state}" if self.back and self.back.state else ""
        return (f"<b>{self.label}</b>: {rec}, {chat}\n{level}\n"
                f"клипов: {self.clips} · слежу {up} мин{back}")
