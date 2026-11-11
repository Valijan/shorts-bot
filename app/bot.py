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
            "camp_label": "", "camp_tags": "", "camp_id": "", "camp_min": 0}
GLOBAL_DEFAULTS = {"autostop": 30}
OPTIONS = {
    "content": [("🎬 Обычное", "auto"), ("🎥 Фильм", "movie"), ("⚔️ MLBB", "mlbb"), ("🔫 PUBG M", "pubg"),
                ("🚗 GTA 6", "gta")],
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
    "gta": ("🚗 GTA 6-стрим", {"content": "gta", "pick": "best", "length": "auto", "mode": "game",
                              "title": "hook", "sub_words": "3", "sub_pos": "bottom", "fx": "auto",
                              "jumpcut": "off", "teaser": "on", "director": "on"}),
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
        [InlineKeyboardButton(text="💰 Кампании (условия блогеров)", callback_data="camp:list:-")],
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
            f"Ник на видео: {html.escape(st['mark']) or 'нет (/watermark)'} · Автостоп: {stop}\n"
            f"💰 Кампания: {html.escape(st['camp_label']) or 'нет (/campaigns)'}")


# ---------- Кампании (клиппинг у разных блогеров) ----------

ONESHOT = {
    "name": "Oneshot", "label": "TWITCH: Oneshotmlbb", "tags": "#oneshotmlbb #ваншотмлбб",
    "min_len": 15, "game": "mlbb", "contact": "@rozhok_23", "window": "2 недели",
    "rates": "50 000 — 1 000 ₽\n100 000 — 3 000 ₽\n150 000 — 5 000 ₽\n200 000 — 8 000 ₽\n"
             "300 000 — 15 000 ₽\n400 000 — 20 000 ₽",
    "rules": "Канал Twitch крупным шрифтом в видео. Без накрутки, без чужого контента и повторов.",
}


def get_camps(uid) -> dict:
    data = load_all()
    camps = data.get(f"camps:{uid}")
    if camps is None:  # первый запуск — сразу кладём Oneshot
        camps = {"oneshot": ONESHOT}
        data[f"camps:{uid}"] = camps
        SETTINGS_FILE.write_text(json.dumps(data))
    return camps


def save_camps(uid, camps: dict):
    data = load_all()
    data[f"camps:{uid}"] = camps
    SETTINGS_FILE.write_text(json.dumps(data))


def parse_campaign(text: str) -> dict:
    """Достаёт условия из объявления блогера или из строк «Ключ: значение»."""
    t = text or ""
    c = {}
    kv = dict((k.strip().lower(), v.strip()) for k, v in re.findall(r"^\s*([^:\n]{2,20}):\s*(.+)$", t, re.M))
    c["name"] = kv.get("имя") or kv.get("name") or ""
    tags = re.findall(r"#[\wа-яё]+", t, re.I)
    c["tags"] = " ".join(dict.fromkeys(tags))
    m = re.search(r"(twitch|kick|youtube|тви[тч]\w*)[^\n]{0,30}?\b([A-Za-z0-9_]{3,25})\b", t, re.I)
    chan = kv.get("канал") or (m.group(2) if m else "")
    if chan and not chan.lower().startswith(("twitch", "kick", "youtube")):
        plat = "KICK" if m and "kick" in m.group(1).lower() else "TWITCH"
        chan = f"{plat}: {chan}"
    c["label"] = chan
    mn = re.search(r"от\s*(\d{1,3})\s*сек", t, re.I) or re.search(r"мин\w*\.? длин\w*:\s*(\d+)", t, re.I)
    c["min_len"] = int(mn.group(1)) if mn else 15
    contacts = [x for x in re.findall(r"@[A-Za-z0-9_]{4,32}", t)]
    c["contact"] = kv.get("контакт") or (contacts[-1] if contacts else "")
    rates = [ln.strip("•-– ").strip() for ln in t.splitlines() if re.search(r"\d[\d .]*\s*(₽|руб|\$|сум)", ln)]
    c["rates"] = kv.get("оплата") or "\n".join(rates[:10])
    w = re.search(r"в течени[еи]\s+(\d+\s+[а-яёa-z]+|[а-яё]+\s+[а-яё]+)", t, re.I)
    c["window"] = kv.get("срок") or (w.group(1).strip() if w else "")
    g = re.search(r"mlbb|млбб|mobile legends|pubg|пабг|gta|гта", t, re.I)
    c["game"] = ("pubg" if re.search(r"pubg|пабг", g.group(0), re.I) else
                 "gta" if re.search(r"gta|гта", g.group(0), re.I) else "mlbb") if g else ""
    if not c["name"]:
        base = c["label"].split(":")[-1].strip() if c["label"] else (c["tags"].split()[0][1:] if c["tags"] else "")
        c["name"] = base[:20] or "Кампания"
    c["rules"] = kv.get("правила", "")
    return c


