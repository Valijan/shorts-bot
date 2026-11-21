import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Загрузка моделей Whisper с HuggingFace: быстрый загрузчик Xet на Windows иногда намертво
# подвешивает весь процесс — берём обычный HTTP и ставим таймауты.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Локальный Bot API сервер (файлы до 2 ГБ). Пусто = обычный облачный API (лимит 20 МБ).
BOT_API_URL = os.getenv("BOT_API_URL", "").rstrip("/")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
# вместо Claude можно ключ Gemini (Google AI Studio, есть бесплатный уровень); если заданы оба — берём Claude
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")   # пусто — бот сам выберет свежую модель Flash
LLM_ON = bool(ANTHROPIC_API_KEY or GEMINI_API_KEY)

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")  # tiny/base/small/medium/large-v3
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")  # cpu / cuda
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
# распознавание в отдельном процессе (по умолчанию на Windows): зависание не замораживает бота
ASR_SUBPROCESS = os.getenv("ASR_SUBPROCESS", "1" if os.name == "nt" else "0") == "1"

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
ALLOWED_USERS = {
    int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x.strip()
}

SUB_FONT = os.getenv("SUB_FONT", "DejaVu Sans")
SHOW_TITLE = os.getenv("SHOW_TITLE", "1") == "1"
WATERMARK = os.getenv("WATERMARK", "")  # например @mychannel
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "")

def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# === Живой режим (/live) ===
LIVE_QUALITY = os.getenv("LIVE_QUALITY", "1080p60,1080p,best")  # качество записи эфира
LIVE_PRE = _float("LIVE_PRE", 20)          # сколько секунд брать ДО начала всплеска
LIVE_POST = _float("LIVE_POST", 8)         # и после пика
LIVE_YT_POST = _float("LIVE_YT_POST", 15)  # на YouTube запись может отставать от зрителей сильнее — после пика берём больше
LIVE_MIN = _float("LIVE_MIN", 20)          # длина клипа, с
LIVE_MAX = _float("LIVE_MAX", 59)
LIVE_RATIO = _float("LIVE_RATIO", 2.0)     # всплеск = чат в N раз активнее нормы стрима
LIVE_Z = _float("LIVE_Z", 3.0)             # и выше нормы на столько «обычных колебаний» (было 4 — клипов выходило мало)
LIVE_MIN_CHAT = _float("LIVE_MIN_CHAT", 6)  # минимум разных людей за 10 с (для маленьких стримов)
LIVE_COOLDOWN = _float("LIVE_COOLDOWN", 20)  # пауза между клипами, с
LIVE_CHANNELS = _int("LIVE_CHANNELS", 1)   # сколько эфиров одновременно (1 = новый /live переключает)
LIVE_BACKFILL = os.getenv("LIVE_BACKFILL", "1") == "1"  # Twitch: разбирать эфир с самого начала по записи
LIVE_BACKFILL_MAX = _int("LIVE_BACKFILL_MAX", 30)       # максимум клипов из уже прошедшей части эфира

DATA_DIR.mkdir(parents=True, exist_ok=True)
TZ_OFFSET = _float("TZ_OFFSET", 5)          # часовой пояс для табло (Ташкент UTC+5)
LIVE_DASH_EVERY = _int("LIVE_DASH_EVERY", 15)  # как часто обновлять табло живого режима, с
FONTS_DIR = os.getenv("FONTS_DIR", "")      # своя папка со шрифтами субтитров (Windows: шрифты рядом с ботом)
NIGHT_CLIPS = _int("NIGHT_CLIPS", 10)          # клипов с каждого стримера в ночном режиме
YT_EMAIL = os.getenv("YT_EMAIL", "valijanabdullayev2000@gmail.com")   # подставляется при входе в YouTube
