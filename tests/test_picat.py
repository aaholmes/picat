import base64

import pytest

from picat import cells, fit, graphics_commands, placeholders, wrap_tmux
from picat.diacritics import DIACRITICS

ESC = "\x1b"


def test_fit_shrinks_to_box_keeping_aspect_ratio():
    assert fit((3072, 2048), (1500, 1500)) == (1500, 1000)
    assert fit((1000, 2000), (800, 800)) == (400, 800)


def test_fit_never_upscales():
    assert fit((300, 200), (1500, 1500)) == (300, 200)


def test_cells_round_up():
    assert cells((1500, 1000), cell_px=(10, 20)) == (150, 50)
    assert cells((1501, 1001), cell_px=(10, 20)) == (151, 51)




def test_graphics_commands_chunk_payload_and_mark_continuation():
    data = bytes(range(256)) * 40  # 10240 bytes -> 13656 base64 chars -> 4 chunks of <=4096
    cmds = graphics_commands(data, "a=T,f=100,i=7")
    assert len(cmds) == 4
    assert cmds[0].startswith(f"{ESC}_Ga=T,f=100,i=7,m=1;")
    assert all(c.startswith(f"{ESC}_Gm=1;") for c in cmds[1:-1])
    assert cmds[-1].startswith(f"{ESC}_Gm=0;")
    assert all(c.endswith(f"{ESC}\\") for c in cmds)
    payload = "".join(c.split(";", 1)[1][:-2] for c in cmds)
    assert base64.b64decode(payload) == data


def test_graphics_commands_single_chunk():
    (cmd,) = graphics_commands(b"abc", "a=T,f=100")
    assert cmd == f"{ESC}_Ga=T,f=100,m=0;YWJj{ESC}\\"


def test_wrap_tmux_doubles_inner_escapes():
    assert wrap_tmux(f"{ESC}_Gx;y{ESC}\\") == f"{ESC}Ptmux;{ESC}{ESC}_Gx;y{ESC}{ESC}\\{ESC}\\"


def test_placeholders_encode_image_id_row_and_column():
    s = placeholders(image_id=0x0A0B0C, cols=2, rows=2)
    lines = s.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith(f"{ESC}[38;2;10;11;12m")
    cell = chr(0x10EEEE)
    assert f"{cell}{chr(DIACRITICS[0])}{chr(DIACRITICS[1])}" in lines[0]
    assert f"{cell}{chr(DIACRITICS[1])}{chr(DIACRITICS[0])}" in lines[1]
    assert lines[-1].endswith(f"{ESC}[39m")


def test_placeholders_reject_images_too_big_to_address():
    with pytest.raises(ValueError):
        placeholders(image_id=1, cols=len(DIACRITICS) + 1, rows=1)



def test_placeholders_can_be_indented():
    s = placeholders(image_id=1, cols=2, rows=2, indent=3)
    for line in s.split("\n"):
        text = line.split("m", 1)[1] if line.startswith("\x1b[") else line
        assert text.startswith("   " + chr(0x10EEEE))


def test_show_centres_image_horizontally(monkeypatch):
    import io

    from PIL import Image

    import picat

    monkeypatch.setattr(picat, "terminal_geometry", lambda: (100, 40, 10, 20))
    monkeypatch.delenv("TMUX", raising=False)
    out = io.StringIO()
    picat.show(Image.new("RGB", (400, 200)), out=out)  # 40x10 cells in a 100-column window
    first_row = out.getvalue().split(chr(0x10EEEE), 1)[0].rsplit("m", 1)[1]
    assert first_row == " " * 30


def test_preview_sizes_are_a_sixteenth_a_quarter_and_a_half_of_the_width():
    from picat import preview_sizes

    assert preview_sizes((2000, 1000)) == [(126, 63), (500, 250), (1000, 500)]  # 125 would round 62.5
    assert preview_sizes((400, 200)) == [(100, 50), (200, 100)]  # 1/16 would be under 32 px wide
    assert preview_sizes((60, 30)) == []


def test_strips_cover_all_rows_on_cell_boundaries():
    from picat import strips

    s = strips(height=1000, nrows=50, count=10)
    assert len(s) == 10
    assert s[0][:3] == (0, 5, 0) and s[-1][1] == 50 and s[-1][3] == 1000
    assert all(a[1] == b[0] and a[3] == b[2] for a, b in zip(s, s[1:]))
    assert all(y0 == 20 * r0 for r0, _, y0, _ in s)  # 20 px per cell row


def test_strips_never_more_than_cell_rows():
    from picat import strips

    assert len(strips(height=100, nrows=3, count=10)) == 3


def test_progress_bar_shows_fraction_and_time_left():
    from picat import progress_bar

    bar = progress_bar(0.5, 1.25, width=20)
    assert "50%" in bar and "1.2 s" in bar
    assert bar.count("█") == 5 and bar.count("░") == 5  # 10 cells left for the bar itself