def camp_card(c: dict, active: bool = False) -> str:
    return (f"{'✅ ' if active else ''}<b>{html.escape(c['name'])}</b>\n"
            f"📺 На видео: {html.escape(c.get('label') or '—')}\n"
            f"🏷 Теги: {html.escape(c.get('tags') or '—')}\n"
            f"⏱ От {c.get('min_len', 15)} с · просмотры в течение: {html.escape(c.get('window') or '—')}\n"
            f"💰 Оплата:\n{html.escape(c.get('rates') or '—')}\n"
            f"💸 Модерация/выплаты: {html.escape(c.get('contact') or '—')}"
            + (f"\n📌 {html.escape(c['rules'])}" if c.get("rules") else ""))


def camps_kb(uid) -> InlineKeyboardMarkup:
    camps, active = get_camps(uid), get_settings(uid).get("camp_id", "")
    rows = []
    for cid, c in camps.items():
        rows.append([InlineKeyboardButton(text=("✅ " if cid == active else "▶️ ") + c["name"],
                                          callback_data=f"camp:on:{cid}"),
                     InlineKeyboardButton(text="ℹ️", callback_data=f"camp:info:{cid}"),
                     InlineKeyboardButton(text="🗑", callback_data=f"camp:del:{cid}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить блогера", callback_data="camp:add:-"),
                 InlineKeyboardButton(text="⏹ Без кампании", callback_data="camp:off:-")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def activate_camp(uid, cid: str | None):
    if not cid:
        for k in ("camp_id", "camp_label", "camp_tags"):
            set_setting(uid, k, "")
        set_setting(uid, "camp_min", 0)
        return
    c = get_camps(uid)[cid]
    set_setting(uid, "camp_id", cid)
    set_setting(uid, "camp_label", c.get("label", ""))
    set_setting(uid, "camp_tags", c.get("tags", ""))
    set_setting(uid, "camp_min", int(c.get("min_len") or 0))
    if c.get("game") in pipeline.GAMES:
        set_setting(uid, "content", c["game"])


CAMPS_HELP = ("💰 <b>Кампании</b> — условия блогеров, которые платят за клипы.\n"
              "Включённая кампания сама ставит канал автора крупно на видео, его теги и мин. длину.\n\n"
              "Добавить: <code>/addcampaign</code> + вставь объявление блогера целиком "
              "(бот сам найдёт канал, теги, ставки, контакт).")


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
    "• <code>mlbb 2</code>, <code>pubg</code>, <code>gta 10</code>, <code>фильм 5</code> — тип видео и число клипов\n"
    "• <code>1:20-2:05</code> — вырезать именно этот кусок (можно несколько через запятую)\n"
    "• <code>gta 2:00:00-4:00:00 ссылка</code> — из длинного эфира скачать и разобрать только это окно\n"
    "• ссылка на запись Twitch — бот сам смотрит, где взрывался чат\n"
    "• <code>топ</code> — один шортс «Топ-5 моментов»\n\n"
    "/settings — настройки · /watermark @ник — ник на видео\n"
    "/campaigns — 💰 кампании блогеров · /addcampaign — добавить\n"
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


@router.message(Command("campaigns"))
async def campaigns(m: Message):
    await m.answer(CAMPS_HELP, reply_markup=camps_kb(m.from_user.id))


@router.message(Command("addcampaign"))
async def add_campaign(m: Message):
    body = (m.text or "").split(maxsplit=1)[1:] or [""]
    if not body[0].strip():
        return await m.answer("Отправь одним сообщением: <code>/addcampaign</code> и сразу ниже — текст "
                              "объявления блогера (условия, теги, канал, контакт).\n\nИли вручную:\n"
                              "<code>/addcampaign\nИмя: Блогер\nКанал: TWITCH: nick\nТеги: #tag1 #tag2\n"
                              "Мин. длина: 15\nОплата: 100к — 2000 ₽\nКонтакт: @manager\nСрок: 2 недели</code>")
    c = parse_campaign(body[0])
    uid = m.from_user.id
    camps = get_camps(uid)
    cid = re.sub(r"\W+", "", c["name"].lower())[:20] or uuid.uuid4().hex[:6]
    camps[cid] = c
    save_camps(uid, camps)
    await m.answer("✅ Добавил кампанию. Проверь, всё ли верно (если нет — пришли заново со строками "
                   "«Канал:», «Теги:» и т.д.):\n\n" + camp_card(c),
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                       InlineKeyboardButton(text="▶️ Включить сейчас", callback_data=f"camp:on:{cid}"),
                       InlineKeyboardButton(text="💰 Все кампании", callback_data="camp:list:-")]]))


