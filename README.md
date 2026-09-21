# picat

Shows an image in the [kitty](https://sw.kovidgoyal.net/kitty/) terminal quickly over a slow
connection, such as ssh. A tiny preview (1/16 of the width) appears almost at once, then sharper
ones (1/4 and 1/2 of the width), all stretched to the final size. The full-resolution image then arrives in
horizontal strips, each replacing the preview where it lands, while a bar below shows the time
left. The image is scaled to fit the terminal window, so no more pixels are sent than can be shown.

```sh
uv tool install -e .     # installs the `picat` command
picat image.png
some-command | picat -
```

It works inside tmux, using kitty's Unicode placeholders (text cells that tmux treats as ordinary
characters and kitty draws the image over), provided tmux has `set -g allow-passthrough on`.

## Compared with `kitten icat`

Both programs ran in a 2000×1416-pixel terminal, with their output read at 10 Mbit/s, on a photo
and on a generated image (a 3×2 grid of 1024×1024 images), each at three sizes. Images wider than
2000 pixels are shrunk to 2000×1283 (photo) or 2000×1333 (generated) by both. Times are the
median of 3 runs, which differed by under 0.02 s.

| Image | First pixels, icat | First pixels, picat | Full image, icat | Full image, picat | Sent, icat | Sent, picat |
|---|---|---|---|---|---|---|
| Photo 1562×1002 | 0.45 s | 0.04 s | 0.51 s | 0.74 s | 0.54 MB | 0.85 MB |
| Photo 3125×2005 | 0.88 s | 0.07 s | 0.97 s | 1.31 s | 0.96 MB | 1.52 MB |
| Photo 6250×4010 | 1.07 s | 0.15 s | 1.17 s | 1.52 s | 1.05 MB | 1.68 MB |
| Generated 768×512 | 0.87 s | 0.04 s | 0.89 s | 1.09 s | 1.06 MB | 1.26 MB |
| Generated 1536×1024 | 3.13 s | 0.08 s | 3.19 s | 3.78 s | 3.86 MB | 4.52 MB |
| Generated 3072×2048 | 7.89 s | 0.18 s | 7.98 s | 6.19 s | 9.30 MB | 7.34 MB |

icat's first pixels are its whole image, since it draws nothing until all the data has arrived.
The whole image usually arrives 0.2–0.6 s later with picat, because its three previews add
13–50% to the data sent. It arrives sooner only for the largest generated image, which both
programs must shrink to fit the window. There icat sends raw pixels compressed with zlib, while
picat sends PNG, which first predicts each pixel from its neighbours and compresses only the
difference; on a smooth generated image that makes the data about a third smaller. On the photo the
prediction does not help, and zlib on raw pixels comes out about 10% smaller than PNG. When an
image is a PNG that already fits, icat sends the file unchanged.
