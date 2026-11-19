"""Ночной режим: несколько стримеров Twitch по очереди.

Для каждого стримера берём запись последнего (или идущего) эфира, читаем её чат целиком,
находим всплески тем же детектором, что и в живом режиме, докачиваем только нужные куски
и отдаём в монтаж. Пока клипы одного стримера не смонтированы — к следующему не переходим.
"""
import logging
import shutil
import threading
import time
from pathlib import Path

from . import config as cfg
from .backfill import TAIL, _gql, _ts, find_moments, grab, hms, strongest, vod_chat

log = logging.getLogger(__name__)

Q_LAST = ("query($l:String!){user(login:$l){displayName stream{createdAt archiveVideo{id}}"
          " videos(first:3,type:ARCHIVE,sort:TIME){edges{node{id createdAt lengthSeconds title}}}}}")


def latest_vod(login: str) -> dict | None:
    """Запись идущего эфира или последнего прошедшего: {id, dur, live, ago, title}."""
    user = _gql(Q_LAST, {"l": login}).get("user")
    if not user:
        raise RuntimeError("такого канала нет")
    vids = [e.get("node") or {} for e in (user.get("videos") or {}).get("edges") or []]
    st = user.get("stream")
    now = time.time()
    if st:
        started = _ts(st.get("createdAt"))
        vid = (st.get("archiveVideo") or {}).get("id")
        if not vid:
            vid = next((v.get("id") for v in vids if abs(_ts(v.get("createdAt")) - started) < 900), None)
        if vid:
            return {"id": str(vid), "dur": max(0.0, now - started - TAIL), "live": True, "ago": 0.0,
                    "title": next((v.get("title") for v in vids if str(v.get("id")) == str(vid)), "")}
    if not vids:
        return None
    v = vids[0]
    dur = float(v.get("lengthSeconds") or 0)
    return {"id": str(v["id"]), "dur": dur, "live": False, "title": v.get("title") or "",
            "ago": max(0.0, now - _ts(v.get("createdAt")) - dur)}


def _ago(sec: float) -> str:
    h = int(sec // 3600)
    return "только что" if h < 1 else (f"{h} ч назад" if h < 48 else f"{h // 24} дн назад")


class NightRun(threading.Thread):
    def __init__(self, logins: list[str], per: int, root: Path, on_clip, on_event, busy):
        super().__init__(daemon=True, name="night")
        self.logins, self.per, self.root = logins, per, root
        self.on_clip, self.on_event, self.busy = on_clip, on_event, busy
        self.stop_ev = threading.Event()
        self.state, self.idx = "запускаюсь", 0
        self.results: list[str] = []
        self.clips = 0
        self.det = dict(ratio=cfg.LIVE_RATIO, z=cfg.LIVE_Z, min_score=cfg.LIVE_MIN_CHAT, pre=cfg.LIVE_PRE,
                        post=cfg.LIVE_POST, lmin=cfg.LIVE_MIN, lmax=cfg.LIVE_MAX, cooldown=cfg.LIVE_COOLDOWN)

    def stop(self):
        self.stop_ev.set()

    def status(self) -> str:
        done = "\n".join(f"✔️ {r}" for r in self.results)
        left = self.logins[self.idx + 1:] if self.idx < len(self.logins) else []
        q = f"\n⏳ дальше: {', '.join(left)}" if left else ""
        cur = (f"\n▶️ {self.idx + 1}/{len(self.logins)} <b>{self.logins[self.idx]}</b>: {self.state}"
               if self.idx < len(self.logins) and not self.stop_ev.is_set() else "")
        return (f"🌙 <b>Ночной разбор</b> · клипов всего: {self.clips}"
                + (f"\n{done}" if done else "") + cur + q)

    def run(self):
        try:
            for i, login in enumerate(self.logins):
                self.idx = i
                if self.stop_ev.is_set():
                    return
                try:
                    n, note = self._one(login)
                    self.results.append(f"{login}: {n} клип(ов){note}")
                except Exception as e:
                    log.exception("night %s", login)
                    self.results.append(f"{login}: ошибка — {str(e)[:120]}")
                    self.on_event(f"⚠️ 🌙 <b>{login}</b>: не получилось ({str(e)[:150]}). Иду к следующему.")
            self.idx = len(self.logins)
            if not self.stop_ev.is_set():
                self.on_event("🌙 <b>Ночной разбор закончен.</b>\n" + "\n".join(f"• {r}" for r in self.results))
        finally:
            shutil.rmtree(self.root, ignore_errors=True)

    def _wait_queue(self, upto: int):
        while self.busy() > upto and not self.stop_ev.wait(5):
            pass

    def _one(self, login: str) -> tuple[int, str]:
        self.state = "ищу запись эфира"
        vod = latest_vod(login)
        if not vod or vod["dur"] < 300:
            self.on_event(f"🌙 <b>{login}</b>: записи эфира нет (стример не сохраняет трансляции). Пропускаю.")
            return 0, " — записи нет"
        when = "идёт сейчас" if vod["live"] else f"закончился {_ago(vod['ago'])}"
        self.state = f"читаю чат записи ({hms(vod['dur'])})"
        self.on_event(f"🌙 {self.idx + 1}/{len(self.logins)} <b>{login}</b>: эфир {hms(vod['dur'])}, {when}. "
                      "Читаю чат и ищу моменты…")
        dur_txt = hms(vod["dur"])

        def prog(f):
            self.state = f"читаю чат записи ({dur_txt}): {int(f * 100)}%"
        msgs = vod_chat(vod["id"], vod["dur"], self.stop_ev, progress=prog)
        if self.stop_ev.is_set():
            return 0, " — остановлено"
        if not msgs:
            self.on_event(f"🌙 <b>{login}</b>: чат записи пуст. Пропускаю.")
            return 0, " — чат пуст"
        moments = strongest(find_moments(msgs, vod["dur"], **self.det), self.per)
        if not moments:
            self.on_event(f"🌙 <b>{login}</b>: всплесков в чате не нашёл. Иду дальше.")
            return 0, " — всплесков нет"
        self.on_event(f"🌙 <b>{login}</b>: самых ярких моментов: {len(moments)}, монтирую по очереди.")
        d = self.root / login
        d.mkdir(parents=True, exist_ok=True)
        sent = 0
        for k, m in enumerate(moments, 1):
            self._wait_queue(2)            # не заваливаем очередь — монтаж идёт по одному
            if self.stop_ev.is_set():
                return sent, " — остановлено"
            self.state = f"качаю момент {k}/{len(moments)} (на {hms(m['start'])} эфира)"
            out = d / f"night_{int(m['start'])}.mp4"
            if grab(vod["id"], m["start"], m["end"], out):
                sent += 1
                self.clips += 1
                self.on_clip(out, {**m, "at": m["start"]}, login)
        self.state = "домонтирую последние клипы"
        self._wait_queue(0)                # к следующему стримеру — только когда очередь пуста
        return sent, ""