@router.callback_query(F.data.startswith("camp:"))
async def on_camp(c: CallbackQuery):
    _, act, cid = c.data.split(":", 2)
    uid = c.from_user.id
    camps = get_camps(uid)
    text = CAMPS_HELP
    if act == "on" and cid in camps:
        activate_camp(uid, cid)
        text = "✅ Включена кампания:\n\n" + camp_card(camps[cid], True)
    elif act == "off":
        activate_camp(uid, None)
        text = "⏹ Кампания выключена — клипы без чужого канала и тегов.\n\n" + CAMPS_HELP
    elif act == "info" and cid in camps:
        text = camp_card(camps[cid], get_settings(uid).get("camp_id") == cid)
    elif act == "del" and cid in camps:
        name = camps.pop(cid)["name"]
        save_camps(uid, camps)
        if get_settings(uid).get("camp_id") == cid:
            activate_camp(uid, None)
        text = f"🗑 Удалил «{html.escape(name)}».\n\n" + CAMPS_HELP
    elif act == "add":
        await c.message.answer("Отправь <code>/addcampaign</code> и сразу ниже вставь объявление блогера.")
        return await c.answer()
    try:
        await c.message.edit_text(text, reply_markup=camps_kb(uid))
    except TelegramBadRequest:
        await c.message.answer(text, reply_markup=camps_kb(uid))
    await c.answer()


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
    elif re.search(r"\bgta\b|\bгта\b|gta ?(6|vi)\b|гта ?6", t):
        over["content"] = "gta"
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


SPAN_MIN = 300  # кусок длиннее 5 минут — это не клип, а окно эфира для разбора


def split_span(over: dict) -> dict:
    """Для ссылок: «2:00:00-4:00:00» значит «скачай и разбери только это окно»."""
    long = [r for r in over.get("ranges", []) if r[1] - r[0] > SPAN_MIN]
    if not long:
        return over
    over = dict(over)
    over["span"] = long[0]
    rest = [r for r in over["ranges"] if r[1] - r[0] <= SPAN_MIN]
    if rest:
        over["ranges"] = rest
    else:
        over.pop("ranges")
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
    if over.get("span"):
        bits.append(f"окно эфира: {fmt(over['span'][0])}–{fmt(over['span'][1])}")
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
                      "over": split_span(parse_overrides(m.text))})


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

MAX_HOURS = float(os.getenv("MAX_HOURS", "6"))  # длиннее — просим указать окно