@pytest.mark.parametrize("progress", [False, True])
def test_progress_bar_only_when_asked(monkeypatch, progress):
    import io

    from PIL import Image

    import picat

    monkeypatch.setattr(picat, "terminal_geometry", lambda: (100, 40, 10, 20))
    monkeypatch.delenv("TMUX", raising=False)
    out = io.StringIO()
    picat.show(Image.new("RGB", (2000, 1000)), out=out, progress=progress)
    assert ("░" in out.getvalue()) == progress


def run_show(monkeypatch, geometry=(213, 57, 9.4, 18.1), size=(3072, 2048), image=None, **kw):
    import io

    from PIL import Image

    import picat

    monkeypatch.setattr(picat, "terminal_geometry", lambda: geometry)
    monkeypatch.delenv("TMUX", raising=False)
    out = io.StringIO()
    picat.show(image or Image.new("RGB", size, "red"), out=out, **kw)
    return out.getvalue()


def transmissions(s):
    """(control keys, decoded image size) for each image sent."""
    import io
    import re

    from PIL import Image

    found = []
    for m in re.finditer(r"\x1b_G(a=T,[^;]*);", s):
        end = s.index("\x1b\\", s.index("m=0;", m.start()))
        payload = "".join(re.findall(r";([A-Za-z0-9+/=]*)(?:\x1b\\|$)", s[m.start():end + 2]))
        keys = dict(kv.split("=") for kv in m.group(1).split(",") if kv != "m=1" and kv != "m=0")
        found.append((keys, Image.open(io.BytesIO(base64.b64decode(payload))).size))
    return found


def test_placeholder_text_is_written_once_with_no_cursor_movement(monkeypatch):
    import re

    s = run_show(monkeypatch)
    assert s.count("\x1b[38;2;") == 1
    assert not re.search(r"\x1b\[\d+[AB]", s)


def test_later_stages_are_placed_relative_to_the_first_preview(monkeypatch):
    sent = transmissions(run_show(monkeypatch))
    (parent, _), rest = sent[0], sent[1:]
    assert parent["U"] == "1" and parent["p"] == "1"
    assert all(k["P"] == parent["i"] and k["Q"] == "1" and "U" not in k for k, _ in rest)
    previews = [k for k, _ in rest if "c" in k]
    strips = [k for k, _ in rest if "c" not in k]
    assert len(previews) == 2 and all(k["c"] == parent["c"] and k["r"] == parent["r"] for k in previews)
    assert [int(k["V"]) for k in strips][0] == 0 and len(strips) > 1
    assert sorted(int(k["V"]) for k in strips) == [int(k["V"]) for k in strips]


def test_replaced_previews_are_deleted_but_not_the_first(monkeypatch):
    import re

    s = run_show(monkeypatch)
    sent = transmissions(s)
    deleted = re.findall(r"_Ga=d,d=I,i=(\d+)", s)
    assert sorted(deleted) == sorted(k["i"] for k, _ in sent[1:3])


def test_previews_have_the_shape_of_the_box_and_strips_their_natural_size(monkeypatch):
    cw, ch = 9.4, 18  # kitty's cells are whole pixels; only a guessed size can be fractional
    sent = transmissions(run_show(monkeypatch, geometry=(213, 57, cw, ch)))
    for k, (w, h) in sent[:3]:
        box = int(k["c"]) * cw / (int(k["r"]) * ch)
        assert abs(w / h - box) < 0.01 * box
    strips = sent[3:]
    width = strips[0][1][0]
    assert all(size[0] == width for _, size in strips)
    ncols = int(sent[0][0]["c"])
    for k, (w, h) in strips:
        assert abs(int(k["X"]) - (ncols * cw - w) / 2) <= 1 and int(k["X"]) < cw
    assert sum(h for _, (w, h) in strips) == int(sent[0][0]["r"]) * round(ch)


def test_main_detaches_unless_showing_progress(monkeypatch):
    import picat

    seen = {}
    monkeypatch.setattr(picat, "show", lambda img, **kw: seen.update(kw))
    monkeypatch.setattr(picat.Image, "open", lambda src: __import__("PIL.Image").Image.new("RGB", (4, 4)))
    for argv, progress in ((["picat", "x.png"], False), (["picat", "--progress", "x.png"], True)):
        monkeypatch.setattr("sys.argv", argv)
        picat.main()
        assert seen == {"progress": progress, "detach": not progress}


def test_watcher_stops_once_a_command_takes_over_from_the_shell():
    from picat import CommandWatcher

    own, shell, cmd = 10, 20, 30
    seq = iter([own, own, shell, shell, cmd])  # picat, then the shell's prompt, then a command
    w = CommandWatcher(lambda: next(seq), own)
    assert [w.command_started() for _ in range(5)] == [False, False, False, False, True]


