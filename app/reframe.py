"""Умное кадрирование 16:9 -> 9:16.

Для каждой сцены (между склейками) отдельно решаем:
  * track — следим камерой за лицом (живые лица YuNet + аниме-лица) или за движением;
  * fit   — кадр целиком на размытом фоне (двое по краям, общий план, нет объекта).
Камера двигается плавно: мёртвая зона + сглаживание + ограничение скорости.
"""
from pathlib import Path

import numpy as np

from .media import H, W, run

MODELS = Path(__file__).resolve().parent.parent / "models"
FPS_SAMPLE = 5          # анализ кадров в секунду
AW = 480                # ширина кадра для анализа

_yunet = None
_anime = None
_haar = None


def _detectors():
    global _yunet, _anime, _haar
    import cv2

    if _yunet is None:
        try:
            _yunet = cv2.FaceDetectorYN.create(str(MODELS / "face_yunet.onnx"), "", (320, 320),
                                               0.6, 0.3, 50)
        except Exception:
            _yunet = False
    if _anime is None:
        c = cv2.CascadeClassifier(str(MODELS / "anime_face.xml"))
        _anime = c if not c.empty() else False
    if _haar is None and _yunet is False:
        try:
            _haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        except Exception:
            _haar = False
    return _yunet, _anime, _haar


def _faces(frame, gray):
    """Список лиц (x_center, ширина, уверенность) в координатах кадра анализа."""
    import cv2

    yunet, anime, haar = _detectors()
    out = []
    h, w = frame.shape[:2]
    if yunet:
        yunet.setInputSize((w, h))
        _, res = yunet.detect(frame)
        if res is not None:
            for r in res:
                x, y, fw, fh, score = r[0], r[1], r[2], r[3], r[-1]
                out.append((x + fw / 2, fw, float(score)))
    elif haar:
        for x, y, fw, fh in haar.detectMultiScale(gray, 1.1, 6, minSize=(24, 24)):
            out.append((x + fw / 2, fw, 0.7))
    if anime and not out:
        for x, y, fw, fh in anime.detectMultiScale(gray, 1.1, 4, minSize=(28, 28)):
            out.append((x + fw / 2, fw, 0.65))
    return out


def _motion_center(prev, gray):
    """Центр движения по горизонтали и насколько оно сосредоточено (0..1)."""
    import cv2

    diff = cv2.absdiff(cv2.GaussianBlur(prev, (5, 5), 0), cv2.GaussianBlur(gray, (5, 5), 0))
    cols = (diff > 25).sum(axis=0).astype(np.float32)
    total = cols.sum()
    if total < gray.size * 0.004:  # почти нет движения
        return None, 0.0
    xs = np.arange(len(cols))
    cx = float((cols * xs).sum() / total)
    spread = float(np.sqrt((cols * (xs - cx) ** 2).sum() / total))
    return cx, max(0.0, 1.0 - spread / (len(cols) * 0.35))


def _analyze(clip: Path):
    import cv2

    cap = cv2.VideoCapture(str(clip))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    step = max(1, int(round(fps / FPS_SAMPLE)))
    samples, idx, prev, scale = [], 0, None, 1.0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                fh, fw = frame.shape[:2]
                scale = AW / fw
                small = cv2.resize(frame, (AW, int(fh * scale)))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                faces = _faces(small, cv2.equalizeHist(gray))
                mx, conc = _motion_center(prev, gray) if prev is not None else (None, 0.0)
                samples.append({"t": idx / fps, "faces": faces, "mx": mx, "mc": conc})
                prev = gray
        idx += 1
    cap.release()
    return samples, scale


def _smooth(ts, xs, crop_w, max_x):
    """Плавная «операторская» камера: мёртвая зона, сглаживание, лимит скорости."""
    xs = np.array(xs, dtype=np.float32)
    if len(xs) >= 5:  # медианный фильтр убирает случайные выбросы
        pad = np.pad(xs, 2, mode="edge")
        xs = np.array([np.median(pad[i:i + 5]) for i in range(len(xs))])
    dead = crop_w * 0.08
    max_step = crop_w * 0.9 / FPS_SAMPLE  # не быстрее ~0.9 ширины кадра в секунду
    cam = [float(xs[0])]
    for x in xs[1:]:
        c = cam[-1]
        d = x - c
        if abs(d) > dead:
            c += float(np.clip((d - np.sign(d) * dead) * 0.45, -max_step, max_step))
        cam.append(c)
    cam = np.clip(np.array(cam) - crop_w / 2, 0, max_x)
    if cam.max() - cam.min() < crop_w * 0.1:  # почти не двигается — ставим статично
        return [(0.0, float(np.median(cam)))]
    return list(zip(ts, cam.tolist()))


