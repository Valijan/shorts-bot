"""Telegram-бот: принимает видео/ссылку, нарезает шортсы, присылает обратно."""
import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import config as cfg
from .pipeline import process

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

LOCAL = bool(cfg.BOT_API_URL)
UPLOAD_LIMIT = (2000 if LOCAL else 50) * 1024 * 1024
DOWNLOAD_LIMIT = (2000 if LOCAL else 20) * 1024 * 1024
URL_RE = re.compile(r"https?://\S+")

router = Router()
queue: asyncio.Queue = asyncio.Queue()

# ---------- Настройки пользователя ----------

SETTINGS_FILE = cfg.DATA_DIR / "settings.json"
DEFAULTS = {"clips": 5, "subs": "orig", "mode": "auto", "length": "mid", "pick": "best"}
OPTIONS = {
    "pick": [("🔥 Лучшие моменты", "best"), ("📼 По порядку", "seq")],
    "clips": [("3", 3), ("5", 5), ("10", 10), ("20", 20), ("Все", 0)],
    "length": [("15–30с", "short"), ("30–45с", "mid"), ("45–60с", "long")],
    "subs": [("Оригинал", "orig"), ("RU", "ru"), ("UZ", "uz"), ("EN", "en"), ("Нет", "none")],
    "mode": [("Авто", "auto"), ("Кроп по лицу", "crop"), ("С размытым фоном", "blur")],
}
TITLES = {"pick": "Как резать", "clips": "Сколько клипов", "length": "Длина клипа", "subs": "Субтитры", "mode": "Кадр 9:16"}


def load_all() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def get_settings(uid: int) -> dict:
    return {**DEFAULTS, **load_all().get(str(uid), {})}


def set_setting(uid: int, key: str, value):
    data = load_all()
    data.setdefault(str(uid), {})[key] = value
    SETTINGS_FILE.write_text(json.dumps(data))


def settings_kb(uid: int) -> InlineKeyboardMarkup:
    st = get_settings(uid)
    rows = []
    for key, opts in OPTIONS.items():
        rows.append([InlineKeyboardButton(text=f"— {TITLES[key]} —", callback_data="noop")])
        rows.append([
            InlineKeyboardButton(text=("✅ " if st[key] == val else "") + label,
                                 callback_data=f"set:{key}:{val}")
            for label, val in opts
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(uid: int) -> str:
    st = get_settings(uid)
    names = {k: dict((v, l) for l, v in OPTIONS[k]) for k in OPTIONS}
    clips = "все" if st["clips"] == 0 else st["clips"]
    return ("⚙️ <b>Настройки</b>\n"
            f"Режим: {names['pick'][st['pick']]}\n"
            f"Клипов: {clips} · Длина: {names['length'][st['length']]}\n"
            f"Субтитры: {names['subs'][st['subs']]} · Кадр: {names['mode'][st['mode']]}")


# ---------- Доступ ----------

class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if cfg.ALLOWED_USERS and user and user.id not in cfg.ALLOWED_USERS:
            if isinstance(event, Message):
                await event.answer(f"⛔ Нет доступа. Ваш ID: <code>{user.id}</code>")
            return
        return await handler(event, data)


# ---------- Команды ----------


@router.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "🎬 <b>Нарезка видео в Shorts</b>\n\n"
        "Пришли мне видеофайл или ссылку (YouTube и др.) — я найду лучшие моменты, "
        "переведу в вертикальный формат 9:16, добавлю субтитры и пришлю готовые клипы.\n\n"
        "/settings — настройки (кол-во клипов, длина, субтитры, кадр)\n"
        "/id — твой Telegram ID\n\n" + settings_text(m.from_user.id)
    )


@router.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Твой ID: <code>{m.from_user.id}</code>")


@router.message(Command("settings"))
async def settings(m: Message):
    await m.answer(settings_text(m.from_user.id), reply_markup=settings_kb(m.from_user.id))


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data.startswith("set:"))
async def on_set(c: CallbackQuery):
    _, key, val = c.data.split(":", 2)
    if key == "clips":
        val = int(val)
    set_setting(c.from_user.id, key, val)
    if key == "pick" and val == "seq":  # по порядку: всё видео частями до минуты
        set_setting(c.from_user.id, "clips", 0)
        set_setting(c.from_user.id, "length", "long")
    try:
        await c.message.edit_text(settings_text(c.from_user.id), reply_markup=settings_kb(c.from_user.id))
    except TelegramBadRequest:
        pass
    await c.answer("Сохранено")


# ---------- Приём видео ----------