def test_watcher_never_stops_if_the_terminal_cannot_be_queried():
    from picat import CommandWatcher

    def fail():
        raise OSError

    assert not CommandWatcher(fail, 10).command_started()


def test_rate_from_two_replies_cancels_the_latency():
    from picat import estimate_rate

    # 0.05 s latency and 1 MB/s: 10 KB -> 0.06 s, 400 KB -> 0.45 s
    assert abs(estimate_rate((10_000, 0.06), (400_000, 0.45)) - 1e6) < 1
    assert estimate_rate((10_000, 0.06), (400_000, 0.05)) == 400_000 / 0.05  # noisy: be safe
    # a noisy difference can never claim more than 1.5 times the larger transfer's average
    assert estimate_rate((10_000, 0.40), (400_000, 0.45)) == 1.5 * 400_000 / 0.45


def test_link_timing_excludes_preparing_the_image(monkeypatch):
    import picat

    clock = [0.0]
    monkeypatch.setattr(picat.time, "monotonic", lambda: clock[0])
    real_preview_png = picat.small_png

    def slow_small_png(*args):
        clock[0] += 1.0  # preparing a preview takes a (simulated) second
        return real_preview_png(*args)

    monkeypatch.setattr(picat, "small_png", slow_small_png)
    measured = []
    monkeypatch.setattr(picat, "estimate_rate", lambda a, b: measured.extend([a, b]) or 1e6)
    monkeypatch.setattr(picat.os, "fork", lambda: 1)
    run_show(monkeypatch, detach=True, reply=lambda image_id: True)
    assert [t for _, t in measured] == [0.0, 0.0]


def test_wait_ok_finds_the_reply_among_other_input():
    import os

    from picat import wait_ok

    r, w = os.pipe()
    os.write(w, b"ab\x1b_Gi=7;OK\x1b\\cd")
    assert wait_ok(r, 7, timeout=1) is True
    os.write(w, b"\x1b_Gi=8;OK\x1b\\")
    assert wait_ok(r, 7, timeout=0.1) is False


def test_pacer_keeps_to_the_rate():
    from picat import Pacer

    now, slept = [0.0], []

    def sleep(t):
        slept.append(t)
        now[0] += t

    p = Pacer(1000, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        p.sent(500)
    assert abs(now[0] - 1.5) < 1e-9


def test_detached_show_measures_the_link_then_leaves_the_rest_to_the_background(monkeypatch):
    import os

    forks = []
    monkeypatch.setattr(os, "fork", lambda: forks.append(1) or 1)
    s = run_show(monkeypatch, detach=True, reply=lambda image_id: True)
    sent = transmissions(s)
    assert forks and len(sent) == 2  # the tiny and 1/4 previews, both with replies asked for
    assert all(k.get("q", "0") == "0" for k, _ in sent)


def test_detached_show_stays_in_the_foreground_without_replies(monkeypatch):
    import os

    monkeypatch.setattr(os, "fork", lambda: (_ for _ in ()).throw(AssertionError("forked")))
    s = run_show(monkeypatch, detach=True, reply=lambda image_id: False)
    assert len(transmissions(s)) > 5  # everything, in the foreground


def test_one_reply_is_enough_to_carry_on_in_the_background(monkeypatch):
    import os

    forks, asked = [], []
    monkeypatch.setattr(os, "fork", lambda: forks.append(1) or 1)

    def reply(image_id):
        asked.append(image_id)
        return len(asked) == 2  # no reply to the first preview, a reply to the second

    run_show(monkeypatch, detach=True, reply=reply)
    assert forks


def chunks_never_interleave(s):
    """kitty takes an image's chunks in sequence: after a chunk marked m=1, the next graphics
    command must be that image's next chunk (no a= key), until one marked m=0."""
    import re

    open_ = False
    for keys in re.findall(r"\x1b_G([^;\x1b]*)[;\x1b]", s):
        if keys.startswith("a=") == open_:  # a new command while one is open, or a stray chunk
            return False
        open_ = "m=1" in keys
    return not open_


def test_stopping_mid_image_closes_that_image_first(monkeypatch):
    calls = []

    def stop():
        calls.append(1)
        return len(calls) > 40  # partway through some image's chunks

    import os

    from PIL import Image

    noise = Image.frombytes("RGB", (1536, 1024), os.urandom(1536 * 1024 * 3))  # many chunks each
    s = run_show(monkeypatch, image=noise, stop=stop)
    full = run_show(monkeypatch, image=noise)
    assert chunks_never_interleave(full)
    assert chunks_never_interleave(s)
    assert len(s) < len(full) / 2
