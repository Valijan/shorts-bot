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
CAM_H = 540                                 # высота блока веб-камеры по умолчанию
CAM_MIN, CAM_MAX = 420, 760                 # пределы, если подстраиваем под настоящее окно вебки
BAR_H = 100                                 # полоса с ником под камерой


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
    grays = []
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
            if not faces and len(out) % 2 == 0:
                faces = _corner_faces(yunet, frame, sc)
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
        if len(out) % 2 == 0:
            grays.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        prev = gray
    analyze.median = np.median(np.stack(grays[-120:]), axis=0).astype(np.uint8) if len(grays) >= 4 else None
    return out, small.shape[1], small.shape[0]


def _corner_faces(yunet, frame, sc):
    """Мелкая вебка в углу: на уменьшенном кадре лицо слишком маленькое для детектора —
    смотрим углы в полном разрешении (увеличенные). Координаты — в масштабе уменьшенного кадра."""
    import cv2

    fh, fw = frame.shape[:2]
    cw, ch = int(fw * 0.4), int(fh * 0.45)
    found = []
    for x0, y0 in ((0, 0), (fw - cw, 0), (0, fh - ch), (fw - cw, fh - ch)):
        crop = frame[y0:y0 + ch, x0:x0 + cw]
        k = AW / cw
        img = cv2.resize(crop, (AW, int(ch * k)))
        yunet.setInputSize((img.shape[1], img.shape[0]))
        _, res = yunet.detect(img)
        if res is None:
            continue
        for r in res:
            if r[-1] > 0.75:
                fx, fy = x0 + r[0] / k, y0 + r[1] / k
                found.append((float(fx * sc), float(fy * sc), float(r[2] / k * sc), float(r[3] / k * sc)))
    return found


def find_facecam(samples, aw, ah):
    """Веб-камера = маленькое лицо, стабильно стоящее в одном месте у края экрана."""
    pts = [f for s in samples for f in s["faces"] if f[2] < aw * 0.16]
    if len(samples) < 8 or len(pts) < len(samples) * 0.35:
        return None
    arr = np.array(pts)
    cx, cy = np.median(arr[:, 0] + arr[:, 2] / 2), np.median(arr[:, 1] + arr[:, 3] / 2)
    near = [p for p in pts if abs(p[0] + p[2] / 2 - cx) < aw * 0.05 and abs(p[1] + p[3] / 2 - cy) < ah * 0.07]
    if len(near) < len(samples) * 0.35:
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


def refine_facecam(med, face, aw, ah):
    """Ищет настоящие границы окна вебки: на «среднем» кадре клипа (медиана по времени) у окна
    камеры есть стабильная граница с игрой — длинная прямая линия. Возвращает доли кадра или None."""
    import cv2

    if med is None:
        return None
    fx, fy, fw = face   # центр лица и ширина лица (в пикселях уменьшенного кадра)
    edges = cv2.dilate(cv2.Canny(med, 30, 90), np.ones((3, 3), np.uint8))
    e = edges > 0

    def best(lo, hi, axis, span):
        lo, hi = int(max(0, lo)), int(min((aw if axis == "x" else ah) - 1, hi))
        a, b = int(max(0, span[0])), int(min((ah if axis == "x" else aw), span[1]))
        if hi - lo < 2 or b - a < 8:
            return None, 0.0
        if axis == "x":
            sc = e[a:b, lo:hi].mean(axis=0)
        else:
            sc = e[lo:hi, a:b].mean(axis=1)
        i = int(np.argmax(sc))
        return lo + i, float(sc[i])

    vspan = (fy - fw * 1.6, fy + fw * 2.2)
    left, sl = best(fx - fw * 4.5, fx - fw * 0.9, "x", vspan)
    right, sr = best(fx + fw * 0.9, fx + fw * 4.5, "x", vspan)
    hspan = (fx - fw * 1.5, fx + fw * 1.5)
    top, st_ = best(fy - fw * 3.0, fy - fw * 0.9, "y", hspan)
    bot, sb = best(fy + fw * 0.9, fy + fw * 3.5, "y", hspan)
    TH = 0.55   # линия должна идти почти по всей высоте/ширине окна
    near = fw * 1.2
    L = left if sl >= TH else (0 if fx - fw * 4.5 <= near else None)
    R = right if sr >= TH else (aw if aw - (fx + fw * 4.5) <= near else None)
    T = top if st_ >= TH else (0 if fy - fw * 3.0 <= near else None)
    B = bot if sb >= TH else (ah if ah - (fy + fw * 3.5) <= near else None)
    if None in (L, R, T, B):
        return None
    w, h = R - L, B - T
    if not (fw * 2.0 <= w <= aw * 0.6 and fw * 1.6 <= h <= ah * 0.7 and 0.8 <= w / h <= 3.0):
        return None
    pad = 2   # внутрь от рамки, чтобы не тащить её край
    L, T, R, B = L + pad, T + pad, R - pad, B - pad
    return L / aw, T / ah, (R - L) / aw, (B - T) / ah


