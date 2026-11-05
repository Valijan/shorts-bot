"""Telegram-бот: принимает видео/ссылку, находит моменты, монтирует шортсы, присылает обратно."""
import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, Message)

from . import ai, fx, pipeline
from . import config as cfg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

LOCAL = bool(cfg.BOT_API_URL)
UPLOAD_LIMIT = (2000 if LOCAL else 50) * 1024 * 1024
DOWNLOAD_LIMIT = (2000 if LOCAL else 20) * 1024 * 1024
URL_RE = re.compile(r"https?://\S+")
TC = r"\d{1,2}:\d{2}(?::\d{2})?"
RANGE_RE = re.compile(rf"({TC})\s*[-–—]\s*({TC})")

router = Router()
queue: asyncio.Queue = asyncio.Queue()
STATE = {"busy": False, "cancel": False, "job": None, "last": time.time(), "warned": False}
SESSIONS: dict[str, dict] = {}

# ---------- Настройки ----------

SETTINGS_FILE = cfg.DATA_DIR / "settings.json"
DEFAULTS = {"content": "auto", "pick": "best", "clips": 5, "length": "auto", "confirm": "off",
            "subs": "orig", "mode": "auto", "sub_color": "yellow", "sub_size": "m",
            "sub_pos": "bottom", "title": "hook", "sub_words": "auto", "fx": "auto", "music": "off", "review": "off",
            "pubtext": "on", "mark": "", "director": "on", "jumpcut": "auto", "teaser": "auto", "pbar": "auto",
            "loop": "auto", "censor": "off", "sub_anim": "pop", "vision": "on",
            "camp_label": "", "camp_tags": ""}
GLOBAL_DEFAULTS = {"autostop": 30}
OPTIONS = {
    "content": [("🎬 Обычное", "auto"), ("🎥 Фильм", "movie"), ("⚔️ MLBB", "mlbb"), ("🔫 PUBG M", "pubg")],
    "pick": [("🔥 Лучшие", "best"), ("📼 По порядку", "seq"), ("🏆 Топ-5", "top")],
    "clips": [("3", 3), ("5", 5), ("10", 10), ("20", 20), ("Все", 0)],
    "length": [("Авто", "auto"), ("15–30с", "short"), ("30–45с", "mid"), ("45–60с", "long")],
    "confirm": [("Сразу монтаж", "off"), ("👀 Сначала выбор", "on")],
    "subs": [("Оригинал", "orig"), ("RU", "ru"), ("UZ", "uz"), ("EN", "en"), ("Нет", "none")],
    "mode": [("Авто", "auto"), ("Лицо", "crop"), ("Размытый фон", "blur"), ("🎮 Игра", "game"),
             ("Сплит", "split")],
    "director": [("🤖 ИИ-монтажник", "on"), ("Только алгоритм", "off")],
    "sub_color": [("🟡", "yellow"), ("🟢", "green"), ("🔵", "cyan"), ("🩷", "pink")],
    "sub_size": [("S", "s"), ("M", "m"), ("L", "l")],
    "sub_pos": [("Низ", "bottom"), ("Центр", "center"), ("Верх", "top")],
    "title": [("🪝 Крючок 4с", "hook"), ("Весь клип", "on"), ("Выкл", "off")],
    "sub_words": [("Авто", "auto"), ("1 слово", "1"), ("2–3 слова", "3")],
    "fx": [("Авто (игры)", "auto"), ("💥 Всегда", "on"), ("Выкл", "off")],
    "music": [("Без музыки", "off"), ("🎵 С музыкой", "on")],
    "review": [("Выкл", "off"), ("🎙 Вкл", "on")],
    "pubtext": [("📋 Вкл", "on"), ("Выкл", "off")],
    "jumpcut": [("Авто", "auto"), ("✂️ Всегда", "on"), ("Выкл", "off")],
    "teaser": [("Авто (игры)", "auto"), ("🎯 Всегда", "on"), ("Выкл", "off")],
    "pbar": [("Авто (игры)", "auto"), ("▬ Всегда", "on"), ("Выкл", "off")],
    "loop": [("Авто (игры)", "auto"), ("🔁 Всегда", "on"), ("Выкл", "off")],
    "censor": [("Без цензуры", "off"), ("🤐 Запикивать мат", "on")],
    "sub_anim": [("💥 Выпрыгивают", "pop"), ("Без анимации", "none")],
    "vision": [("👁 ИИ видит килы", "on"), ("Выкл", "off")],
    "autostop": [("Выкл", 0), ("15 мин", 15), ("30 мин", 30), ("60 мин", 60)],
}
TITLES = {"content": "Что за видео", "pick": "Как резать", "clips": "Сколько клипов",
          "length": "Длина клипа", "confirm": "Перед монтажом", "subs": "Субтитры",
          "mode": "Кадр 9:16", "sub_color": "Цвет слова", "sub_size": "Размер субтитров",
          "sub_pos": "Где субтитры", "title": "Заголовок",
          "sub_words": "Слов в субтитре", "fx": "Эффекты на килах",
          "music": "Фоновая музыка (/music)", "review": "Обзор: ИИ-комментарий с озвучкой",
          "pubtext": "Текст для публикации",
          "jumpcut": "Убирать паузы в речи", "teaser": "Тизер: яркий момент в начале",
          "pbar": "Полоска прогресса внизу", "loop": "Петля (конец → начало)",
          "censor": "Мат (для монетизации)", "sub_anim": "Анимация субтитров",
          "vision": "Поиск килов по кадрам (нужен ключ Claude)",
          "director": "Камера в играх (ИИ нужен ключ Claude)", "autostop": "Автостоп pod'а без дела"}
