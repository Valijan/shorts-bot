"""Работа с видео: анализ, поиск склеек, громкость, трекинг лица, рендер 9:16."""
import json
import subprocess
from pathlib import Path

import numpy as np

from . import config as cfg

W, H = 1080, 1920



def ass_filter(name: str) -> str:
    """Фильтр субтитров ffmpeg; если задана своя папка шрифтов — libass берёт шрифты оттуда."""
    fd = cfg.FONTS_DIR
    if not fd:
        return f"ass={name}"
    # «C:» в пути Windows: двоеточие внутри фильтра надо экранировать, иначе ffmpeg режет путь
    path = Path(fd).as_posix().replace(":", "\\:")
    return f"ass={name}:fontsdir='{path}'"

_FILE_OPT = None  # поддерживает ли ffmpeg синтаксис «-/filter_complex файл» (7.0+)


def _file_opt_supported() -> bool:
    """FFmpeg 8+ удалил -filter_complex_script, а до 7.0 не было «-/filter_complex».
    Проверяем один раз реальным запуском, какой вариант понимает установленный ffmpeg."""
    global _FILE_OPT
    if _FILE_OPT is None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "g.txt").write_text("[0:v]null[v]")
            r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
                                "-i", "nullsrc=s=16x16:d=0.04", "-/filter_complex", "g.txt",
                                "-map", "[v]", "-f", "null", "-"],
                               cwd=d, capture_output=True, text=True)
        _FILE_OPT = r.returncode == 0
    return _FILE_OPT


def _fix_script_opts(cmd):
    if "-filter_complex_script" not in cmd or not _file_opt_supported():
        return cmd
    return ["-/filter_complex" if a == "-filter_complex_script" else a for a in cmd]
def run(cmd, cwd=None):
    cmd = _fix_script_opts(list(cmd))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {r.stderr[-1500:]}")
    return r.stdout


def probe(path: Path) -> dict:
    out = run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,width,height:stream_tags=rotate:format=duration",
        "-of", "json", str(path),
    ])
    data = json.loads(out)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("В файле нет видеодорожки")
    w, h = int(v["width"]), int(v["height"])
    if str(v.get("tags", {}).get("rotate", "0")) in ("90", "270", "-90"):
        w, h = h, w
    has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])
    return {"w": w, "h": h, "duration": float(data["format"]["duration"]), "audio": has_audio}


def loudness_per_sec(path: Path) -> np.ndarray:
    """Громкость (дБ) по секундам — стримингом, без загрузки всего аудио в память."""
    sr = 4000
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    vals = []
    while True:
        buf = proc.stdout.read(sr * 2)
        if not buf:
            break
        a = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
        vals.append(float(np.sqrt(np.mean(a * a))) + 1e-3)
    proc.wait()
    if not vals:
        return np.zeros(1)
    return 20 * np.log10(np.array(vals) / 32768)


def detect_cuts(path: Path) -> list[float]:
    """Моменты смены сцен (секунды)."""
    try:
        from scenedetect import SceneManager, open_video
        from scenedetect.detectors import ContentDetector

        video = open_video(str(path))
        sm = SceneManager()
        sm.auto_downscale = True
        sm.add_detector(ContentDetector(threshold=27))
        sm.detect_scenes(video, frame_skip=1)
        return [s[0].get_seconds() for s in sm.get_scene_list()][1:]
    except Exception:
        return []


def extract(src: Path, start: float, end: float, dst: Path):
    """Точная вырезка фрагмента (с перекодированием, чтобы не было рывков на старте)."""
    run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{end - start:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
        # ровно 30 кадров/с: у скачанных видео бывает «плавающая» частота, из-за неё после
        # вырезания пауз картинка уезжала от звука и субтитров всё сильнее к концу клипа
        "-vf", f"fps={FPS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
        "-c:a", "aac", "-b:a", "192k", str(dst),
    ])


def face_track(clip: Path, cuts: list[float], src_w: int, src_h: int):
    """Возвращает (доля кадров с лицами, [(время_начала_плана, x_кропа), ...])."""
    crop_w = int(src_h * 9 / 16) // 2 * 2
    center = max(0, src_w - crop_w) // 2
    try:
        import cv2

        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except Exception:  # нет детектора лиц — используем размытый фон
        return 0.0, [(0.0, center)]
    cap = cv2.VideoCapture(str(clip))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    step = max(1, int(round(fps / 3)))  # ~3 проверки в секунду
    samples, idx = [], 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                fh, fw = frame.shape[:2]
                scale = 480 / fw
                small = cv2.resize(frame, (480, int(fh * scale)))
                gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
                faces = cascade.detectMultiScale(gray, 1.1, 6, minSize=(28, 28))
                cx = None
                if len(faces):
                    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                    cx = (x + w / 2) / scale * (src_w / fw)
                samples.append((idx / fps, cx))
        idx += 1
    cap.release()

    crop_w = int(src_h * 9 / 16) // 2 * 2
    max_x = max(0, src_w - crop_w)
    center = max_x // 2
    if not samples:
        return 0.0, [(0.0, center)]
    face_ratio = sum(1 for _, c in samples if c is not None) / len(samples)

    # Один статичный кроп на каждый план — смотрится как ручной монтаж, без «дрожания»
    bounds = [0.0] + [c for c in cuts if 0.3 < c] + [1e9]
    shots, last_x = [], center
    for a, b in zip(bounds, bounds[1:]):
        xs = [c for t, c in samples if a <= t < b and c is not None]
        if xs:
            x = int(np.median(xs) - crop_w / 2)
            last_x = min(max(0, x), max_x) // 2 * 2
        shots.append((a, last_x))
        if b > samples[-1][0]:
            break
    return face_ratio, shots


