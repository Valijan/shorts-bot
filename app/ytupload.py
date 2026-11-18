"""Публикация на YouTube через браузер (YouTube Studio), как будто загружает сам владелец.

Отдельный профиль Chrome в DATA_DIR/yt-profile: один раз входим в Google руками (login_window),
дальше бот загружает сам и без окон. Если в Studio не нашлась настройка 18+ для взрослого клипа —
загружаем приватно, чтобы ничего не нарушить. При любой ошибке сохраняем скриншот для владельца.
"""
import logging
import re
import shutil
import time
from pathlib import Path

from . import config as cfg

log = logging.getLogger(__name__)

PROFILE = cfg.DATA_DIR / "yt-profile"
STUDIO = "https://studio.youtube.com"
UPLOAD = "https://www.youtube.com/upload"


def _context(p, headless: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    opts = dict(headless=headless, viewport=None if not headless else {"width": 1400, "height": 900},
                locale="ru-RU", args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
                ignore_default_args=["--enable-automation"])
    for channel in ("chrome", "msedge", None):   # настоящий Chrome Google пускает охотнее
        try:
            return p.chromium.launch_persistent_context(str(PROFILE), channel=channel, **opts) if channel \
                else p.chromium.launch_persistent_context(str(PROFILE), **opts)
        except Exception as e:
            log.info("browser %s: %s", channel, str(e)[:120])
    raise RuntimeError("не нашёл браузер: поставь Google Chrome или выполни «python -m playwright install chromium»")


def _logged_in(page) -> bool:
    return "accounts.google.com" not in page.url and "ServiceLogin" not in page.url


def login_window(timeout: int = 900) -> tuple[bool, str]:
    """Открывает окно на ПК: владелец входит в Google, бот ждёт до timeout секунд."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(STUDIO, wait_until="domcontentloaded")
        t0 = time.time()
        while time.time() - t0 < timeout:
            if _logged_in(page) and "studio.youtube.com" in page.url and "/channel/" in page.url:
                name = ""
                try:
                    name = page.title().replace("- YouTube Studio", "").strip()
                except Exception:
                    pass
                time.sleep(3)
                ctx.close()
                return True, name
            time.sleep(2)
        ctx.close()
        return False, "не дождался входа (15 минут)"


def status() -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright
    if not PROFILE.exists():
        return False, "вход не выполнен"
    with sync_playwright() as p:
        ctx = _context(p, headless=True)
        page = ctx.new_page()
        try:
            page.goto(STUDIO, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            ok = _logged_in(page) and "/channel/" in page.url
            return ok, (page.title().replace("- YouTube Studio", "").strip() if ok else "вход не выполнен")
        finally:
            ctx.close()


def _click_first(page, selectors, timeout=4000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            return True
        except Exception:
            continue
    return False


def _type_box(page, sel: str, text: str):
    box = page.locator(sel).first
    box.wait_for(state="visible", timeout=180000)
    box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.insert_text(text)


def upload(path: Path, title: str, desc: str, adult: bool, visibility: str = "PUBLIC",
           shot_dir: Path | None = None) -> dict:
    """{'ok', 'url', 'error', 'shot', 'private_fallback'}"""
    from playwright.sync_api import sync_playwright
    shot_dir = shot_dir or (cfg.DATA_DIR / "yt-shots")
    shot_dir.mkdir(parents=True, exist_ok=True)
    res = {"ok": False, "url": "", "error": "", "shot": None, "private_fallback": False}
    with sync_playwright() as p:
        ctx = _context(p, headless=True)
        page = ctx.new_page()
        try:
            page.goto(UPLOAD, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)
            if not _logged_in(page):
                raise RuntimeError("не выполнен вход в YouTube — нажми «Войти» в /youtube")
            page.locator("input[type=file]").first.set_input_files(str(path))
            _type_box(page, "#title-textarea #textbox", title[:100])
            try:
                _type_box(page, "#description-textarea #textbox", desc[:4900])
            except Exception as e:
                log.warning("описание не заполнилось: %s", e)
            _click_first(page, ["tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
                                "[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']"])
            if adult:
                found = _click_first(page, ["tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_SELF']",
                                            "[name='VIDEO_AGE_RESTRICTION_SELF']"], 2500)
                if not found:   # секция «Возрастные ограничения» свёрнута — разворачиваем
                    _click_first(page, ["text=/Возрастные ограничения/i", "text=/Age restriction/i",
                                        "#age-restriction-toggle", "ytcp-button#toggle-button"], 3000)
                    found = _click_first(page, ["tp-yt-paper-radio-button[name='VIDEO_AGE_RESTRICTION_SELF']",
                                                "[name='VIDEO_AGE_RESTRICTION_SELF']"], 4000)
                if not found:
                    visibility, res["private_fallback"] = "PRIVATE", True
            try:
                res["url"] = page.locator("a.ytcp-video-info, span.video-url-fadeable a").first.get_attribute(
                    "href", timeout=20000) or ""
            except Exception:
                pass
            for _ in range(3):
                if not _click_first(page, ["#next-button"], 15000):
                    raise RuntimeError("не нажимается «Далее» в окне загрузки")
                page.wait_for_timeout(1500)
            if not _click_first(page, [f"tp-yt-paper-radio-button[name='{visibility}']", f"[name='{visibility}']"],
                                10000):
                raise RuntimeError("не нашёл выбор видимости")
            # ждём, пока файл догрузится (иначе при закрытии браузера загрузка оборвётся)
            t0 = time.time()
            while time.time() - t0 < 1200:
                txt = ""
                try:
                    txt = page.locator("ytcp-video-upload-progress").first.inner_text(timeout=3000)
                except Exception:
                    pass
                if not re.search(r"\d+\s?%", txt):
                    break
                page.wait_for_timeout(3000)
            if not _click_first(page, ["#done-button"], 20000):
                raise RuntimeError("не нажимается «Сохранить/Опубликовать»")
            page.wait_for_timeout(5000)
            if not res["url"]:
                try:
                    res["url"] = page.locator("a[href*='youtu']").first.get_attribute("href", timeout=10000) or ""
                except Exception:
                    pass
            res["ok"] = True
        except Exception as e:
            res["error"] = str(e)[:300]
            shot = shot_dir / f"fail_{int(time.time())}.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
                res["shot"] = shot
            except Exception:
                pass
            log.warning("youtube upload failed: %s", e)
        finally:
            ctx.close()
    return res


def available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def cleanup_old(outbox: Path, days: int = 3):
    if not outbox.exists():
        return
    cutoff = time.time() - days * 86400
    for f in outbox.iterdir():
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True) if f.is_file() else shutil.rmtree(f, ignore_errors=True)