MENUS = {
    "cut": ("✂️ Нарезка", ["content", "pick", "clips", "length", "confirm", "vision"]),
    "look": ("🎨 Оформление", ["mode", "director", "title", "subs", "sub_words", "sub_anim", "sub_color", "sub_size", "sub_pos", "censor"]),
    "fx": ("✨ Эффекты и звук", ["fx", "jumpcut", "teaser", "loop", "pbar", "music", "review", "pubtext"]),
    "srv": ("🖥 Сервер", ["autostop"]),
}
GLOBAL_KEYS = {"autostop"}
PRESETS = {
    "mlbb": ("⚔️ MLBB-стрим", {"content": "mlbb", "pick": "best", "length": "auto", "mode": "game",
                               "title": "hook", "sub_words": "1", "sub_pos": "bottom", "fx": "auto",
                               "jumpcut": "auto", "teaser": "on", "director": "on"}),
    "pubg": ("🔫 PUBG-стрим", {"content": "pubg", "pick": "best", "length": "auto", "mode": "game",
                               "title": "hook", "sub_words": "1", "sub_pos": "bottom", "fx": "auto",
                               "jumpcut": "auto", "teaser": "on", "director": "on"}),
    "movie": ("🎥 Фильм", {"content": "movie", "pick": "best", "length": "long", "mode": "auto",
                          "title": "hook", "sub_words": "3", "fx": "off", "jumpcut": "off",
                          "teaser": "off"}),
    "talk": ("🎙 Разговорное", {"content": "auto", "pick": "best", "length": "mid", "mode": "auto",
                               "title": "hook", "sub_words": "3", "fx": "off", "jumpcut": "on",
                               "teaser": "off"}),
}


def load_all() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def get_global() -> dict:
    return {**GLOBAL_DEFAULTS, **load_all().get("_global", {})}


def get_settings(uid: int) -> dict:
    return {**DEFAULTS, **load_all().get(str(uid), {}), **get_global()}


def set_setting(uid, key: str, value):
    data = load_all()
    owner = "_global" if key in GLOBAL_KEYS else str(uid)
    data.setdefault(owner, {})[key] = value
    SETTINGS_FILE.write_text(json.dumps(data))


def _label(key, val):
    return dict((v, l) for l, v in OPTIONS[key]).get(val, str(val))


def menu_kb() -> InlineKeyboardMarkup:
    presets = [InlineKeyboardButton(text=t, callback_data=f"preset:{k}") for k, (t, _) in PRESETS.items()]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="— 🎛 Пресет в одну кнопку —", callback_data="noop")],
        presets[:2], presets[2:],
        *[[InlineKeyboardButton(text=title, callback_data=f"menu:{k}")] for k, (title, _) in MENUS.items()],
    ])


def section_kb(uid: int, sec: str) -> InlineKeyboardMarkup:
    st = get_settings(uid)
    rows = []
    for key in MENUS[sec][1]:
        rows.append([InlineKeyboardButton(text=f"— {TITLES[key]} —", callback_data="noop")])
        opts = OPTIONS[key]
        for i in range(0, len(opts), 4):
            rows.append([InlineKeyboardButton(text=("✅ " if st[key] == val else "") + label,
                                              callback_data=f"set:{sec}:{key}:{val}")
                         for label, val in opts[i:i + 4]])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(uid: int) -> str:
    st = get_settings(uid)
    clips = "все" if st["clips"] == 0 else st["clips"]
    stop = f"{st['autostop']} мин" if st["autostop"] else "выкл"
    return ("⚙️ <b>Настройки</b>\n"
            f"Видео: {_label('content', st['content'])} · {_label('pick', st['pick'])}\n"
            f"Клипов: {clips} · Длина: {_label('length', st['length'])} · "
            f"{_label('confirm', st['confirm'])}\n"
            f"Субтитры: {_label('subs', st['subs'])} {_label('sub_color', st['sub_color'])} "
            f"{st['sub_size'].upper()} · Кадр: {_label('mode', st['mode'])}\n"
            f"Эффекты: {_label('fx', st['fx'])} · Музыка: {_label('music', st['music'])}\n"
            f"Ник на видео: {html.escape(st['mark']) or 'нет (/watermark)'} · Автостоп: {stop}")


