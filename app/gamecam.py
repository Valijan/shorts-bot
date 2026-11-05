"""«Игровой оператор» для 16:9 -> 9:16.

Раскладки:
  stream — в кадре есть веб-камера стримера: лицо сверху, геймплей снизу (2/3 экрана);
  follow — камера во весь экран плавно ведёт действие (героя/перестрелку),
           а в широкие моменты (тимфайт) отъезжает: кадр крупнее на размытом фоне.
Куда смотреть, решает анализ движения, а с ключом Claude — ИИ-монтажник по кадрам.
"""
import base64
import logging
from pathlib import Path

import numpy as np

from . import config as cfg
from .media import H, W, run
from .reframe import _detectors, _smooth

log = logging.getLogger(__name__)
FPS = 4
AW = 480
CENTER_BIAS = {"mlbb": 0.3, "pubg": 0.5}   # герой / прицел обычно в центре экрана
CAM_H = 640                                 # высота блока веб-камеры в раскладке stream


def _frames(clip: Path):
    import cv2

    cap = cv2.VideoCapture(str(clip))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    step = max(1, int(round(fps / FPS)))
    idx = 0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                yield idx / fps, frame
        idx += 1
    cap.release()


def analyze(clip: Path):
    import cv2

    yunet, _, _ = _detectors()
    out, prev = [], None
    for t, frame in _frames(clip):
        fh, fw = frame.shape[:2]
        sc = AW / fw
        small = cv2.resize(frame, (AW, int(fh * sc)))
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        faces = []
        if yunet:
            yunet.setInputSize((small.shape[1], small.shape[0]))
            _, res = yunet.detect(small)
            if res is not None:
                faces = [(float(r[0]), float(r[1]), float(r[2]), float(r[3])) for r in res if r[-1] > 0.7]
        mx = spread = None
        energy = 0.0
        if prev is not None:
            diff = cv2.absdiff(prev, gray) > 22
            cols = diff.sum(axis=0).astype(np.float32)
            tot = cols.sum()
            energy = float(tot / diff.size)
            if tot > diff.size * 0.004:
                xs = np.arange(len(cols))
                mx = float((cols * xs).sum() / tot)
                spread = float(np.sqrt((cols * (xs - mx) ** 2).sum() / tot))
        out.append({"t": t, "faces": faces, "mx": mx, "spread": spread, "e": energy})
        prev = gray
    return out, small.shape[1], small.shape[0]


def find_facecam(samples, aw, ah):
    """Веб-камера = маленькое лицо, стабильно стоящее в одном месте у края экрана."""
    pts = [f for s in samples for f in s["faces"] if f[2] < aw * 0.16]
    if len(samples) < 8 or len(pts) < len(samples) * 0.5:
        return None
    arr = np.array(pts)
    cx, cy = np.median(arr[:, 0] + arr[:, 2] / 2), np.median(arr[:, 1] + arr[:, 3] / 2)
    near = [p for p in pts if abs(p[0] + p[2] / 2 - cx) < aw * 0.05 and abs(p[1] + p[3] / 2 - cy) < ah * 0.07]
    if len(near) < len(samples) * 0.5:
        return None
    edge = cx < aw * 0.33 or cx > aw * 0.67 or cy < ah * 0.3 or cy > ah * 0.7
    if not edge:
        return None
    fw = float(np.median([p[2] for p in near]))
    bw = fw * 3.4
    bh = bw * CAM_H / W
    x = float(np.clip(cx - bw / 2, 0, aw - bw))
    y = float(np.clip(cy - bh * 0.42, 0, ah - bh))
    return x / aw, y / ah, bw / aw, bh / ah  # доли кадра


