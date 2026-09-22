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
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import tty
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


class Stopped(Exception):
    pass


class CommandWatcher:
    """Tells when the user has run a command since picat returned the prompt: the terminal's
    foreground process group changes from picat's to the shell's, then to the command's."""

    def __init__(self, foreground, own):
        self.foreground, self.own, self.shell = foreground, own, None

    def command_started(self):
        try:
            fg = self.foreground()
        except OSError:
            return False
        if fg == self.own:
            return False
        if self.shell is None:
            self.shell = fg
        return fg != self.shell


def estimate_rate(small, large):
    """Link rate in bytes/s from two (bytes, seconds until kitty's reply) measurements. The
    difference cancels the latency. Timings are noisy, and too high an estimate would queue data
    ahead of the shell's output, so it is capped at 1.5 times the larger transfer's plain average
    (which includes the latency, so underestimates the rate), and falls back to that average."""
    (b1, t1), (b2, t2) = small, large
    average = b2 / max(t2, 1e-3)
    if t2 > t1 and b2 > b1:
        return min((b2 - b1) / (t2 - t1), 1.5 * average)
    return average


def wait_ok(fd, image_id, timeout):
    """Wait for kitty's reply about `image_id` on `fd`: True for OK, False for an error or none."""
    end, buf = time.monotonic() + timeout, b""
    pattern = re.compile(rb"\x1b_Gi=(\d+)[^;]*;([^\x1b]*)\x1b\\")
    while True:
        for m in pattern.finditer(buf):
            if int(m.group(1)) == image_id:
                return m.group(2) == b"OK"
        left = end - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return False
        buf += os.read(fd, 4096)


class Pacer:
    """Sleeps as needed so that data goes out no faster than `rate` bytes/s."""

    def __init__(self, rate, clock=time.monotonic, sleep=time.sleep):
        self.rate, self.clock, self.sleep = rate, clock, sleep
        self.start, self.total = clock(), 0

    def sent(self, n):
        self.total += n
        ahead = self.start + self.total / self.rate - self.clock()
        if ahead > 0:
            self.sleep(ahead)


def terminal_replies():
    """A function that waits for kitty's reply about an image, reading from the terminal, and one
    that puts the terminal back as it was. None if there is no terminal to read."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        saved = termios.tcgetattr(fd)
    except OSError:
        return None
    tty.setcbreak(fd)  # the reply is not echoed, and arrives without waiting for a newline

    def restore():
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        os.close(fd)

    return (lambda image_id: wait_ok(fd, image_id, timeout=2)), restore


def show(img, out=None, strip_count=24, progress=False, detach=False, stop=lambda: False, reply=None):
    """Draw `img` below the cursor.

    With `detach`, return once the first two previews are on screen and send the rest from a
    background process, so the prompt comes back quickly. The link rate is measured from how long
    kitty takes to acknowledge those two previews, and the rest is sent a little slower than that,
    so the shell's output is never stuck behind a queue of image data. The background process
    stops when the user runs a command. With a reply to only the second preview, its plain
    average rate is used; with none, it all runs in the foreground instead.

    `stop()` is checked before each chunk sent (the background process watches for commands this
    way), and `reply(image_id)` (for tests) stands in for waiting for kitty's reply."""
    out = out or sys.stdout
    cols, rows, cw, ch = terminal_geometry()
    target = fit(img.size, (cols * cw, max(1, rows - 3) * ch))  # room for the bar and the prompt
    ncols, nrows = cells(target, (cw, ch))
    ncols, nrows = min(ncols, len(DIACRITICS)), min(nrows, len(DIACRITICS))
    indent = (cols - ncols) // 2  # centred, as icat does
    wrap = wrap_tmux if os.environ.get("TMUX") else (lambda s: s)

    # The first preview is shown through placeholder text, written once. Everything after it is
    # placed relative to that (kitty keeps such placements in step with the text as it scrolls),
    # so the image can improve without touching the text, while the shell uses the terminal.
    # Placeholder images keep their aspect ratio inside their box of cells, while relative
    # placements given a size in cells are stretched to fill it, so previews are padded to exactly
    # the box's shape; the full-resolution strips are placed at their natural pixel size instead.
    # Each cell row is a whole number of image pixels, so strips need no scaling.
    k = max(1, round(ch)) / ch
    target = max(1, round(target[0] * k)), max(1, round(target[1] * k))
    box = round(ncols * cw * k), nrows * max(1, round(ch))
    offset = (box[0] - target[0]) // 2, (box[1] - target[1]) // 2
    bands = strips(box[1], nrows, strip_count)
    sizes = preview_sizes(box)
    detach = detach and len(sizes) >= 2  # two previews are needed to measure the link

    sent, start, pacer = 0, time.monotonic(), None

    def send(data, control):
        nonlocal sent
        for n, cmd in enumerate(graphics_commands(data, control)):
            if stop():  # checked before every chunk, so the terminal is freed within one chunk
                if n:  # end this image's chunks, so kitty is ready for another program's image
                    out.write(wrap(f"{ESC}_Gm=0;{ESC}\\"))
                    out.flush()
                raise Stopped
            out.write(wrap(cmd))
            out.flush()  # one write per sequence, so the shell's output cannot split it
            sent += len(cmd)
            if pacer:
                pacer.sent(len(cmd))

    def bar(remaining):  # `remaining`: estimated bytes still to send
        if progress:
            total = sent + remaining
            rate = sent / max(time.monotonic() - start, 1e-3)
            out.write("\r" + " " * indent + progress_bar(min(1, sent / total), max(0, total - sent) / rate, max(20, ncols)) + f"{ESC}[K")
            out.flush()

    def preview(size):
        s = size[0] / box[0]
        inner = max(1, round(target[0] * s)), max(1, round(target[1] * s))
        small = img.resize(inner, Image.LANCZOS, reducing_gap=3.0)
        return s, small_png(small, size, (round(offset[0] * s), round(offset[1] * s)))

    parent = random.randint(1, 2**24 - 5 - strip_count)  # later stages take the ids after it
    grid = f"c={ncols},r={nrows}"
    below = f"p=1,P={parent},Q=1,H=0,C=1"  # placed relative to the first preview
    restore = None
    if detach and reply is None:
        replies = terminal_replies()
        if replies:
            reply, restore = replies
        else:
            detach = False
    quiet = "q=0" if detach else "q=2"  # q=0: kitty replies OK once it has the whole image

    try:
        data = preview(sizes[0])[1] if sizes else png(img if img.size == target else img.resize(target, Image.LANCZOS))
        before, t0 = sent, time.monotonic()  # time the link only, not preparing the image
        send(data, f"a=T,U=1,f=100,i={parent},p=1,{grid},{quiet}")
        out.write(placeholders(parent, ncols, nrows, indent) + "\n")
        out.flush()
        if not sizes:
            return
        shown, later = None, list(enumerate(sizes[1:]))
        if detach:
            first = (sent - before, time.monotonic() - t0) if reply(parent) else None
            s, data = preview(sizes[1])
            before, t0 = sent, time.monotonic()
            send(data, f"a=T,f=100,i={parent + 1},V=0,{grid},z=1,{below},q=0")
            second = (sent - before, time.monotonic() - t0) if reply(parent + 1) else None
            shown, later = parent + 1, later[1:]
            log(f"replies {first} {second}")
            if first and second:
                rate = estimate_rate(first, second)
            elif second:  # its plain average underestimates the rate, so is safe
                rate = second[0] / max(second[1], 1e-3)
            if second:
                log(f"rate {8 * rate / 1e6:.1f} Mbit/s, pacing at 90%")
            else:
                detach = False  # no replies: without the rate, sending in the background would
                # queue data ahead of the shell's output, so carry on in the foreground
    finally:
        if restore:
            restore()

    if detach:
        if os.fork():
            return
        # Stay in the terminal's session, so its foreground process group can be read.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        fd = out.fileno()
        stop = CommandWatcher(lambda: os.tcgetpgrp(fd), os.getpgrp()).command_started
        pacer = Pacer(0.9 * rate)
    try:
        try:
            ended = rest(img, out, send, bar, preview, later, shown, bands, target, offset, parent, grid, below)
        except Stopped:
            ended = "stopped for a command"
        log(f"{ended} after {time.monotonic() - start:.2f} s, {sent} bytes")
    finally:
        if progress:
            out.write(f"\r{ESC}[2K")
            out.flush()
        if detach:
            os._exit(0)