# ---------- Доступ и активность ----------

class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if cfg.ALLOWED_USERS and user and user.id not in cfg.ALLOWED_USERS:
            if isinstance(event, Message):
                await event.answer(f"⛔ Нет доступа. Ваш ID: <code>{user.id}</code>")
            return
        STATE["last"], STATE["warned"] = time.time(), False
        return await handler(event, data)


# ---------- Команды ----------

HELP = (
    "🎬 <b>Нарезка видео в Shorts</b>\n\n"
    "Пришли видеофайл (до 2 ГБ) или ссылку. В подписи можно указать:\n"
    "• <code>mlbb 2</code>, <code>pubg</code>, <code>фильм 5</code> — тип видео и число клипов\n"
    "• <code>1:20-2:05</code> — вырезать именно этот кусок (можно несколько через запятую)\n"
    "• <code>топ</code> — один шортс «Топ-5 моментов»\n\n"
    "/settings — настройки · /watermark @ник — ник на видео\n"
    "/campaign Канал #тег — клиппинг-кампания (канал крупно + теги)\n"
    "/music — фоновая музыка · /queue — очередь · /cancel — отмена\n"
    "/stop — выключить сервер сейчас · /id — твой ID"
)


@router.message(CommandStart())
async def start(m: Message):
    await m.answer(HELP + "\n\n" + settings_text(m.from_user.id))


@router.message(Command("help"))
async def help_(m: Message):
    await m.answer(HELP)


@router.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Твой ID: <code>{m.from_user.id}</code>")


@router.message(Command("settings"))
async def settings(m: Message):
    await m.answer(settings_text(m.from_user.id), reply_markup=menu_kb())


@router.message(Command("watermark"))
async def watermark(m: Message):
    arg = (m.text or "").split(maxsplit=1)[1:] or [""]
    val = arg[0].strip()[:40]
    if val.lower() in ("off", "нет", "выкл", "-"):
        val = ""
    if not val and len((m.text or "").split()) == 1:
        cur = get_settings(m.from_user.id)["mark"] or "нет"
        return await m.answer(f"Сейчас: {html.escape(cur)}\nПример: <code>/watermark @mychannel</code>\n"
                              "Убрать: <code>/watermark off</code>")
    set_setting(m.from_user.id, "mark", val)
    await m.answer(f"✅ Ник на видео: {html.escape(val) or 'убран'}")


@router.message(Command("campaign"))
async def campaign(m: Message):
    """/campaign Oneshotmlbb #oneshotmlbb #ваншотмлбб — надпись канала на всё видео + обязательные теги."""
    parts = (m.text or "").split()[1:]
    uid = m.from_user.id
    if parts and parts[0].lower() in ("off", "выкл", "нет", "-"):
        set_setting(uid, "camp_label", "")
        set_setting(uid, "camp_tags", "")
        return await m.answer("✅ Кампания выключена.")
    if not parts:
        st = get_settings(uid)
        cur = (f"Сейчас: <b>{html.escape(st['camp_label'])}</b> {html.escape(st['camp_tags'])}"
               if st["camp_label"] else "Сейчас кампании нет.")
        return await m.answer(cur + "\n\nПример:\n<code>/campaign Oneshotmlbb #oneshotmlbb #ваншотмлбб</code>\n"
                              "Название канала будет крупно на всём видео, теги — первыми в тексте публикации.\n"
                              "Выключить: <code>/campaign off</code>")
    tags = " ".join(p for p in parts if p.startswith("#"))
    name = " ".join(p for p in parts if not p.startswith("#"))[:30]
    label = name if name.lower().startswith(("twitch", "kick", "youtube")) else f"TWITCH: {name}"
    set_setting(uid, "camp_label", label)
    set_setting(uid, "camp_tags", tags)
    await m.answer(f"✅ Кампания: на видео будет <b>{html.escape(label.upper())}</b>\n"
                   f"Теги: {html.escape(tags) or 'нет'}\n\nТеперь присылай стримы этого автора.")


