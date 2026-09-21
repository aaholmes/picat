import base64

import pytest

from picat import cells, fit, graphics_commands, placeholders, preview_size, wrap_tmux
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


def test_preview_is_a_quarter_width_but_not_tiny():
    assert preview_size((1600, 1000)) == (400, 250)
    assert preview_size((400, 200)) == (256, 128)
    assert preview_size((200, 100)) is None  # already small: no preview needed


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


def test_show_swaps_to_final_image_by_reprinting_placeholders(monkeypatch):
    import io
    import re

    from PIL import Image

    import picat

    monkeypatch.setattr(picat, "terminal_geometry", lambda: (100, 40, 10, 20))
    monkeypatch.delenv("TMUX", raising=False)
    out = io.StringIO()
    picat.show(Image.new("RGB", (2000, 1000)), out=out)
    s = out.getvalue()

    ids = [int(m) for m in re.findall(r"_Ga=T,[^;]*?i=(\d+)", s)]
    assert len(ids) == 2 and ids[0] != ids[1]  # preview and final under different ids
    preview_id, final_id = ids
    colour = lambda i: f"\x1b[38;2;{(i >> 16) & 255};{(i >> 8) & 255};{i & 255}m"
    # Order: preview data, placeholders for it, final data, cursor back up, final placeholders,
    # then the preview image deleted.
    p_preview = s.index(f"i={preview_id},")
    p_ph1 = s.index(colour(preview_id))
    p_final = s.index(f"i={final_id},")
    p_ph2 = s.index(colour(final_id))
    p_delete = s.index(f"_Ga=d,d=I,i={preview_id}")
    assert p_preview < p_ph1 < p_final < p_ph2 < p_delete
    up = re.search(r"\x1b\[(\d+)A\r", s[p_final:p_ph2])
    assert up is not None
    assert int(up.group(1)) == s[p_ph1:p_final].count("\n")  # back to the first placeholder row


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