def plan(clip: Path, cuts: list[float], duration: float, src_w: int, src_h: int,
         mode: str = "auto") -> list[dict]:
    """Возвращает сцены: [{"s","e","kind": "track"|"fit", "keys": [(t, x), ...]}]."""
    bounds = [0.0] + sorted(c for c in cuts if 0.4 < c < duration - 0.4) + [duration]
    if mode == "blur" or src_h >= src_w:
        return [{"s": 0.0, "e": duration, "kind": "fit", "keys": []}]

    crop_w = int(src_h * 9 / 16) // 2 * 2
    max_x = src_w - crop_w
    samples, scale = _analyze(clip)
    k = 1 / scale  # из координат анализа в исходные
    crop_a = crop_w * scale  # ширина кропа в координатах анализа

    shots, last_x = [], None
    for a, b in zip(bounds, bounds[1:]):
        ss = [s for s in samples if a <= s["t"] < b]
        kind, xs, ts = "fit", [], []
        with_faces = [s for s in ss if s["faces"]]
        if ss and len(with_faces) / len(ss) >= 0.3:
            # лица есть: если двое далеко друг от друга — показываем целиком
            wide = 0
            for s in with_faces:
                big = [f for f in s["faces"] if f[1] >= max(f2[1] for f2 in s["faces"]) * 0.6]
                if len(big) >= 2 and max(f[0] for f in big) - min(f[0] for f in big) > crop_a * 0.85:
                    wide += 1
            if wide / len(with_faces) < 0.5 or mode == "crop":
                kind = "track"
                prev = None
                for s in ss:
                    if s["faces"]:
                        # держим того же человека, переключаемся только на заметно крупнее
                        best = max(s["faces"], key=lambda f: f[1] * f[2])
                        if prev is not None:
                            near = min(s["faces"], key=lambda f: abs(f[0] - prev))
                            if best[1] < near[1] * 1.5:
                                best = near
                        prev = best[0]
                    if prev is not None:
                        ts.append(s["t"] - a)
                        xs.append(prev * k)
        elif ss:
            moving = [s for s in ss if s["mx"] is not None and s["mc"] > 0.35]
            if len(moving) / len(ss) >= 0.5 or mode == "crop":
                kind = "track"
                prev = None
                for s in ss:
                    if s["mx"] is not None and s["mc"] > 0.35:
                        prev = s["mx"]
                    if prev is not None:
                        ts.append(s["t"] - a)
                        xs.append(prev * k)
        if kind == "fit" and not (ss and with_faces):
            kind = "track"  # нет лиц и явного движения — всё равно заполняем вертикаль
            # центр «интереса»: среднее движение по сцене, иначе прошлая позиция/центр
            mxs = [s["mx"] * k for s in ss if s["mx"] is not None]
            if mxs:
                xs, ts = [float(np.median(mxs))], [0.0]
        if mode == "crop":
            kind = "track"
        if kind == "track":
            if not xs:  # крупный план без данных — держим прошлую позицию или центр
                xs, ts = [last_x + crop_w / 2 if last_x is not None else src_w / 2], [0.0]
            keys = _smooth(ts, xs, crop_w, max_x)
            last_x = keys[-1][1]
        else:
            keys = []
        shots.append({"s": a, "e": b, "kind": kind, "keys": keys})

    # очень короткие сцены (< 0.6 c) наследуют решение соседа — без «мигания»
    for i, sh in enumerate(shots):
        if sh["e"] - sh["s"] < 0.6 and i > 0:
            sh["kind"], sh["keys"] = shots[i - 1]["kind"], shots[i - 1]["keys"][-1:]
            if sh["keys"]:
                sh["keys"] = [(0.0, sh["keys"][0][1])]
    return shots


def _x_expr(keys) -> str:
    """Кусочно-линейное выражение x(t) для ffmpeg crop."""
    keys = [(round(t, 2), int(x) // 2 * 2) for t, x in keys]
    if len(keys) == 1:
        return str(keys[0][1])
    # прореживаем: ключ каждые ~0.4 с
    thin = [keys[0]]
    for t, x in keys[1:]:
        if t - thin[-1][0] >= 0.4:
            thin.append((t, x))
    if thin[-1] != keys[-1]:
        thin.append(keys[-1])
    expr = str(thin[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(thin, thin[1:]))):
        expr = f"if(lt(t,{t1}),{x0}+({x1 - x0})*(t-{t0})/{max(t1 - t0, 0.01)},{expr})"
    return f"if(lt(t,{thin[0][0]}),{thin[0][1]},{expr})"


def render(clip: Path, out: Path, ass_name: str, shots: list[dict], src_w: int, src_h: int):
    cwd = clip.parent
    crop_w = int(src_h * 9 / 16) // 2 * 2
    ch = src_h // 2 * 2
    parts, labels = [], []
    for i, sh in enumerate(shots):
        trim = f"[0:v]trim=start={sh['s']:.3f}:end={sh['e']:.3f},setpts=PTS-STARTPTS"
        if sh["kind"] == "track":
            parts.append(f"{trim},crop=w={crop_w}:h={ch}:x='{_x_expr(sh['keys'])}':y=0,"
                         f"scale={W}:{H},setsar=1[v{i}]")
        else:
            parts.append(
                f"{trim},split=2[a{i}][b{i}];"
                f"[a{i}]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                f"gblur=sigma=12,eq=brightness=-0.12,scale={W}:{H}[bg{i}];"
                f"[b{i}]scale={W}:{H}:force_original_aspect_ratio=decrease[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1[v{i}]")
        labels.append(f"[v{i}]")
    graph = ";\n".join(parts)
    graph += f";\n{''.join(labels)}concat=n={len(labels)}:v=1:a=0,fps=30,ass={ass_name}[v]"
    (cwd / "graph.txt").write_text(graph)
    run([
        "ffmpeg", "-y", "-v", "error", "-i", clip.name, "-filter_complex_script", "graph.txt",
        "-map", "[v]", "-map", "0:a:0?", "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name,
    ], cwd=cwd)