@router.message(Command("music"))
async def music(m: Message):
    parts = (m.text or "").split()
    if len(parts) > 1 and parts[1] in ("clear", "очистить", "удалить"):
        for p in fx.music_files():
            p.unlink(missing_ok=True)
        return await m.answer("🗑 Музыка удалена.")
    files = fx.music_files()
    names = "\n".join(f"• {html.escape(p.name)}" for p in files[:20]) or "пока пусто"
    await m.answer("🎵 <b>Фоновая музыка</b>\nПришли мне аудиофайлы (mp3/m4a) — они сохранятся, и "
                   "к каждому клипу будет подмешиваться случайный трек (тише голоса).\n"
                   "Включить: /settings → ✨ Эффекты и звук.\n"
                   "⚠️ Бери музыку без авторских прав (YouTube Audio Library), иначе Content ID.\n\n"
                   f"Сейчас:\n{names}\n\nУдалить всё: <code>/music clear</code>")


@router.message(Command("queue"))
async def show_queue(m: Message):
    cur = STATE["job"]
    lines = [f"▶️ Сейчас: {cur.get('label', 'видео')}" if cur else "Сейчас ничего не обрабатывается."]
    pending = list(queue._queue)  # noqa: SLF001
    if pending:
        lines.append(f"⏳ В очереди: {len(pending)}")
        lines += [f"{i}. {j.get('label', 'видео')}" for i, j in enumerate(pending, 1)]
    await m.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cancel(m: Message):
    uid, removed = m.from_user.id, 0
    kept = []
    while not queue.empty():
        j = queue.get_nowait()
        queue.task_done()
        if j.get("uid") == uid:
            removed += 1
        else:
            kept.append(j)
    for j in kept:
        queue.put_nowait(j)
    running = STATE["job"] and STATE["job"].get("uid") == uid
    if running:
        STATE["cancel"] = True
    await m.answer("🛑 " + ("Останавливаю текущую задачу. " if running else "")
                   + (f"Убрал из очереди: {removed}." if removed else "")
                   if running or removed else "Нечего отменять.")


@router.message(Command("stop"))
async def stop_now(m: Message):
    await m.answer("⏸ Выключаю сервер… Включить снова: RunPod → Start.")
    ok, msg = await asyncio.to_thread(stop_pod)
    if not ok:
        await m.answer(f"Не получилось: {msg}")


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(c: CallbackQuery):
    sec = c.data.split(":", 1)[1]
    uid = c.from_user.id
    try:
        if sec == "root":
            await c.message.edit_text(settings_text(uid), reply_markup=menu_kb())
        else:
            await c.message.edit_text(f"{MENUS[sec][0]}\n\n" + settings_text(uid),
                                      reply_markup=section_kb(uid, sec))
    except TelegramBadRequest:
        pass
    await c.answer()


@router.callback_query(F.data.startswith("preset:"))
async def on_preset(c: CallbackQuery):
    name, vals = PRESETS[c.data.split(":", 1)[1]]
    for k, v in vals.items():
        set_setting(c.from_user.id, k, v)
    try:
        await c.message.edit_text(f"✅ Пресет «{name}»\n\n" + settings_text(c.from_user.id),
                                  reply_markup=menu_kb())
    except TelegramBadRequest:
        pass
    await c.answer(f"Пресет {name}")


@router.callback_query(F.data.startswith("set:"))
async def on_set(c: CallbackQuery):
    _, sec, key, val = c.data.split(":", 3)
    if key in ("clips", "autostop"):
        val = int(val)
    uid = c.from_user.id
    set_setting(uid, key, val)
    if key == "pick" and val == "seq":  # по порядку: всё видео частями до минуты
        set_setting(uid, "clips", 0)
        set_setting(uid, "length", "long")
    if key == "music" and val == "on" and not fx.music_files():
        await c.message.answer("🎵 Сначала пришли мне аудиофайлы с музыкой (см. /music).")
    try:
        await c.message.edit_text(f"{MENUS[sec][0]}\n\n" + settings_text(uid),
                                  reply_markup=section_kb(uid, sec))
    except TelegramBadRequest:
        pass
    await c.answer("Сохранено")


# ---------- Приём видео, музыки, текста ----------

def _sec(tc: str) -> float:
    parts = [int(x) for x in tc.split(":")]
    return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_overrides(text: str) -> dict:
    """«mlbb 2 https://…», «1:20-2:05», «топ» → настройки только для этого видео."""
    t = URL_RE.sub(" ", (text or "").lower())
    over = {}
    ranges = [(_sec(a), _sec(b)) for a, b in RANGE_RE.findall(t)]
    ranges = [(a, b) for a, b in ranges if b > a]
    if ranges:
        over["ranges"] = ranges
    t = RANGE_RE.sub(" ", t)
    if re.search(r"mlbb|млбб|mobile ?legends|мобайл ?легенд", t):
        over["content"] = "mlbb"
    elif re.search(r"pubg|пабг|пубг", t):
        over["content"] = "pubg"
    elif re.search(r"фильм|кино|movie|film|сериал", t):
        over["content"] = "movie"
    if re.search(r"\bтоп\b|\btop\b|подборк", t):
        over["pick"] = "top"
    num = re.search(r"(?<![\d/:.])(\d{1,2})(?![\d/:.])", t)
    if num and 1 <= int(num.group(1)) <= 20 and "top" not in over.get("pick", ""):
        over["clips"] = int(num.group(1))
    if ("content" in over or "clips" in over) and "pick" not in over:
        over["pick"] = "best"
    return over