def _director(clip: Path, duration: float, content: str):
    """ИИ-монтажник: Claude смотрит кадры и говорит, где действие и широкий ли план."""
    if not cfg.ANTHROPIC_API_KEY or duration < 3:
        return None
    import cv2
    import anthropic

    step = max(1.0, duration / 30)
    want = [i * step for i in range(int(duration / step) + 1)]
    imgs, ts, j = [], [], 0
    for t, frame in _frames(clip):
        if j < len(want) and t >= want[j]:
            small = cv2.resize(frame, (384, int(frame.shape[0] * 384 / frame.shape[1])))
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                imgs.append(base64.b64encode(buf.tobytes()).decode())
                ts.append(t)
            j += 1
    if not imgs:
        return None
    game = {"mlbb": "Mobile Legends", "pubg": "PUBG Mobile"}.get(content, "игра")
    content_blocks = []
    for i, b in enumerate(imgs):
        content_blocks.append({"type": "text", "text": f"Кадр {i} (t={ts[i]:.1f}s)"})
        content_blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}})
    content_blocks.append({"type": "text", "text": f"""Ты монтажёр вертикальных шортсов по {game}.
Из горизонтального кадра вырезается вертикальное окно 9:16 (примерно треть ширины).
Для каждого кадра скажи, где по горизонтали центр главного действия (свой герой, драка, килл),
как доля ширины 0..1, и wide=true, если действие разбросано по всему экрану (тимфайт) и окно его обрежет.
Ответь ТОЛЬКО JSON: {{"frames":[{{"i":0,"x":0.5,"wide":false}}]}}"""})
    try:
        client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        msg = client.messages.create(model=cfg.CLAUDE_MODEL, max_tokens=2000,
                                     messages=[{"role": "user", "content": content_blocks}])
        from .ai import _json
        data = _json("".join(b.text for b in msg.content if getattr(b, "type", "") == "text")) or {}
        res = []
        for f in data.get("frames", []):
            i = int(f["i"])
            if 0 <= i < len(ts):
                res.append((ts[i], float(np.clip(f.get("x", 0.5), 0, 1)), bool(f.get("wide", False))))
        return res or None
    except Exception as e:
        log.warning("director failed: %s", e)
        return None


