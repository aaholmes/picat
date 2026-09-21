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


def test_show_sends_three_previews_then_strips(monkeypatch):
    import io
    import re

    from PIL import Image

    import picat

    monkeypatch.setattr(picat, "terminal_geometry", lambda: (100, 40, 10, 20))
    monkeypatch.delenv("TMUX", raising=False)
    out = io.StringIO()
    picat.show(Image.new("RGB", (2000, 1000)), out=out)
    s = out.getvalue()
    grids = re.findall(r"_Ga=T,[^;]*?c=(\d+),r=(\d+)", s)
    assert len({c for c, _ in grids}) == 1  # all stretched to one width in cells
    assert len(grids) > 4  # three previews, then several strips
    nrows = int(grids[0][1])
    assert sum(int(r) for _, r in grids[3:]) == nrows  # strips tile the image's rows
    assert s.count("_Ga=d,d=I") == 3  # all previews freed
    assert s.rstrip().endswith("\x1b[2K") or "\x1b[2K" in s[-40:]  # progress bar cleared


def test_every_image_matches_its_cell_box_so_strips_line_up(monkeypatch):
    # kitty fits each image into its c x r box keeping the aspect ratio, centring it; any
    # mismatch shifts that strip sideways and makes the image's edges jagged.
    import io
    import re

    from PIL import Image

    import picat

    cw, ch = 9.4, 18.1
    monkeypatch.setattr(picat, "terminal_geometry", lambda: (213, 57, cw, ch))
    monkeypatch.delenv("TMUX", raising=False)
    out = io.StringIO()
    picat.show(Image.new("RGB", (3072, 2048), "red"), out=out)
    s = out.getvalue()
    starts = list(re.finditer(r"\x1b_Ga=T,[^;]*?c=(\d+),r=(\d+)", s))
    assert len(starts) > 3
    widths = []  # on screen, in pixels
    for m in starts:
        end = s.index("\x1b\\", s.index("m=0;", m.start()))
        payload = "".join(re.findall(r";([A-Za-z0-9+/=]*)(?:\x1b\\|$)", s[m.start():end + 2]))
        w, h = Image.open(io.BytesIO(base64.b64decode(payload))).size
        c, r = int(m.group(1)), int(m.group(2))
        widths.append(min(c * cw, w * r * ch / h))
    previews, strip_widths = widths[:3], widths[3:]
    assert max(strip_widths) - min(strip_widths) < 1
    assert all(abs(p - strip_widths[0]) < 0.01 * strip_widths[0] for p in previews)


def test_placeholder_row_names_that_row_in_every_cell():
    from picat import placeholder_row

    cell = chr(0x10EEEE)
    s = placeholder_row(image_id=1, row=3, cols=4, indent=2)
    body = s.split("m", 1)[1].rsplit("\x1b", 1)[0]
    assert body == "  " + "".join(cell + chr(DIACRITICS[3]) + chr(DIACRITICS[c]) for c in range(4))


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


def test_main_reads_progress_flag(monkeypatch):
    import picat

    seen = {}
    monkeypatch.setattr(picat, "show", lambda img, progress=False: seen.update(progress=progress))
    monkeypatch.setattr(picat.Image, "open", lambda src: __import__("PIL.Image").Image.new("RGB", (4, 4)))
    for argv, want in ((["picat", "x.png"], False), (["picat", "--progress", "x.png"], True)):
        monkeypatch.setattr("sys.argv", argv)
        picat.main()
        assert seen["progress"] is want