def describe_over(over: dict) -> str:
    if not over:
        return ""
    bits = []
    if "content" in over:
        bits.append(_label("content", over["content"]))
    if over.get("pick") == "top":
        bits.append("🏆 Топ-5")
    if "clips" in over:
        bits.append(f"клипов: {over['clips']}")
    if over.get("ranges"):
        bits.append("куски: " + ", ".join(f"{fmt(a)}–{fmt(b)}" for a, b in over["ranges"]))
    return "\n🎯 Для этого видео: " + ", ".join(bits)


async def enqueue(m: Message, job: dict):
    pos = queue.qsize() + (1 if STATE["busy"] else 0)
    status = await m.answer(
        "📥 Принял! " + (f"В очереди: {pos}." if pos else "Начинаю…")
        + describe_over(job.get("over") or {}) + "\n" + settings_text(m.from_user.id))
    job.update(chat_id=m.chat.id, uid=m.from_user.id, status_id=status.message_id)
    await queue.put(job)


@router.message(F.audio | (F.document & F.document.mime_type.startswith("audio")))
async def on_audio(m: Message, bot: Bot):
    f = m.audio or m.document
    name = re.sub(r"[^\w.\- ]", "_", f.file_name or f"track_{f.file_unique_id}.mp3")
    tg_file = await bot.get_file(f.file_id)
    dst = fx.MUSIC_DIR / name
    if LOCAL and os.path.isabs(tg_file.file_path) and os.path.exists(tg_file.file_path):
        shutil.copy(tg_file.file_path, dst)
    else:
        await bot.download_file(tg_file.file_path, destination=dst)
    await m.answer(f"🎵 Сохранил «{html.escape(name)}». Треков: {len(fx.music_files())}.\n"
                   "Включи музыку: /settings → ✨ Эффекты и звук.")


@router.message(F.video | F.document | F.video_note)
async def on_file(m: Message):
    f = m.video or m.video_note or m.document
    if m.document and not (m.document.mime_type or "").startswith("video"):
        return await m.answer("Это не видео 🙂 Пришли видеофайл, ссылку или аудио для музыки.")
    if f.file_size and f.file_size > DOWNLOAD_LIMIT:
        mb = DOWNLOAD_LIMIT // 1024 // 1024
        return await m.answer(f"Файл больше {mb} МБ — Telegram не даст боту его скачать. "
                              "Скачай видео в 480p/720p или обрежь до нужного куска и пришли снова.")
    await enqueue(m, {"type": "new", "src": "file", "file_id": f.file_id, "label": "видеофайл",
                      "over": parse_overrides(m.caption or "")})


@router.message(F.text.regexp(URL_RE))
async def on_link(m: Message):
    url = URL_RE.search(m.text).group(0)
    await enqueue(m, {"type": "new", "src": "url", "url": url, "label": url[:40],
                      "over": parse_overrides(m.text)})


@router.message(F.text)
async def other(m: Message):
    await m.answer("Пришли видеофайл или ссылку на видео. /help — подсказка, /settings — настройки.")


# ---------- Кнопки после анализа / монтажа ----------

def pick_kb(sid: str) -> InlineKeyboardMarkup:
    s = SESSIONS[sid]
    btns = [InlineKeyboardButton(text=("✅ " if i in s["sel"] else "⬜ ") + str(i + 1),
                                 callback_data=f"sel:{sid}:{i}") for i in range(len(s["clips"]))]
    rows = [btns[i:i + 5] for i in range(0, len(btns), 5)]
    rows.append([InlineKeyboardButton(text=f"🎬 Монтировать ({len(s['sel'])})", callback_data=f"go:{sid}"),
                 InlineKeyboardButton(text="❌ Отмена", callback_data=f"end:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def after_kb(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё 3 клипа", callback_data=f"more:{sid}"),
         InlineKeyboardButton(text="🏆 Топ-5 нарезка", callback_data=f"top:{sid}")],
        [InlineKeyboardButton(text="✅ Закончить с этим видео", callback_data=f"end:{sid}")],
    ])