def rest(img, out, send, bar, preview, later, shown, bands, target, offset, parent, grid, below):
    """Everything still to send: the remaining previews, as (index, size), then the
    full-resolution strips. Images are prepared in background threads while earlier ones are
    sent."""
    wrap = wrap_tmux if os.environ.get("TMUX") else (lambda s: s)
    b64 = lambda n: 4 * math.ceil(n / 3)
    encoded, previews = queue.Queue(), queue.Queue()

    def encode():
        final = img if img.size == target else img.resize(target, Image.LANCZOS)
        oy = offset[1]
        for _, _, y0, y1 in bands:
            if oy <= y0 and y1 <= oy + target[1]:  # no padding in this strip, so no alpha needed
                encoded.put(png(final.crop((0, y0 - oy, target[0], y1 - oy))))
            else:
                encoded.put(png(pad(final.crop((0, max(0, y0 - oy), target[0], min(target[1], y1 - oy))),
                                    (target[0], y1 - y0), (0, max(0, oy - y0)))))

    threading.Thread(target=lambda: [previews.put(preview(size)) for _, size in later], daemon=True).start()
    threading.Thread(target=encode, daemon=True).start()

    for n, _ in later:
        s, data = previews.get()
        send(data, f"a=T,f=100,i={parent + 1 + n},V=0,{grid},z={1 + n},{below},q=2")
        if shown:  # the new preview now covers it
            out.write(wrap(f"{ESC}_Ga=d,d=I,i={shown},q=2{ESC}\\"))
        shown = parent + 1 + n
        bar(b64(len(data)) / s**2)  # the full image, judged from this preview

    strip_bytes = []
    for j, (r0, r1, y0, y1) in enumerate(bands):
        data = encoded.get()
        strip_bytes.append(b64(len(data)))
        send(data, f"a=T,f=100,i={parent + 4 + j},V={r0},X={offset[0]},z=10,{below},q=2")
        bar((len(bands) - j - 1) * sum(strip_bytes) / len(strip_bytes))
    if shown:
        out.write(wrap(f"{ESC}_Ga=d,d=I,i={shown},q=2{ESC}\\"))
    out.flush()
    return "finished"


def log(message):
    """Append to the file named by $PICAT_LOG, if set: for finding out what a run did."""
    path = os.environ.get("PICAT_LOG")
    if path:
        with open(path, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {os.getpid()} {message}\n")


def main():
    args = sys.argv[1:]
    progress = bool({"--progress", "-p"} & set(args))
    args = [a for a in args if a not in ("--progress", "-p")]
    if len(args) != 1 or args[0] in ("-h", "--help"):
        sys.exit(__doc__.strip())
    src = sys.stdin.buffer if args[0] == "-" else args[0]
    img = Image.open(src)
    img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    show(img, progress=progress, detach=not progress)
