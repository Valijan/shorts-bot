import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


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

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")  # tiny/base/small/medium/large-v3
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")  # cpu / cuda
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
ALLOWED_USERS = {
    int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x.strip()
}

SUB_FONT = os.getenv("SUB_FONT", "DejaVu Sans")
SHOW_TITLE = os.getenv("SHOW_TITLE", "1") == "1"
WATERMARK = os.getenv("WATERMARK", "")  # например @mychannel
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "")

DATA_DIR.mkdir(parents=True, exist_ok=True)