def cam_height(cam, src_w, src_h) -> int:
    """Высота блока вебки в вертикальном видео: по пропорциям окна, в разумных пределах."""
    _, _, w, h = cam
    ch = W * (h * src_h) / max(1.0, w * src_w)
    return int(np.clip(round(ch / 2) * 2, CAM_MIN, CAM_MAX))


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
         use_ai: bool = True, events=(), bar: bool = False) -> dict:
    """events — моменты килов внутри клипа (с): серия килов = тимфайт = широкий план."""
    samples, aw, ah = analyze(clip)
    cam = find_facecam(samples, aw, ah)
    refined = False
    if cam:
        pts = [f for s_ in samples for f in s_["faces"] if f[2] < aw * 0.16]
        arr = np.array(pts)
        face = (float(np.median(arr[:, 0] + arr[:, 2] / 2)), float(np.median(arr[:, 1] + arr[:, 3] / 2)),
                float(np.median(arr[:, 2])))
        try:
            r = refine_facecam(getattr(analyze, "median", None), face, aw, ah)
        except Exception as e:
            log.warning("refine_facecam: %s", e)
            r = None
        if r:
            cam, refined = r, True
    cam_h = cam_height(cam, src_w, src_h) if (cam and refined) else CAM_H
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
    bar_h = BAR_H if (cam and bar) else 0
    if cam:
        out_h = H - cam_h - bar_h
        crop_h = src_h // 2 * 2
        crop_w = min(src_w, int(crop_h * W / out_h)) // 2 * 2
    else:
        out_h = H
        crop_h = src_h // 2 * 2
        crop_w = int(crop_h * 9 / 16) // 2 * 2
    max_x = src_w - crop_w
    min_x = 0
    if cam:   # окно вебки сбоку — сдвигаем игровое окно так, чтобы оно её не цепляло (если хватает места)
        cx0, cx1 = cam[0] * src_w, (cam[0] + cam[2]) * src_w
        if cx1 <= src_w * 0.5 and src_w - cx1 >= crop_w:
            min_x = int(cx1) + 4
        elif cx0 >= src_w * 0.5 and cx0 >= crop_w + 8:
            max_x = int(cx0) - 4 - crop_w

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
        if min_x:
            keys = [(t, max(min_x, x)) for t, x in keys]
        shots.append({"s": a, "e": b, "kind": "track", "keys": keys})
    react = []
    if cam:  # всплески громкости = реакция стримера → зум камеры
        from .media import loudness_per_sec
        try:
            ld = loudness_per_sec(clip)
            z = (ld - np.median(ld)) / (np.std(ld) + 1e-6)
            for t in np.argsort(z)[::-1]:
                if z[t] < 1.6 or len(react) >= 4:
                    break
                if all(abs(t - r) > 3 for r in react):
                    react.append(float(t) + 0.4)
        except Exception:
            pass
    layout = {"layout": "stream" if cam else "follow", "shots": shots, "crop_w": crop_w, "react": sorted(react),
              "crop_h": crop_h, "out_h": out_h, "ai": bool(ai), "bar_h": bar_h,
              "cam_h": cam_h if cam else 0, "cam_refined": refined}
    if cam:
        x, y, w, h = cam
        layout["cam"] = (int(x * src_w) // 2 * 2, int(y * src_h) // 2 * 2,
                         int(w * src_w) // 2 * 2, int(h * src_h) // 2 * 2)
    return layout


def render(clip: Path, out: Path, ass_name: str, lay: dict, src_w: int, src_h: int):
    from .reframe import _x_expr

    cwd = clip.parent
    cw, ch, oh = lay["crop_w"], lay["crop_h"], lay["out_h"]
    cam_h = lay.get("cam_h") or CAM_H
    parts, labels = [], []
    n = len(lay["shots"])
    src = "[0:v]"
    if lay["layout"] == "stream":
        # окно вебки вырезано наверх отдельно — в игровой части закрашиваем его под цвет окружения,
        # чтобы снизу не торчали обрывки камеры и рамки оверлея
        x, y, w, h = lay["cam"]
        m = int(min(src_w, src_h) * 0.015)
        dx, dy = max(1, x - m), max(1, y - m)
        dw, dh = min(w + 2 * m, src_w - dx - 1), min(h + 2 * m, src_h - dy - 1)
        outs = "".join(f"[s{i}]" for i in range(n))
        parts.append(f"[0:v]delogo=x={dx}:y={dy}:w={dw}:h={dh}" + (f",split={n}{outs}" if n > 1 else "[s0]"))
    for i, sh in enumerate(lay["shots"]):
        src = f"[s{i}]" if lay["layout"] == "stream" else "[0:v]"
        trim = f"{src}trim=start={sh['s']:.3f}:end={sh['e']:.3f},setpts=PTS-STARTPTS"
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
        bh = lay.get("bar_h", 0)
        zoom = ""
        if lay.get("react"):
            zb = "+".join(f"exp(-pow((t-{t:.2f})/0.35,2))" for t in lay["react"])
            zoom = (f"scale=w='trunc({W}*(1+0.18*({zb}))/2)*2':h='trunc({cam_h}*(1+0.18*({zb}))/2)*2':"
                    f"eval=frame,crop={W}:{cam_h},")
        if bh:
            graph += (f";\n[0:v]crop={w}:{h}:{x}:{y},scale={W}:{cam_h},setsar=1,{zoom}fps=30,"
                      f"pad={W}:{cam_h + bh}:0:0:black[cam];"
                      f"[cam][game]vstack=inputs=2,ass={ass_name}[v]")
        else:
            graph += (f";\n[0:v]crop={w}:{h}:{x}:{y},scale={W}:{cam_h},setsar=1,{zoom}fps=30[cam];"
                      f"[cam][game]vstack=inputs=2,"
                      f"drawbox=y={cam_h - 3}:w=iw:h=6:color=white@0.9:t=fill,ass={ass_name}[v]")
    else:
        graph += f";\n[game]ass={ass_name}[v]"
    (cwd / "graph.txt").write_text(graph)
    run([
        "ffmpeg", "-y", "-v", "error", "-i", clip.name, "-filter_complex_script", "graph.txt",
        "-map", "[v]", "-map", "0:a:0?", "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name,
    ], cwd=cwd)