async def _session_or_warn(c: CallbackQuery, sid: str):
    s = SESSIONS.get(sid)
    if not s:
        await c.answer("Это видео уже удалено с сервера — пришли его снова.", show_alert=True)
    return s


@router.callback_query(F.data.startswith("sel:"))
async def on_sel(c: CallbackQuery):
    _, sid, i = c.data.split(":")
    s = await _session_or_warn(c, sid)
    if not s:
        return
    i = int(i)
    s["sel"] ^= {i}
    try:
        await c.message.edit_reply_markup(reply_markup=pick_kb(sid))
    except TelegramBadRequest:
        pass
    await c.answer()


@router.callback_query(F.data.startswith(("go:", "more:", "top:")))
async def on_go(c: CallbackQuery):
    kind, sid = c.data.split(":")
    s = await _session_or_warn(c, sid)
    if not s:
        return
    if kind == "go" and not s["sel"]:
        return await c.answer("Отметь хотя бы один момент", show_alert=True)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    labels = {"go": "монтаж выбранных", "more": "ещё 3 клипа", "top": "топ-5 нарезка"}
    status = await c.message.answer(f"📥 {labels[kind].capitalize()} — "
                                    + ("в очереди." if STATE["busy"] or queue.qsize() else "начинаю…"))
    await queue.put({"type": kind, "sid": sid, "chat_id": c.message.chat.id, "uid": c.from_user.id,
                     "status_id": status.message_id, "label": labels[kind]})
    await c.answer()


@router.callback_query(F.data.startswith("end:"))
async def on_end(c: CallbackQuery):
    sid = c.data.split(":")[1]
    drop_session(sid)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await c.answer("Готово, файлы удалены")


# ---------- Сессии (видео, оставленное для «ещё» и выбора) ----------

def drop_session(sid: str):
    s = SESSIONS.pop(sid, None)
    if s:
        shutil.rmtree(s["workdir"], ignore_errors=True)
        if s.get("local_src"):
            Path(s["local_src"]).unlink(missing_ok=True)


def keep_session(s: dict) -> str:
    # держим не больше 2 видео, чтобы не забить диск
    while len(SESSIONS) >= 2:
        drop_session(min(SESSIONS, key=lambda k: SESSIONS[k]["ts"]))
    sid = uuid.uuid4().hex[:8]
    s["ts"] = time.time()
    SESSIONS[sid] = s
    return sid


async def janitor():
    while True:
        await asyncio.sleep(600)
        for sid in [k for k, s in SESSIONS.items() if time.time() - s["ts"] > 3 * 3600]:
            drop_session(sid)


# ---------- Автостоп RunPod ----------