def ytdl(url: str, outdir: Path, span=None) -> Path:
    """Скачивает видео по ссылке. span=(с, по) в секундах — только этот кусок эфира."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import download_range_func

    base = {"noplaylist": True, "quiet": True, "no_warnings": True}
    if cfg.YTDLP_COOKIES and Path(cfg.YTDLP_COOKIES).exists():
        base["cookiefile"] = cfg.YTDLP_COOKIES
    try:
        with YoutubeDL(base) as y:
            dur = float((y.extract_info(url, download=False) or {}).get("duration") or 0)
        if span:
            if dur and span[0] >= dur:
                raise RuntimeError(f"Окно начинается после конца видео (оно идёт {fmt(dur)}).")
            span = (span[0], min(span[1], dur) if dur else span[1])
        length = (span[1] - span[0]) if span else dur
        if length > MAX_HOURS * 3600:
            raise RuntimeError(
                f"Эфир идёт {fmt(dur)} — целиком это слишком долго и тяжело. Укажи окно до "
                f"{int(MAX_HOURS)} часов, например:\ngta 2:00:00-4:00:00 {url}")
        h = 1080 if length <= 5400 else 720   # длинные эфиры качаем в 720p — иначе не хватит диска
        opts = {**base, "outtmpl": str(outdir / "source.%(ext)s"),
                "format": f"bv*[height<={h}]+ba/b[height<={h}]/b", "merge_output_format": "mp4"}
        if span:
            opts["download_ranges"] = download_range_func(None, [span])
        with YoutubeDL(opts) as y:
            y.extract_info(url, download=True)
    except RuntimeError:
        raise
    except Exception as e:
        if "not a bot" in str(e) or "Sign in" in str(e):
            raise RuntimeError("YouTube не даёт скачать с сервера (просит вход). "
                               "Скачай видео на телефон и пришли сюда файлом — до 2 ГБ.") from None
        if "subscriber" in str(e).lower():
            raise RuntimeError("Эта запись Twitch только для подписчиков канала — скачать её нельзя.") from None
        raise
    files = sorted(outdir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Не удалось скачать видео по ссылке")
    return files[0]


def fmt(t: float) -> str:
    if t >= 3600:
        return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"
    return f"{int(t // 60)}:{int(t % 60):02d}"


TAGS = {"mlbb": "#mlbb #mobilelegends #shorts", "pubg": "#pubgmobile #pubg #shorts",
        "gta": "#gta6 #gta #shorts",
        "movie": "#фильм #кино #shorts", "auto": "#shorts"}
TYPE_ICON = {"kill": "🔥 Килл-момент", "tip": "💡 Совет", "funny": "😂 Смешной момент",
             "action": "🚔 Экшен"}


def caption_for(r: dict, st: dict, segments, lang) -> str:
    kind = TYPE_ICON.get(r.get("type", ""), "") if r.get("content") in pipeline.GAMES else ""
    r = {**r, "title": (r.get("title") or "").replace("*", "")}
    off = float(st.get("_offset") or 0)  # клип вырезан из окна эфира — показываем время оригинала
    head = (f"<b>{html.escape(r['title'] or 'Клип ' + str(r['index']))}</b>\n"
            + (f"{kind}\n" if kind else "")
            + (f"⏱ {fmt(r['start'] + off)}–{fmt(r['end'] + off)} из оригинала\n" if not r.get("text") else ""))
    if st.get("pubtext") != "on":
        return head + "\n" + (st.get("camp_tags", "") + " " + TAGS.get(r.get("content", "auto"), "#shorts")).strip()
    lang = st["subs"] if st["subs"] in ai.LANG_NAMES else (lang if lang in ai.LANG_NAMES else "ru")
    p = ai.publish_text(r, segments, r.get("content", "auto"), lang, st.get("camp_tags", ""))
    body = (f"\n📋 <b>Для публикации</b> (нажми, чтобы скопировать)\nНазвание:\n"
            f"<code>{html.escape(p['title'])}</code>\nОписание:\n"
            f"<code>{html.escape(p['desc'])}\n\n{html.escape(p['tags'])}</code>")
    camp = get_camps(st["_uid"]).get(st.get("camp_id", "")) if st.get("_uid") and st.get("camp_id") else None
    if camp:
        body += (f"\n\n💰 {html.escape(camp['name'])}: просмотры в течение {html.escape(camp.get('window') or '—')}, "
                 f"выплаты — {html.escape(camp.get('contact') or '—')}")
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
                    span = job.get("over", {}).get("span")
                    if span:
                        await set_status(f"⬇️ Скачиваю окно эфира {fmt(span[0])}–{fmt(span[1])}…", force=True)
                    src = await asyncio.to_thread(ytdl, job["url"], workdir, span)
                else:
                    tg_file = await bot.get_file(job["file_id"])
                    if LOCAL and os.path.isabs(tg_file.file_path) and os.path.exists(tg_file.file_path):
                        src = local_src = Path(tg_file.file_path)  # файл уже на диске сервера
                    else:
                        src = workdir / "source.mp4"
                        await bot.download_file(tg_file.file_path, destination=src)
                st = {**get_settings(job["uid"]), **job.get("over", {}), "_uid": job["uid"],
                      "_url": job.get("url") if job["src"] == "url" else None,
                      "_offset": (job.get("over", {}).get("span") or (0,))[0] if job["src"] == "url" else 0}
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
