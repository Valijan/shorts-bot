"""Что случилось на стриме — по тому, ЧТО пишет чат во время всплеска.

Всплеск находим по количеству людей в чате, а здесь смотрим на смысл сообщений:
смешно (KEKW, ахаха), хайп (POG, W, имба), фейл (L, кринж, 💀), шок (WTF, ЧТО, monkaS),
грусть (F, Sadge). Скучные всплески — паровоз подписок, розыгрыш, голосование «1 1 1» —
помечаем как шум: такой момент не стоит монтировать. Работает без интернета и без ИИ.
"""
import re
from collections import Counter

KINDS = {
    "funny": ("😂", "Смешной момент"),
    "hype": ("🔥", "Хайп"),
    "fail": ("💀", "Фейл"),
    "shock": ("😱", "Шок"),
    "sad": ("😢", "Грустный момент"),
}

_P = {
    "funny": r"kekw|kekl|lulw?|omegalul|icant|pepelaugh|lmf?ao|rofl|\bxd+\b|\bхд+\b|"
             r"а?х[ах]{2,}|[ах]?ха[хa]{1,}|ору+|ржу|ржака|угар|смешн|😂|🤣|😆",
    "hype": r"\bpog\w*|poggers|pogu|\bw+\b|\bgoat\b|\bez\b|clap|имб[аы]|топ+\b|красав|легенд|"
            r"жестк|мощн|сильн|еее+|воу|вау|wow|🔥|💪|👏|🐐",
    "fail": r"\bl+\b|ratio|кринж|позор|лох|днищ|🤡|💀|бб+\b|\bgg\b|минус|ну ты|ноо+|no{3,}",
    "shock": r"wtf|\bомг\b|\bomg\b|monka\w*|\bd:|чт[оo]+[\?!]*$|чего+\??$|жесть|\?{3,}|😱|😳|🤯|а{4,}|wha+t",
    "sad": r"\bf+\b|sadge|bibletump|biblethump|pepehands|😢|😭|грустн|жалко|rip\b|\bрип\b",
}
_NOISE = (r"gift|sub(s|scri)|подписк|prime|raid|рейд|giveaway|розыгр|!drop|дроп|"
          r"^[\d+\-]{1,3}$|^[12]$|^\+$|бит[ыа]|ставк")
_RX = {k: re.compile(v, re.I) for k, v in _P.items()}
_NOISE_RX = re.compile(_NOISE, re.I)
_WORD = re.compile(r"[\wЀ-ӿ]+|[^\s\w]", re.U)


def classify(texts: list[str]) -> dict:
    """{'kind', 'emoji', 'label', 'share', 'noise', 'skip', 'top'} по сообщениям всплеска."""
    from .safety import CHAT_ADULT, SEXUAL
    texts = [t.strip() for t in texts if t and t.strip()]
    if len(texts) < 4:
        return {"kind": "", "emoji": "⚡", "label": "Момент", "share": 0.0, "noise": 0.0, "skip": False,
                "top": [], "adult": 0.0}
    adult = sum(1 for t in texts if CHAT_ADULT.search(t) or SEXUAL.search(t)) / len(texts)
    hits = Counter()
    noise = 0
    for t in texts:
        if _NOISE_RX.search(t):
            noise += 1
            continue
        for k, rx in _RX.items():
            if rx.search(t):
                hits[k] += 1
    n = len(texts)
    words = Counter(w for t in texts for w in {x for x in _WORD.findall(t) if len(x) > 1 or not x.isalnum()})
    top = [w for w, c in words.most_common(3) if c >= max(3, n * 0.08)]
    kind, cnt = (hits.most_common(1) or [("", 0)])[0]
    share = cnt / n
    noise_share = noise / n
    if share < 0.12:
        kind = ""
    emoji, label = KINDS.get(kind, ("⚡", "Момент"))
    return {"kind": kind, "emoji": emoji, "label": label, "share": round(share, 2),
            "noise": round(noise_share, 2), "skip": noise_share >= 0.5 and share < 0.2, "top": top,
            "adult": round(adult, 2)}


def describe(m: dict) -> str:
    """Короткая подпись для Telegram: '😂 Смешной момент · KEKW, ахаха'."""
    if not m:
        return ""
    s = f"{m['emoji']} {m['label']}"
    if m.get("top"):
        s += " · " + ", ".join(m["top"][:3])
    return s


def rank(moment: dict) -> float:
    """Сила момента для отбора лучших: всплеск × «эмоциональность» чата."""
    base = moment.get("peak", 1) / max(1.0, moment.get("norm", 1))
    md = moment.get("mood") or {}
    return base * (1.0 + md.get("share", 0)) * (0.3 if md.get("skip") else 1.0)