def stop_pod():
    pod = os.getenv("RUNPOD_POD_ID")
    if not pod:
        return False, "это не RunPod (нет RUNPOD_POD_ID)"
    if shutil.which("runpodctl"):
        r = subprocess.run(["runpodctl", "stop", "pod", pod], capture_output=True, text=True)
        if r.returncode == 0:
            return True, ""
    key = os.getenv("RUNPOD_API_KEY")
    if key:
        import urllib.request
        q = json.dumps({"query": f'mutation {{ podStop(input: {{podId: "{pod}"}}) {{ id }} }}'})
        req = urllib.request.Request(f"https://api.runpod.io/graphql?api_key={key}", data=q.encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return True, ""
        except Exception as e:
            return False, str(e)[:200]
    return False, "нет runpodctl и RUNPOD_API_KEY"


async def autostop(bot: Bot):
    while True:
        await asyncio.sleep(60)
        mins = get_global().get("autostop", 0)
        if not mins or STATE["busy"] or not queue.empty() or not os.getenv("RUNPOD_POD_ID"):
            continue
        idle = (time.time() - STATE["last"]) / 60
        users = list(cfg.ALLOWED_USERS)
        if idle >= mins - 2 and not STATE["warned"]:
            STATE["warned"] = True
            for u in users:
                try:
                    await bot.send_message(u, "⏳ Без дела уже долго — через 2 минуты выключу сервер, "
                                              "чтобы не тратить деньги. Напиши что угодно, чтобы продлить.")
                except Exception:
                    pass
        elif idle >= mins and STATE["warned"]:
            for u in users:
                try:
                    await bot.send_message(u, "⏸ Выключаю сервер. Включить: RunPod → Start "
                                              "(если GPU занята — выбери Migrate).")
                except Exception:
                    pass
            ok, msg = await asyncio.to_thread(stop_pod)
            if not ok:
                log.warning("autostop failed: %s", msg)
                STATE["last"] = time.time()


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
    try:
        with YoutubeDL(opts) as y:
            y.extract_info(url, download=True)
    except Exception as e:
        if "not a bot" in str(e) or "Sign in" in str(e):
            raise RuntimeError("YouTube не даёт скачать с сервера (просит вход). "
                               "Скачай видео на телефон и пришли сюда файлом — до 2 ГБ.") from None
        raise
    files = sorted(outdir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Не удалось скачать видео по ссылке")
    return files[0]


def fmt(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


TAGS = {"mlbb": "#mlbb #mobilelegends #shorts", "pubg": "#pubgmobile #pubg #shorts",
        "movie": "#фильм #кино #shorts", "auto": "#shorts"}
TYPE_ICON = {"kill": "🔥 Килл-момент", "tip": "💡 Совет", "funny": "😂 Смешной момент"}


def caption_for(r: dict, st: dict, segments, lang) -> str:
    kind = TYPE_ICON.get(r.get("type", ""), "") if r.get("content") in pipeline.GAMES else ""
    r = {**r, "title": (r.get("title") or "").replace("*", "")}
    head = (f"<b>{html.escape(r['title'] or 'Клип ' + str(r['index']))}</b>\n"
            + (f"{kind}\n" if kind else "")
            + (f"⏱ {fmt(r['start'])}–{fmt(r['end'])} из оригинала\n" if not r.get("text") else ""))
    if st.get("pubtext") != "on":
        return head + "\n" + (st.get("camp_tags", "") + " " + TAGS.get(r.get("content", "auto"), "#shorts")).strip()
    lang = st["subs"] if st["subs"] in ai.LANG_NAMES else (lang if lang in ai.LANG_NAMES else "ru")
    p = ai.publish_text(r, segments, r.get("content", "auto"), lang, st.get("camp_tags", ""))
    body = (f"\n📋 <b>Для публикации</b> (нажми, чтобы скопировать)\nНазвание:\n"
            f"<code>{html.escape(p['title'])}</code>\nОписание:\n"
            f"<code>{html.escape(p['desc'])}\n\n{html.escape(p['tags'])}</code>")
    return (head + body)[:1024]


async def worker(bot: Bot):
    loop = asyncio.get_running_loop()
    while True:
        job = await queue.get()
        STATE.update(busy=True, cancel=False, job=job)
        chat, sid_msg = job["chat_id"], job["status_id"]
        last = {"t": 0.0, "text": ""}
        session_kept = False
        workdir = local_src = None

        async def set_status(text: str, force=False, kb=None):
            if text == last["text"] or (not force and time.time() - last["t"] < 3):
                return
            last.update(t=time.time(), text=text)
            try:
                await bot.edit_message_text(text, chat_id=chat, message_id=sid_msg, reply_markup=kb)
            except TelegramBadRequest:
                pass

        def progress(text: str):
            if STATE["cancel"]:
                raise pipeline.Cancelled()
            asyncio.run_coroutine_threadsafe(set_status(text), loop)

        def cancelled():
            return STATE["cancel"]

        async def send_clip(r, st, A):
            if r["path"].stat().st_size > UPLOAD_LIMIT:
                await bot.send_message(chat, f"Клип {r['index']} слишком большой для отправки.")
                return
            cap = await asyncio.to_thread(caption_for, r, st, A["segments"], A["lang"])
            await bot.send_video(chat, FSInputFile(r["path"]), caption=cap, width=1080, height=1920,
                                 duration=int(r["end"] - r["start"]), supports_streaming=True,
                                 request_timeout=600)

        def on_clip_factory(st, A):
            def on_clip(r):
                asyncio.run_coroutine_threadsafe(send_clip(r, st, A), loop).result()
            return on_clip

        try:
            if job["type"] == "new":
                workdir = cfg.DATA_DIR / "jobs" / uuid.uuid4().hex[:10]
                workdir.mkdir(parents=True, exist_ok=True)
                await set_status("⬇️ Скачиваю видео…", force=True)
                if job["src"] == "url":
                    src = await asyncio.to_thread(ytdl, job["url"], workdir)
                else:
                    tg_file = await bot.get_file(job["file_id"])
                    if LOCAL and os.path.isabs(tg_file.file_path) and os.path.exists(tg_file.file_path):
                        src = local_src = Path(tg_file.file_path)  # файл уже на диске сервера
                    else:
                        src = workdir / "source.mp4"
                        await bot.download_file(tg_file.file_path, destination=src)
                st = {**get_settings(job["uid"]), **job.get("over", {})}
                if st.get("review") == "on" and not cfg.ANTHROPIC_API_KEY:
                    await bot.send_message(chat, "⚠️ Для «Обзора» нужен ключ Claude API. Режу без комментариев.")
                A = await asyncio.to_thread(pipeline.analyze, src, st, progress)
                s = {"uid": job["uid"], "chat": chat, "src": src, "workdir": workdir, "st": st, "A": A,
                     "used": [], "clips": [], "sel": set(), "local_src": local_src}
                sid = keep_session(s)
                session_kept = True
                if st.get("pick") == "top":
                    await _do_top(bot, s, sid, progress, send_clip, set_status)
                    continue
                clips = await asyncio.to_thread(pipeline.choose, A, st, int(st.get("clips", 5)))
                if not clips:
                    raise RuntimeError("Не удалось найти подходящие фрагменты")
                if st.get("confirm") == "on" and not st.get("ranges"):
                    s["clips"], s["sel"] = clips, set(range(len(clips)))
                    await _send_previews(bot, chat, src, clips)
                    await set_status(f"👀 Нашёл {len(clips)} моментов. Отметь нужные и жми «Монтировать».",
                                     force=True, kb=pick_kb(sid))
                    continue
                await _do_render(bot, s, sid, clips, progress, on_clip_factory, set_status, cancelled)
            else:
                sid = job["sid"]
                s = SESSIONS.get(sid)
                if not s:
                    raise RuntimeError("Видео уже удалено с сервера — пришли его снова.")
                session_kept = True
                s["ts"] = time.time()
                if job["type"] == "go":
                    clips = [c for i, c in enumerate(s["clips"]) if i in s["sel"]]
                    await _do_render(bot, s, sid, clips, progress, on_clip_factory, set_status, cancelled)
                elif job["type"] == "more":
                    clips = await asyncio.to_thread(pipeline.choose, s["A"], {**s["st"], "ranges": None},
                                                    3, s["used"])
                    if not clips:
                        await set_status("Больше ярких моментов не нашёл 🤷", force=True, kb=after_kb(sid))
                        continue
                    await _do_render(bot, s, sid, clips, progress, on_clip_factory, set_status, cancelled)
                elif job["type"] == "top":
                    await _do_top(bot, s, sid, progress, send_clip, set_status)
        except pipeline.Cancelled:
            await set_status("🛑 Отменено.", force=True)
        except Exception as e:
            log.exception("job failed")
            await set_status(f"❌ Ошибка: {html.escape(str(e)[:300])}", force=True)
        finally:
            if not session_kept and workdir:
                shutil.rmtree(workdir, ignore_errors=True)
                if local_src:
                    local_src.unlink(missing_ok=True)
            STATE.update(busy=False, cancel=False, job=None, last=time.time(), warned=False)
            queue.task_done()


async def _do_render(bot, s, sid, clips, progress, on_clip_factory, set_status, cancelled):
    st, A = s["st"], s["A"]
    res = await asyncio.to_thread(pipeline.render, s["src"], s["workdir"], st, A, clips, progress,
                                  on_clip_factory(st, A), cancelled)
    s["used"] += [(c["start"], c["end"]) for c in clips]
    await set_status(f"✅ Готово! {len(res)} шортс(ов). Что дальше?", force=True, kb=after_kb(sid))


async def _do_top(bot, s, sid, progress, send_clip, set_status):
    st, A = s["st"], s["A"]
    r = await asyncio.to_thread(pipeline.compile_top, s["src"], s["workdir"], st, A, progress)
    r.update(index=1, total=1)
    await send_clip(r, st, A)
    r["path"].unlink(missing_ok=True)
    await set_status("✅ Топ-5 готов! Что дальше?", force=True, kb=after_kb(sid))


async def _send_previews(bot, chat, src, clips):
    def grab():
        out = []
        for i, c in enumerate(clips[:10]):
            p = src.parent / f"prev_{i}.jpg"
            t = c["start"] + min(3.0, (c["end"] - c["start"]) / 2)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src),
                            "-frames:v", "1", "-vf", "scale=640:-2", str(p)])
            out.append(p)
        return out
    frames = await asyncio.to_thread(grab)
    media = []
    for i, (c, p) in enumerate(zip(clips, frames)):
        if p.exists():
            cap = f"{i + 1}. {fmt(c['start'])}–{fmt(c['end'])} {html.escape((c.get('title') or '').replace('*', ''))}"
            media.append(InputMediaPhoto(media=FSInputFile(p), caption=cap))
    if media:
        await bot.send_media_group(chat, media)
    if len(clips) > 10:
        rest = "\n".join(f"{i + 1}. {fmt(c['start'])}–{fmt(c['end'])} {html.escape((c.get('title') or '').replace('*', ''))}"
                         for i, c in enumerate(clips[10:], 10))
        await bot.send_message(chat, rest)


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
    asyncio.create_task(janitor())
    asyncio.create_task(autostop(bot))
    log.info("Bot started (local API: %s, LLM: %s)", LOCAL, bool(cfg.ANTHROPIC_API_KEY))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
