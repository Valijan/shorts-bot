"""Эффекты монтажа: удар-зум, тряска, вспышка, звук удара, замедление; фоновая музыка."""
import random
from pathlib import Path

from . import config as cfg
from .media import H, W, run, venc

MUSIC_DIR = (cfg.DATA_DIR / "music").resolve()
MUSIC_DIR.mkdir(parents=True, exist_ok=True)


def _bumps(times, width):
    if not times:
        return "0"
    return "+".join(f"exp(-pow((t-{t:.2f})/{width},2))" for t in times)


def punch(src: Path, out: Path, times: list[float], slowmo_at: float | None = None,
          has_audio: bool = True):
    """times — моменты (с) событий внутри клипа. Видео 1080x1920 уже с субтитрами."""
    times = sorted(times)[:6]
    cwd = src.parent
    z = f"(1+0.20*({_bumps(times, 0.16)}))"
    sh = f"({_bumps(times, 0.11)})"
    fl = f"({_bumps(times, 0.07)})"
    v = (f"[0:v]scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame,"
         f"crop={W}:{H}:x='clip((iw-{W})/2+22*sin(t*95)*{sh},0,iw-{W})':"
         f"y='clip((ih-{H})/2+16*cos(t*80)*{sh},0,ih-{H})',"
         f"eq=brightness='0.22*{fl}':eval=frame,setsar=1[v]")
    graph = [v]
    amap = []
    if has_audio:
        booms = []
        for i, t in enumerate(times):
            booms.append(
                f"aevalsrc='0.9*sin(2*PI*t*(48+110*exp(-22*t)))*exp(-4.5*t)':d=0.9:s=44100,"
                f"aformat=channel_layouts=stereo,adelay={int(t * 1000)}|{int(t * 1000)}[b{i}]")
        graph += booms
        ins = "[0:a]" + "".join(f"[b{i}]" for i in range(len(times)))
        graph.append(f"{ins}amix=inputs={len(times) + 1}:duration=first:normalize=0,"
                     f"alimiter=limit=0.95[a]")
        amap = ["-map", "[a]"]
    if slowmo_at is not None:  # замедление — в том же проходе, без второго пережатия
        graph = [x.replace("[v]", "[pv]").replace("[a]", "[pa]") for x in graph]
        graph += _slowmo_graph("[pv]", "[pa]" if has_audio else None, slowmo_at)
    (cwd / "fx.txt").write_text(";\n".join(graph))
    run(["ffmpeg", "-y", "-v", "error", "-i", src.name, "-filter_complex_script", "fx.txt",
         "-map", "[v]", *amap, *venc(), "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
         out.name], cwd=cwd)


def _slowmo_graph(vin: str, ain: str | None, at: float, length=0.7, factor=2.0) -> list[str]:
    """Граф замедления куска [at-0.1, at+length] в factor раз. Выходы: [v] и [a]."""
    a, b = max(0.0, at - 0.1), at + length
    head = a >= 0.05  # кил в самом начале — куска «до» нет
    n = 3 if head else 2
    g = [f"{vin}split={n}" + ("[v0]" if head else "") + "[v1][v2]"]
    if head:
        g.append(f"[v0]trim=end={a},setpts=PTS-STARTPTS[p0]")
    g += [f"[v1]trim=start={a}:end={b},setpts={factor}*(PTS-STARTPTS),fps=30[p1]",
          f"[v2]trim=start={b},setpts=PTS-STARTPTS[p2]"]
    vs = ("[p0]" if head else "") + "[p1][p2]"
    if ain:
        g.append(f"{ain}asplit={n}" + ("[a0]" if head else "") + "[a1][a2]")
        if head:
            g.append(f"[a0]atrim=end={a},asetpts=PTS-STARTPTS[q0]")
        g += [f"[a1]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,atempo={1 / factor}[q1]",
              f"[a2]atrim=start={b},asetpts=PTS-STARTPTS[q2]"]
        pairs = ("[p0][q0]" if head else "") + "[p1][q1][p2][q2]"
        g.append(f"{pairs}concat=n={n}:v=1:a=1[v][a]")
    else:
        g.append(f"{vs}concat=n={n}:v=1:a=0[v]")
    return g


def slowmo(src: Path, out: Path, at: float, has_audio=True, length=0.7, factor=2.0):
    """Замедляет кусок [at-0.1, at+length] в factor раз."""
    cwd = src.parent
    g = _slowmo_graph("[0:v]", "[0:a]" if has_audio else None, at, length, factor)
    (cwd / "slow.txt").write_text(";\n".join(g))
    maps = ["-map", "[v]"] + (["-map", "[a]"] if has_audio else [])
    run(["ffmpeg", "-y", "-v", "error", "-i", src.name, "-filter_complex_script", "slow.txt",
         *maps, *venc(), "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name], cwd=cwd)


def music_files() -> list[Path]:
    return sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in
                  (".mp3", ".m4a", ".ogg", ".wav", ".aac", ".opus", ".flac"))


def add_music(src: Path, out: Path, has_audio=True, volume=0.22) -> bool:
    files = music_files()
    if not files:
        return False
    track = random.choice(files).resolve()
    cwd = src.parent
    if has_audio:  # музыка приглушается, когда говорит стример
        g = (f"[1:a]volume={volume},aformat=channel_layouts=stereo[m];"
             f"[0:a]asplit=2[k][o];"
             f"[m][k]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[d];"
             f"[o][d]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        g = f"[1:a]volume={volume * 2}[a]"
    (cwd / "mus.txt").write_text(g)
    run(["ffmpeg", "-y", "-v", "error", "-i", src.name, "-stream_loop", "-1", "-i", str(track),
         "-filter_complex_script", "mus.txt", "-map", "0:v", "-map", "[a]", "-shortest",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out.name],
        cwd=cwd)
    return True