async def enqueue(m: Message, job: dict):
    pos = queue.qsize() + (1 if getattr(worker, "busy", False) else 0)
    status = await m.answer(
        "📥 Принял! " + (f"В очереди: {pos}." if pos else "Начинаю…") + "\n" + settings_text(m.from_user.id)
    )
    job.update(chat_id=m.chat.id, uid=m.from_user.id, status_id=status.message_id)
    await queue.put(job)


@router.message(F.video | F.document | F.video_note)
async def on_file(m: Message):
    f = m.video or m.video_note or m.document
    if m.document and not (m.document.mime_type or "").startswith("video"):
        return await m.answer("Это не видео 🙂 Пришли видеофайл или ссылку.")
    if f.file_size and f.file_size > DOWNLOAD_LIMIT:
        mb = DOWNLOAD_LIMIT // 1024 // 1024
        return await m.answer(f"Файл больше {mb} МБ. Пришли ссылку или файл поменьше.")
    await enqueue(m, {"type": "file", "file_id": f.file_id})


@router.message(F.text.regexp(URL_RE))
async def on_link(m: Message):
    await enqueue(m, {"type": "url", "url": URL_RE.search(m.text).group(0)})


@router.message(F.text)
async def other(m: Message):
    await m.answer("Пришли видеофайл или ссылку на видео. /settings — настройки.")


# ---------- Обработка ----------

def ytdl(url: str, outdir: Path) -> Path:
    from yt_dlp import YoutubeDL

    opts = {
        "outtmpl": str(outdir / "source.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4", "noplaylist": True, "quiet": True, "no_warnings": True,
    }
    if cfg.YTDLP_COOKIES and Path(cfg.YTDLP_COOKIES).exists():
        opts["cookiefile"] = cfg.YTDLP_COOKIES
    with YoutubeDL(opts) as y:
        y.extract_info(url, download=True)
    files = sorted(outdir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Не удалось скачать видео по ссылке")
    return files[0]


def fmt(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


async def worker(bot: Bot):
    loop = asyncio.get_running_loop()
    while True:
        job = await queue.get()
        worker.busy = True
        workdir = cfg.DATA_DIR / "jobs" / uuid.uuid4().hex[:10]
        workdir.mkdir(parents=True, exist_ok=True)
        chat, sid = job["chat_id"], job["status_id"]
        last = {"t": 0.0, "text": ""}
        local_src = None

        async def set_status(text: str, force=False):
            if text == last["text"] or (not force and time.time() - last["t"] < 3):
                return
            last.update(t=time.time(), text=text)
            try:
                await bot.edit_message_text(text, chat_id=chat, message_id=sid)
            except TelegramBadRequest:
                pass

        def progress(text: str):
            asyncio.run_coroutine_threadsafe(set_status(text), loop)

        try:
            await set_status("⬇️ Скачиваю видео…", force=True)
            if job["type"] == "url":
                src = await asyncio.to_thread(ytdl, job["url"], workdir)
            else:
                tg_file = await bot.get_file(job["file_id"])
                if LOCAL and os.path.isabs(tg_file.file_path) and os.path.exists(tg_file.file_path):
                    src = local_src = Path(tg_file.file_path)  # файл уже на диске сервера
                else:
                    src = workdir / "source.mp4"
                    await bot.download_file(tg_file.file_path, destination=src)

            st = get_settings(job["uid"])

            async def send_clip(r):
                cap = (f"<b>{r['title'] or 'Клип ' + str(r['index'])}</b>\n"
                       f"⏱ {fmt(r['start'])}–{fmt(r['end'])} из оригинала\n\n#shorts")
                if r["path"].stat().st_size > UPLOAD_LIMIT:
                    await bot.send_message(chat, f"Клип {r['index']} слишком большой для отправки.")
                    return
                await bot.send_video(
                    chat, FSInputFile(r["path"]), caption=cap, width=1080, height=1920,
                    duration=int(r["end"] - r["start"]), supports_streaming=True,
                    request_timeout=600,
                )

            def on_clip(r):
                asyncio.run_coroutine_threadsafe(send_clip(r), loop).result()

            results = await asyncio.to_thread(process, src, workdir, st, progress, on_clip)
            await set_status(f"✅ Готово! {len(results)} шортс(ов).", force=True)
        except Exception as e:
            log.exception("job failed")
            await set_status(f"❌ Ошибка: {str(e)[:300]}", force=True)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if local_src:
                try:
                    local_src.unlink()
                except OSError:
                    pass
            worker.busy = False
            queue.task_done()


async def main():
    if not cfg.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан в .env")
    session = None
    if LOCAL:
        session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.BOT_API_URL, is_local=True),
                                 timeout=900)
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(cfg.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(router)
    asyncio.create_task(worker(bot))
    log.info("Bot started (local API: %s, LLM: %s)", LOCAL, bool(cfg.ANTHROPIC_API_KEY))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
