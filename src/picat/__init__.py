"""Show an image in kitty quickly over a slow connection, sized to fit the terminal window: a tiny
preview (1/16 of the width) first, then larger ones (1/4 and 1/2), then the full-resolution image as
horizontal strips, each a separate image replacing the preview where it lands.

Uses kitty's graphics protocol with Unicode placeholders, so it also works inside tmux (which needs
`set -g allow-passthrough on`).

Usage: picat [--progress] IMAGE    (`-` reads the image from standard input)

  --progress, -p   show a bar below the image with the time left
"""

import base64
import fcntl
import io
import math
import os
import queue
import random
import struct
import subprocess
import sys
import termios
import threading
import time

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


def preview_sizes(size, min_width=32):
    """Previews at about 1/16, 1/4 and 1/2 of the width, skipping any narrower than `min_width`. Each
    width is nudged so that the rounded height keeps the aspect ratio as closely as possible."""
    w, h = size
    sizes = []
    for d in (16, 4, 2):
        if w // d >= min_width:
            pw = min(range(w // d, w // d + 8), key=lambda x: abs(h * x / w - round(h * x / w)))
            sizes.append((pw, round(h * pw / w)))
    return sizes


def strips(height, nrows, count):
    """Split the image into at most `count` horizontal strips of whole cell rows, as
    (first cell row, end cell row, first pixel row, end pixel row)."""
    count = min(count, nrows)
    cell = [round(j * nrows / count) for j in range(count + 1)]
    px = [round(k * height / nrows) for k in cell]
    return [(cell[j], cell[j + 1], px[j], px[j + 1]) for j in range(count)]


def progress_bar(fraction, seconds_left, width):
    text = f" {round(fraction * 100)}% {seconds_left:.1f} s"
    n = max(1, width - len(text))
    filled = round(fraction * n)
    return "█" * filled + "░" * (n - filled) + text


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
    lines = [" " * indent + cell_row(row, cols) for row in range(rows)]
    return colour(image_id) + "\n".join(lines) + f"{ESC}[39m"


def placeholder_row(image_id, row, cols, indent=0):
    """One row of placeholders, showing row `row` of the image."""
    return colour(image_id) + " " * indent + cell_row(row, cols) + f"{ESC}[39m"


def cell_row(row, cols):
    return "".join(PLACEHOLDER + chr(DIACRITICS[row]) + chr(DIACRITICS[col]) for col in range(cols))


def colour(image_id):
    r, g, b = (image_id >> 16) & 255, (image_id >> 8) & 255, image_id & 255
    return f"{ESC}[38;2;{r};{g};{b}m"


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


def pad(img, size, offset):
    """`img` placed at `offset` on a transparent canvas of `size`."""
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.paste(img, offset)
    return canvas


def small_png(img, size, offset):
    """`img` padded as `pad` does, as a PNG with a 256-colour palette: previews are temporary,
    so smaller beats exact."""
    if img.mode == "RGBA":
        return png(pad(img, size, offset).quantize(256, method=Image.Quantize.FASTOCTREE))
    q = img.quantize(255, method=Image.Quantize.MEDIANCUT)  # leaves index 255 for the padding
    canvas = Image.new("P", size, 255)
    palette = q.getpalette()
    canvas.putpalette(palette + [0] * (768 - len(palette)))
    canvas.paste(q, offset)
    canvas.info["transparency"] = 255
    return png(canvas)


def show(img, out=None, strip_count=24, progress=False):
    out = out or sys.stdout
    cols, rows, cw, ch = terminal_geometry()
    target = fit(img.size, (cols * cw, max(1, rows - 3) * ch))  # room for the bar and the prompt
    ncols, nrows = cells(target, (cw, ch))
    ncols, nrows = min(ncols, len(DIACRITICS)), min(nrows, len(DIACRITICS))
    indent = (cols - ncols) // 2  # centred, as icat does
    wrap = wrap_tmux if os.environ.get("TMUX") else (lambda s: s)
    # kitty fits each image into its box keeping the aspect ratio and centres it, so every strip
    # must have the same scale, or it is shifted sideways.
    # Heights must be exact: a strip is ~50 pixels tall and ~30 times as wide, so half a pixel of
    # rounding in its height would shift its sides by ~15. So each cell row is a whole number of
    # image pixels tall, and the image is scaled by the (tiny) factor this needs.
    k = max(1, round(ch)) / ch
    target = max(1, round(target[0] * k)), max(1, round(target[1] * k))
    # Only the height is padded: strips that all share one scale are centred with equal margins.
    canvas = target[0], nrows * max(1, round(ch))
    offset = 0, (canvas[1] - target[1]) // 2
    bands = strips(canvas[1], nrows, strip_count)

    # Encode the full-resolution strips in the background while the previews go out.
    encoded = queue.Queue()

    def encode():
        final = img if img.size == target else img.resize(target, Image.LANCZOS)
        oy = offset[1]
        for _, _, y0, y1 in bands:
            if oy <= y0 and y1 <= oy + target[1]:  # no padding in this strip, so no alpha needed
                encoded.put(png(final.crop((0, y0 - oy, target[0], y1 - oy))))
            else:
                encoded.put(png(pad(final.crop((0, max(0, y0 - oy), target[0], min(target[1], y1 - oy))),
                                    (target[0], y1 - y0), (0, max(0, oy - y0)))))

    threading.Thread(target=encode, daemon=True).start()

    sent, start = 0, time.monotonic()

    def send(data, control):
        nonlocal sent
        for cmd in graphics_commands(data, control):
            cmd = wrap(cmd)
            out.write(cmd)
            sent += len(cmd)
        out.flush()

    def bar(total):
        if not progress:
            return
        rate = sent / max(time.monotonic() - start, 1e-3)
        out.write("\r" + " " * indent + progress_bar(min(1, sent / total), max(0, total - sent) / rate, max(20, ncols)) + f"{ESC}[K")
        out.flush()

    def draw_row(image_id, row, image_row=None):  # the cursor waits on the bar line, below the image
        up = nrows - row
        image_row = row if image_row is None else image_row
        out.write(f"{ESC}[{up}A\r" + placeholder_row(image_id, image_row, ncols, indent) + f"{ESC}[{up}B\r")

    b64 = lambda n: 4 * math.ceil(n / 3)
    first = random.randint(1, 2**24 - 5 - strip_count)  # previews and strips take the ids after it
    grid = f"U=1,f=100,c={ncols},r={nrows},q=2"
    # Previews are also made in the background, so each is prepared while the one before is sent.
    sizes = preview_sizes(canvas)
    previews = queue.Queue()

    def make_previews():
        for size in sizes:
            k = size[0] / canvas[0]
            inner = max(1, round(target[0] * k)), max(1, round(target[1] * k))
            small = img.resize(inner, Image.LANCZOS, reducing_gap=3.0)
            previews.put((k, small_png(small, size, (round(offset[0] * k), round(offset[1] * k)))))

    threading.Thread(target=make_previews, daemon=True).start()
    preview_ids, estimate = [], None  # estimate: bytes of the full image, from the latest preview
    for n in range(1, len(sizes) + 1):
        k, data = previews.get()
        send(data, f"a=T,i={first + n},{grid}")
        preview_ids.append(first + n)
        estimate = b64(len(data)) / k**2
        if n == 1:
            out.write(placeholders(first + n, ncols, nrows, indent) + "\n")
        else:
            for row in range(nrows):
                draw_row(first + n, row)
        bar(sent + estimate)

    # Each full-resolution strip is its own image, covering whole cell rows, so it can replace the
    # preview row by row as it arrives.
    if not preview_ids:
        out.write("\n" * nrows)
    strip_bytes = []
    for j, (r0, r1, y0, y1) in enumerate(bands):
        data = encoded.get()
        strip_bytes.append(b64(len(data)))
        send(data, f"a=T,i={first + 4 + j},U=1,f=100,c={ncols},r={r1 - r0},q=2")
        for row in range(r0, r1):
            draw_row(first + 4 + j, row, row - r0)
        left = len(bands) - j - 1
        bar(sent + left * sum(strip_bytes) / len(strip_bytes))
    for i in preview_ids:
        out.write(wrap(f"{ESC}_Ga=d,d=I,i={i},q=2{ESC}\\"))  # free the previews' memory
    out.write(f"\r{ESC}[2K")
    out.flush()


def main():
    args = sys.argv[1:]
    progress = bool({"--progress", "-p"} & set(args))
    args = [a for a in args if a not in ("--progress", "-p")]
    if len(args) != 1 or args[0] in ("-h", "--help"):
        sys.exit(__doc__.strip())
    src = sys.stdin.buffer if args[0] == "-" else args[0]
    img = Image.open(src)
    img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    show(img, progress=progress)