def plan(clip: Path, duration: float, src_w: int, src_h: int, content: str = "mlbb",
         use_ai: bool = True, events=()) -> dict:
    """events — моменты килов внутри клипа (с): серия килов = тимфайт = широкий план."""
    samples, aw, ah = analyze(clip)
    cam = find_facecam(samples, aw, ah)
    bias = CENTER_BIAS.get(content, 0.4)
    ai = _director(clip, duration, content) if use_ai else None

    # куда смотреть (доля ширины) и широкий ли план — по каждому сэмплу
    xs, wide, ts = [], [], []
    for s in samples:
        if s["mx"] is None or s["e"] < 0.002:
            x = xs[-1] if xs else 0.5          # движения нет — держим камеру
        elif (s["spread"] or 0) / aw > 0.24:
            x = 0.5                            # движется весь экран — это прокрутка камеры игры
        else:
            x = bias * 0.5 + (1 - bias) * s["mx"] / aw
        xs.append(x)
        ts.append(s["t"])
    ev = sorted(events)
    spans = []  # серии килов: 2+ кила с паузой < 5 с
    i = 0
    while i < len(ev):
        j = i
        while j + 1 < len(ev) and ev[j + 1] - ev[j] < 5:
            j += 1
        if j > i:
            spans.append((ev[i] - 1.0, ev[j] + 1.5))
        i = j + 1
    wide = [any(a <= t <= b for a, b in spans) for t in ts]
    if ai:  # ИИ поверх движения: интерполируем его решения на все сэмплы
        at = np.array([a[0] for a in ai])
        ax = np.array([a[1] for a in ai])
        aw_ = np.array([1.0 if a[2] else 0.0 for a in ai])
        tt = np.array(ts)
        xs = list(np.interp(tt, at, ax))
        wide = list(np.interp(tt, at, aw_) > 0.5)

    # раскладка
    if cam:
        out_h = H - CAM_H
        crop_h = src_h // 2 * 2
        crop_w = min(src_w, int(crop_h * W / out_h)) // 2 * 2
    else:
        out_h = H
        crop_h = src_h // 2 * 2
        crop_w = int(crop_h * 9 / 16) // 2 * 2
    max_x = src_w - crop_w

    # сцены «широкий план» (минимум 1.5 с, чтобы не мигало)
    segs, cur = [], None
    for t, w in zip(ts, wide):
        k = "wide" if w and not cam else "track"
        if cur and cur[2] == k:
            cur[1] = t
        else:
            if cur:
                segs.append(cur)
            cur = [t, t, k]
    if cur:
        segs.append(cur)
    merged = []
    for s in segs:
        if s[2] == "wide" and s[1] - s[0] < 1.5:
            s[2] = "track"
        if merged and merged[-1][2] == s[2]:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    bounds = [0.0] + [m[0] for m in merged[1:]] + [duration]
    shots = []
    for (a, b), m in zip(zip(bounds, bounds[1:]), merged):
        if m[2] == "wide":
            shots.append({"s": a, "e": b, "kind": "wide", "keys": []})
            continue
        sel = [(t - a, x * src_w) for t, x in zip(ts, xs) if a <= t < b] or [(0.0, src_w / 2)]
        keys = _smooth([p[0] for p in sel], [p[1] for p in sel], crop_w, max_x)
        shots.append({"s": a, "e": b, "kind": "track", "keys": keys})
    layout = {"layout": "stream" if cam else "follow", "shots": shots, "crop_w": crop_w,
              "crop_h": crop_h, "out_h": out_h, "ai": bool(ai)}
    if cam:
        x, y, w, h = cam
        layout["cam"] = (int(x * src_w) // 2 * 2, int(y * src_h) // 2 * 2,
                         int(w * src_w) // 2 * 2, int(h * src_h) // 2 * 2)
    return layout


def render(clip: Path, out: Path, ass_name: str, lay: dict, src_w: int, src_h: int):
    from .reframe import _x_expr

    cwd = clip.parent
    cw, ch, oh = lay["crop_w"], lay["crop_h"], lay["out_h"]
    parts, labels = [], []
    for i, sh in enumerate(lay["shots"]):
        trim = f"[0:v]trim=start={sh['s']:.3f}:end={sh['e']:.3f},setpts=PTS-STARTPTS"
        if sh["kind"] == "track":
            parts.append(f"{trim},crop=w={cw}:h={ch}:x='{_x_expr(sh['keys'])}':y=0,"
                         f"scale={W}:{oh},setsar=1[g{i}]")
        else:  # широкий план: весь кадр крупно (зум 1.4) на размытом фоне
            zw = int(W * 1.4) // 2 * 2
            zh = int(zw * src_h / src_w) // 2 * 2
            parts.append(
                f"{trim},split=2[a{i}][b{i}];"
                f"[a{i}]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                f"gblur=sigma=14,eq=brightness=-0.15,scale={W}:{oh}[bg{i}];"
                f"[b{i}]scale={zw}:{zh},crop={W}:{min(zh, oh)}[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1[g{i}]")
        labels.append(f"[g{i}]")
    graph = ";\n".join(parts) + f";\n{''.join(labels)}concat=n={len(labels)}:v=1:a=0,fps=30[game]"
    if lay["layout"] == "stream":
        x, y, w, h = lay["cam"]
        graph += (f";\n[0:v]crop={w}:{h}:{x}:{y},scale={W}:{CAM_H},setsar=1,fps=30[cam];"
                  f"[cam][game]vstack=inputs=2,"
                  f"drawbox=y={CAM_H - 3}:w=iw:h=6:color=white@0.9:t=fill,ass={ass_name}[v]")
    else:
        graph += f";\n[game]ass={ass_name}[v]"
    (cwd / "graph.txt").write_text(graph)
    run([
        "ffmpeg", "-y", "-v", "error", "-i", clip.name, "-filter_complex_script", "graph.txt",
        "-map", "[v]", "-map", "0:a:0?", "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name,
    ], cwd=cwd)
