"""Чат Twitch как сигнал: всплеск сообщений = в эфире случилось что-то яркое.

Подключение анонимное (без логина и ключей), только чтение. Считаем сообщения по секундам.
"""
import logging
import random
import re
import socket
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

HOST, PORT = "irc.chat.twitch.tv", 6667
REACT = 5.0      # зрители пишут через несколько секунд после момента
WIN = 5          # окно, с: считаем сообщения за 5 секунд
MIN_MSGS = 5     # меньше — не всплеск даже в тихом чате
URL_RE = re.compile(r"twitch\.tv/([A-Za-z0-9_]{3,25})(?:[/?#]|$)", re.I)


def channel_from_url(url: str) -> str | None:
    m = URL_RE.search(url or "")
    name = m.group(1).lower() if m else None
    return None if name in (None, "videos", "directory", "clips") else name


class TwitchChat:
    """Фоновый счётчик сообщений чата. start() → … → events(a, b) → stop()."""

    def __init__(self, channel: str, host: str = HOST, port: int = PORT):
        self.channel, self.host, self.port = channel.lower().lstrip("#"), host, port
        self.times: list[float] = []     # время каждого сообщения
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=15) as s:
                    s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{self.channel}\r\n".encode())
                    s.settimeout(5)
                    buf = b""
                    while not self._stop.is_set():
                        try:
                            data = s.recv(4096)
                        except socket.timeout:
                            continue
                        if not data:
                            break
                        buf += data
                        *lines, buf = buf.split(b"\r\n")
                        for ln in lines:
                            if ln.startswith(b"PING"):
                                s.sendall(b"PONG" + ln[4:] + b"\r\n")
                            elif b" PRIVMSG #" in ln:
                                self.times.append(time.time())
            except Exception as e:
                log.info("chat reconnect (%s)", e)
            self._stop.wait(5)

    def events(self, wall_a: float, wall_b: float) -> list[dict]:
        """Всплески чата на отрезке [wall_a, wall_b] → события со временем от начала отрезка."""
        return spikes(np.array(self.times), wall_a, wall_b)


def spikes(times: np.ndarray, wall_a: float, wall_b: float) -> list[dict]:
    n = int(wall_b - wall_a) + 1
    if n < WIN or not len(times):
        return []
    # обычный темп чата — по всей истории эфира, чтобы «всплеск» был относительно нормы стримера
    t0 = float(times.min())
    hist = np.histogram(times, bins=max(1, int(time.time() - t0) // WIN + 1))[0] if len(times) > 20 else np.array([0])
    base = float(np.median(hist))
    spread = float(np.median(np.abs(hist - base))) or 1.0
    need = max(MIN_MSGS, base * 2.5, base + 4 * spread)
    per_sec = np.histogram(times[(times >= wall_a) & (times <= wall_b)], bins=n, range=(wall_a, wall_a + n))[0]
    win = np.convolve(per_sec, np.ones(WIN), mode="same")
    out = []
    for i in np.argsort(win)[::-1]:
        if win[i] < need:
            break
        if any(abs(int(i) - e["_i"]) < 20 for e in out):  # один всплеск — одно событие
            continue
        ratio = win[i] / max(base, 1.0)
        out.append({"t": max(0.0, float(i) - REACT), "w": float(min(7, 3 + ratio / 2)), "kind": "funny",
                    "label": "всплеск чата", "_i": int(i)})
    for e in out:
        e.pop("_i")
    return sorted(out, key=lambda e: e["t"])
