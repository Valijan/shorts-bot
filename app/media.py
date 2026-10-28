"""Работа с видео: анализ, поиск склеек, громкость, трекинг лица, рендер 9:16."""
import json
import subprocess
from pathlib import Path

import numpy as np

W, H = 1080, 1920


def run(cmd, cwd=None):
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
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
        "-c:a", "aac", "-b:a", "192k", str(dst),
    ])


def face_track(clip: Path, cuts: list[float], src_w: int, src_h: int):
    """Возвращает (доля кадров с лицами, [(время_начала_плана, x_кропа), ...])."""
    import cv2

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
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
              f"scale={W}:{H},setsar=1,ass={ass_name}")
        args = ["-vf", vf]
    else:
        fc = (
            f"[0:v]split=2[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"gblur=sigma=12,eq=brightness=-0.12,scale={W}:{H}[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,ass={ass_name}[v]"
        )
        args = ["-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?"]
    run([
        "ffmpeg", "-y", "-v", "error", "-i", clip.name, *args, "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name,
    ], cwd=cwd)
