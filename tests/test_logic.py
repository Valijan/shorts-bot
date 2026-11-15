"""Проверки логики без видеокарты и Telegram:  python3 tests/test_logic.py"""
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import ai, reframe  # noqa: E402


def kinds(text, content, words=None):
    seg = {"s": 10.0, "e": 12.0, "t": text}
    if words:
        seg["words"] = words
    return [(e["kind"], e["label"]) for e in ai.game_events([seg], content)]


def test_announcer_cyrillic_and_latin():
    for t in ("Savage!", "Сэвидж!", "Маньяк", "Трипл килл", "Triple kill", "дабл кил"):
        assert kinds(t, "mlbb")[0][0] == "kill", t


def test_streamer_talk_is_not_kill():
    assert kinds("маньяка так и не дали мне сегодня", "mlbb")[0][0] == "talk"
    assert kinds("маньяка не дали", "mlbb")[0][0] == "talk"  # коротко, но не дикторская форма


def test_announcer_inside_long_segment():
    # диктор попал в один сегмент с болтовнёй стримера, но по времени стоит отдельно
    words = [{"s": 10 + i * 0.3, "e": 10.25 + i * 0.3, "w": w}
             for i, w in enumerate("ну давай давай заходи сюда быстрее".split())]
    words += [{"s": 16.0, "e": 16.4, "w": "Triple"}, {"s": 16.4, "e": 16.8, "w": "kill!"}]
    ev = ai.game_events([{"s": 10, "e": 17, "t": "x", "words": words}], "mlbb")
    assert [(e["kind"], e["label"], e["t"]) for e in ev] == [("kill", "TRIPLE KILL", 16.0)]


def test_pubg_short_shouts_count():
    for t in ("Хедшот!", "Headshot", "Минус один"):
        assert kinds(t, "pubg")[0][0] == "kill", t
    assert kinds("я его почти убил но он успел убежать за дом", "pubg")[0][0] == "talk"


def test_series_is_whole_and_savage_without_bang():
    ev = [{"t": 100.0, "w": 4, "kind": "kill", "label": "DOUBLE KILL"},
          {"t": 108.0, "w": 8, "kind": "kill", "label": "MANIAC!"},
          {"t": 112.0, "w": 10, "kind": "kill", "label": "SAVAGE!"},
          {"t": 117.0, "w": 3, "kind": "kill", "label": "SHUT DOWN"}]  # кил после главного
    c = ai.kill_chains(ev)[0]
    assert c["start"] <= 97.5 and c["end"] == 119.5 and c["label"] == "SAVAGE!"
    # savage найден только по кадрам (подпись без «!»), ранние килы не распознаны — берём 18 с назад
    c = ai.kill_chains([{"t": 200.0, "w": 10, "kind": "kill", "label": "SAVAGE"}])[0]
    assert c["start"] == 182.0
    picks = ai.pick_game(600, np.zeros(600), ev, 1, 15, 30)
    assert picks[0]["start"] <= 97.5 and picks[0]["end"] >= 119.5


def test_long_series_keeps_main_kill():
    ev = [{"t": 100.0 + 10 * i, "w": 3, "kind": "kill", "label": "KILL"} for i in range(6)]
    ev.append({"t": 158.0, "w": 10, "kind": "kill", "label": "SAVAGE!"})
    p = ai.pick_game(600, np.zeros(600), ev, 1, 15, 30)[0]
    assert p["end"] - p["start"] <= 30 and p["start"] <= 158.0 <= p["end"] - 2


def test_camera_path_60s(tmp=Path("/tmp/shorts_test")):
    """Непрерывный план 59 с: раньше ffmpeg падал на слишком длинном выражении камеры."""
    tmp.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=59",
                    "-c:v", "libx264", "-preset", "ultrafast", str(tmp / "long.mp4")], check=True)
    (tmp / "subs.ass").write_text("[Script Info]\nScriptType: v4.00+\n\n[Events]\n")
    keys = [(i * 0.2, 400 + 300 * np.sin(i / 7)) for i in range(295)]
    reframe.render(tmp / "long.mp4", tmp / "out.mp4", "subs.ass",
                   [{"s": 0, "e": 59, "kind": "track", "keys": keys}], 1280, 720)
    assert (tmp / "out.mp4").stat().st_size > 10000


def test_gpu_error_filter():
    class Cancelled(Exception):
        pass
    assert not ai._gpu_error(Cancelled())
    assert not ai._gpu_error(subprocess.CalledProcessError(1, "ffmpeg"))
    assert ai._gpu_error(RuntimeError("Library libcudnn_ops.so.9 is not found"))
    assert ai._gpu_error(RuntimeError("CUDA failed with error out of memory"))


def test_gta_events():
    assert kinds("Потрачено!", "gta")[0] == ("kill", "WASTED")
    # в GTA нет диктора — слово стримера в длинной фразе тоже сигнал
    assert kinds("за нами копы уже пять звёзд давай быстрее гони", "gta")[0][0] == "kill"
    assert ("tip", "💡") in kinds("смотрите тут пасхалка", "gta")


def test_live_sends_only_strong_moments():
    from app import live
    ev = [{"t": 100.0, "w": 6, "kind": "kill", "label": "WASTED"},
          {"t": 150.0, "w": 2.4, "kind": "talk", "label": "x"}]
    clips = [{"start": 90, "end": 110, "score": 3}, {"start": 140, "end": 160, "score": 3},
             {"start": 2, "end": 18, "score": 9}, {"start": 40, "end": 70, "score": 8}]
    assert [c["start"] for c in live.worth_it(clips, ev, 20.0, False)] == [90]       # без ИИ — только с событием
    assert [c["start"] for c in live.worth_it(clips, ev, 20.0, True)] == [90, 40]    # с ИИ — ещё оценка 7+
    # клип целиком в хвосте прошлого куска не повторяется (start=2..18 при хвосте 20 с)


def test_live_chunks_and_window(tmp=Path("/tmp/shorts_live")):
    import shutil
    from app import live, media
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=35",
                    "-f", "lavfi", "-i", "sine=f=440:d=35", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "30", "-c:a", "aac", str(tmp / "stream.ts")], check=True)
    proc = live.start([(str(tmp / "stream.ts"), {})], tmp / "rec", chunk=10)
    proc.wait(60)
    chunks = live.ready_chunks(tmp / "rec", set(), finished=True)
    assert len(chunks) >= 3, chunks
    assert len(live.ready_chunks(tmp / "rec", set(), finished=False)) == len(chunks) - 1
    live.save_tail(chunks[0], tmp / "tail.ts", 4)
    win, overlap = live.make_window(chunks[1], tmp / "tail.ts", tmp / "w")
    a, b = media.probe(chunks[1]), media.probe(win)
    assert 2 <= overlap <= 6 and abs(b["duration"] - a["duration"] - overlap) < 1.0 and b["audio"]


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("OK  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e)[:300])
    sys.exit(1 if fails else 0)