def render(clip: Path, out: Path, ass_name: str, mode: str, src_w: int, src_h: int,
           shots: list | None = None):
    """Рендер вертикального ролика 1080x1920 с субтитрами. Работает в папке клипа."""
    cwd = clip.parent
    vertical = src_h >= src_w
    if mode == "crop" and not vertical and shots:
        crop_w = int(src_h * 9 / 16) // 2 * 2
        (cwd / "cmds.txt").write_text(
            "".join(f"{t:.3f} crop@fc x {x};\n" for t, x in shots[1:])
        )
        sendcmd = "sendcmd=f=cmds.txt," if len(shots) > 1 else ""
        vf = (f"{sendcmd}crop@fc=w={crop_w}:h={src_h // 2 * 2}:x={shots[0][1]}:y=0,"
              f"scale={W}:{H},setsar=1,{ass_filter(ass_name)}")
        args = ["-vf", vf]
    else:
        fc = (
            f"[0:v]split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"gblur=sigma=12,eq=brightness=-0.12,scale={W}:{H}[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{ass_filter(ass_name)}[v]"
        )
        args = ["-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?"]
    run([
        "ffmpeg", "-y", "-v", "error", "-i", clip.name, *args, "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name,
    ], cwd=cwd)


def motion_per_sec(path: Path) -> np.ndarray:
    """Интенсивность действия по секундам: разница соседних кадров + вспышки (скиллы, взрывы)."""
    fps, w, h = 4, 96, 54
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-an",
         "-vf", f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    size, prev, per_frame = w * h, None, []
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        f = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
        if prev is not None:
            per_frame.append(float(np.mean(np.abs(f - prev))) + 0.5 * abs(float(f.mean() - prev.mean())))
        prev = f
    proc.wait()
    if not per_frame:
        return np.zeros(1)
    n = len(per_frame) // fps + 1
    a = np.resize(np.array(per_frame + [0.0] * fps), n * fps)[: n * fps]
    return a.reshape(n, fps).max(axis=1)


FPS = 30


def snap_keep(keep: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Границы кусков — точно по кадрам (1/30 с): тогда картинка, звук и субтитры режутся одинаково."""
    out = []
    for a, b in keep:
        a, b = round(a * FPS) / FPS, round(b * FPS) / FPS
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        elif b > a:
            out.append((a, b))
    return out


def jumpcut(src: Path, keep: list[tuple[float, float]], dst: Path):
    """Оставляет только куски keep (секунды, уже выровненные snap_keep), паузы вырезает."""
    half = 0.5 / FPS   # кадр с t ровно на границе b не берём — иначе каждый стык добавлял лишний кадр
    expr = "+".join(f"between(t,{a - half:.4f},{b - half:.4f})" for a, b in keep)
    aexpr = "+".join(f"between(t,{a:.4f},{b:.4f})" for a, b in keep)
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vf", f"select='{expr}',setpts=N/{FPS}/TB",
        "-af", f"aselect='{aexpr}',asetpts=N/SR/TB",
        "-r", str(FPS), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-c:a", "aac", "-b:a", "192k",
        str(dst),
    ])


def teaser(src: Path, at: float, dst: Path, length: float = 1.5):
    """Ставит в начало самый яркий момент (at, с) + белую вспышку-переход."""
    a = max(0.0, at - length * 0.55)
    b = a + length
    g = (f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,"
         f"fade=t=out:st={length - 0.12:.3f}:d=0.12:color=white[tv];"
         f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[ta];"
         f"[0:v]fade=t=in:st=0:d=0.15:color=white[mv];"
         f"[tv][ta][mv][0:a]concat=n=2:v=1:a=1[v][a]")
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", g,
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst),
    ])


def progress_bar(src: Path, dst: Path, duration: float, color: str = "0xFFD400"):
    """Тонкая полоска внизу, заполняется к концу ролика."""
    d = max(duration, 0.1)
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-filter_complex", f"color=c={color}:s={W}x14:r=30[c];"
                           f"[0:v][c]overlay=x='-W+W*t/{d:.3f}':y=H-14:shortest=1[v]",
        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(dst),
    ])


def beep(src: Path, dst: Path, spans: list[tuple[float, float]]):
    """Глушит мат и ставит писк 1 кГц."""
    spans = [(max(0.0, a - 0.03), b + 0.03) for a, b in spans]
    en = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in spans)
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-filter_complex",
        f"[0:a]volume=0:enable='{en}'[m];"
        f"sine=f=1000:r=44100,volume=0.35,aformat=channel_layouts=stereo,"
        f"volume='if({en},1,0)':eval=frame[b];"
        f"[m][b]amix=inputs=2:duration=first:normalize=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", str(dst),
    ])


def loopify(src: Path, dst: Path, fade: float = 0.35):
    """Петля: последние доли секунды плавно переходят в первый кадр — повтор без шва."""
    dur = probe(src)["duration"]
    if dur < 4:
        raise RuntimeError("too short")
    off = dur - fade
    run([
        "ffmpeg", "-y", "-v", "error", "-i", str(src), "-i", str(src),
        "-filter_complex",
        f"[1:v]trim=0:{fade + 0.05:.2f},setpts=PTS-STARTPTS[h];"
        f"[0:v][h]xfade=transition=fade:duration={fade}:offset={off:.3f}[v];"
        f"[0:a]afade=t=out:st={off:.3f}:d={fade}[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst),
    ])
