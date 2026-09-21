"""Show an image in kitty quickly over a slow connection: a small preview first, then the
full-resolution version drawn over it in place, sized to fit the terminal window.

Uses kitty's graphics protocol with Unicode placeholders, so it also works inside tmux (which needs
`set -g allow-passthrough on`).

Usage: picat IMAGE        (or `picat -` to read the image from standard input)
"""

import base64
import fcntl
import io
import math
import os
import random
import struct
import subprocess
import sys
import termios

from PIL import Image

from picat.diacritics import DIACRITICS

ESC = "\x1b"
CHUNK = 4096  # the protocol's maximum payload per escape sequence
PLACEHOLDER = chr(0x10EEEE)


def fit(size, box):
    """Largest size with `size`'s aspect ratio that fits in `box`, never larger than `size`."""
    w, h = size
    scale = min(1, box[0] / w, box[1] / h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def cells(size, cell_px):
    return math.ceil(size[0] / cell_px[0]), math.ceil(size[1] / cell_px[1])


def preview_size(size, min_width=256):
    """A quarter of the width (1/16 of the pixels), or None if the image is already small."""
    w, h = size
    pw = max(min_width, w // 4)
    if pw >= w:
        return None
    return pw, round(h * pw / w)


def graphics_commands(data, control):
    """Split `data` into kitty graphics protocol escape sequences carrying `control` keys."""
    b64 = base64.b64encode(data).decode()
    chunks = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)] or [""]
    cmds = []
    for n, chunk in enumerate(chunks):
        more = int(n < len(chunks) - 1)
        keys = f"{control},m={more}" if n == 0 else f"m={more}"
        cmds.append(f"{ESC}_G{keys};{chunk}{ESC}\\")
    return cmds


def wrap_tmux(seq):
    """tmux forwards a sequence to the outer terminal only inside its passthrough envelope."""
    return f"{ESC}Ptmux;{seq.replace(ESC, ESC + ESC)}{ESC}\\"


def placeholders(image_id, cols, rows, indent=0):
    """Text cells that kitty replaces with the image: each cell names its row and column with
    combining characters, and the foreground colour carries the image id."""
    if cols > len(DIACRITICS) or rows > len(DIACRITICS):
        raise ValueError(f"at most {len(DIACRITICS)} rows and columns can be addressed")
    r, g, b = (image_id >> 16) & 255, (image_id >> 8) & 255, image_id & 255
    lines = []
    for row in range(rows):
        line = " " * indent + "".join(PLACEHOLDER + chr(DIACRITICS[row]) + chr(DIACRITICS[col]) for col in range(cols))
        lines.append(line)
    return f"{ESC}[38;2;{r};{g};{b}m" + "\n".join(lines) + f"{ESC}[39m"


def terminal_geometry():
    """(columns, rows, cell width px, cell height px) of the terminal on standard output."""
    rows, cols, xpix, ypix = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    if not (xpix and ypix):
        # tmux may not report pixel sizes; ask kitty through icat, which knows how to query it.
        try:
            out = subprocess.run(["kitten", "icat", "--print-window-size"], capture_output=True, text=True, timeout=3).stdout
            xpix, ypix = map(int, out.strip().split("x"))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            xpix, ypix = cols * 10, rows * 20  # a typical cell size, as a last resort
    return cols, rows, xpix / cols, ypix / rows


def png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def show(img, out=None):
    out = out or sys.stdout
    cols, rows, cw, ch = terminal_geometry()
    target = fit(img.size, (cols * cw, max(1, rows - 2) * ch))  # leave room for the prompt
    ncols, nrows = cells(target, (cw, ch))
    ncols, nrows = min(ncols, len(DIACRITICS)), min(nrows, len(DIACRITICS))
    indent = (cols - ncols) // 2  # centred, as icat does
    wrap = wrap_tmux if os.environ.get("TMUX") else (lambda s: s)

    def send(im, image_id):
        # a=T transmit and display, U=1 virtual placement shown through placeholders, c/r size in
        # cells (so a preview is stretched to the final size), q=2 no replies from the terminal.
        control = f"a=T,U=1,f=100,i={image_id},c={ncols},r={nrows},q=2"
        for cmd in graphics_commands(png(im), control):
            out.write(wrap(cmd))
        out.flush()

    final = img if img.size == target else img.resize(target, Image.LANCZOS)
    final_id = random.randint(1, 2**24 - 1)
    small = preview_size(target)
    if not small:
        send(final, final_id)
        out.write(placeholders(final_id, ncols, nrows, indent) + "\n")
        out.flush()
        return

    preview_id = final_id % (2**24 - 1) + 1  # any other valid id
    send(img.resize(small, Image.LANCZOS), preview_id)
    out.write(placeholders(preview_id, ncols, nrows, indent) + "\n")
    out.flush()
    # The full image goes under a new id, so the preview stays on screen while it arrives (re-using
    # an id deletes the old image first). Rewriting the placeholder text with the new id then
    # switches over in one step, and makes kitty redraw those cells.
    send(final, final_id)
    out.write(f"{ESC}[{nrows}A\r" + placeholders(final_id, ncols, nrows, indent) + "\n")
    out.write(wrap(f"{ESC}_Ga=d,d=I,i={preview_id},q=2{ESC}\\"))  # free the preview's memory
    out.flush()


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__.strip())
    src = sys.stdin.buffer if sys.argv[1] == "-" else sys.argv[1]
    img = Image.open(src)
    img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    show(img)
