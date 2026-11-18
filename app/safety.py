"""Проверка клипа на 18+ перед публикацией: речь (субтитры) + реакция чата.

adult  — ставим на YouTube «Возрастное ограничение 18+»;
block  — явный сексуальный контент: сам не публикуем, спрашиваем владельца.
"""
import re

HEAVY = re.compile(r"\b(?:на|по|за|от|вы|у|при|раз)?(?:х[уy][йяеиюё]\w*|пизд\w*|[её]б[ауиоыёе]\w*|"
                   r"бл[яе]д\w*|залуп\w*|муд[ао]к\w*|fuck\w*|cunt\w*|motherf\w*)", re.I)
SEXUAL = re.compile(r"секс\w*|порн\w*|голы[ейхм]\w*|голая|сиськ\w*|трах\w*|минет\w*|\bчлен\w*|"
                    r"\bsex\w*|porn\w*|nude\w*|naked|boobs|onlyfans|nsfw|хентай|hentai", re.I)
CHAT_ADULT = re.compile(r"18\s*\+|nsfw|\bтос\b|\btos\b|\bбан\b|\bban\b|🔞|🍑|💦|😏|горяч|раздел", re.I)


def check(speech: str, chat_texts: list[str] | None = None, adult_share: float = 0.0) -> dict:
    speech = speech or ""
    heavy = len(HEAVY.findall(speech))
    sexual = len(SEXUAL.findall(speech))
    chat = chat_texts or []
    chat_hits = sum(1 for t in chat if CHAT_ADULT.search(t) or SEXUAL.search(t))
    share = max(adult_share, chat_hits / len(chat) if chat else 0.0)
    reasons = []
    if heavy >= 3:
        reasons.append(f"мат в речи ({heavy})")
    if sexual:
        reasons.append(f"сексуальные темы в речи ({sexual})")
    if share >= 0.1:
        reasons.append(f"чат пишет «18+/бан/nsfw» ({int(share * 100)}%)")
    return {"adult": bool(reasons), "block": sexual >= 2 or (sexual >= 1 and share >= 0.15),
            "reasons": reasons, "heavy": heavy}
