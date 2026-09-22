# picat

Shows an image in the [kitty](https://sw.kovidgoyal.net/kitty/) terminal quickly over a slow
connection, such as one over ssh. A tiny preview (1/16 of the width) appears almost at once, then
sharper ones (1/4 and 1/2 of the width), all stretched to the final size. The full-resolution
image then arrives in horizontal strips, each covering the preview where it lands, the most
detailed parts first, so that an interrupted load leaves the parts that gain most from it sharp.
The image is scaled to fit the terminal window, so no more pixels are sent than can be shown.

![kitten icat and picat side by side, showing the same photo over a 10 Mbit/s link](docs/picat-vs-icat.gif)

picat (progressive icat) shows a blurry preview after 0.2 s and a sharp image by 0.8 s; `kitten
icat` shows nothing until the whole image arrives at 4.1 s. Both ran in kitty 0.32.2 windows of
1000×1400 pixels, with their output passed to kitty at 10 Mbit/s, and picat had already measured
the link's rate. The
photo, of Yosemite National Park, is by
[Vulturesong](https://commons.wikimedia.org/wiki/File:Yosemite_National_Park_-_HCP_-_October_07,_2022_-_012.jpg)
and in the public domain (CC0).

```sh
uv tool install -e .     # installs the `picat` command
picat image.png
some-command | picat -
picat --progress image.png
```

The prompt comes back as soon as the first preview is on screen, and the rest of the image loads
in the background while you carry on typing. Everything goes to kitty over the one connection, so
if the image data were sent as fast as possible, your typing would be echoed only after it. picat
therefore sends the rest at 90% of the link's rate, which it measures from how long kitty takes to
acknowledge the first two previews, and reuses for 10 minutes on the same ssh connection. The
first run in that time waits for those acknowledgements, about half a second on a 10 Mbit/s link,
and anything typed during that wait is lost. If you run another command before the image has
finished, loading stops, and the image stays at whichever stage it had reached.

`--progress` instead loads everything before returning the prompt, with a bar below the image
showing the time left. Setting `PICAT_LOG` to a file name logs the measured rate and how each
background load ended.

It works inside tmux, the terminal multiplexer that splits a terminal into panes and keeps
sessions running between logins, using kitty's Unicode placeholders: text cells that tmux passes
through as ordinary characters and kitty draws the image over. tmux needs `set -g
allow-passthrough on`. picat needs kitty 0.31 or later, which can position one image relative to
another, as the previews are while they load.

## Compared with `kitten icat`

`kitten icat` is the image viewer that comes with kitty. Both programs ran in a 2000×1416-pixel
terminal, with their output read at 10 Mbit/s, on a photo and on a generated image (a 3×2 grid of
1024×1024 images), each at three sizes. Images wider than 2000 pixels are shrunk to 2000×1283
(photo) or 2000×1333 (generated) by both. picat already knew the link's rate, so it returned the
prompt without waiting for acknowledgements. Times are the median of 3 runs, which differed by at
most 0.10 s.

| Image | Full image, icat | First pixels, picat | Prompt back, picat | Full image, picat | Sent, icat | Sent, picat |
|---|---|---|---|---|---|---|
| Photo 1562×1002 | 0.51 s | 0.06 s | 0.11 s | 0.64 s | 0.53 MB | 0.68 MB |
| Photo 3125×2005 | 0.98 s | 0.10 s | 0.18 s | 1.18 s | 0.96 MB | 1.25 MB |
| Photo 6250×4010 | 1.18 s | 0.17 s | 0.30 s | 1.41 s | 1.05 MB | 1.40 MB |
| Generated 768×512 | 0.89 s | 0.05 s | 0.06 s | 1.11 s | 1.06 MB | 1.21 MB |
| Generated 1536×1024 | 3.20 s | 0.09 s | 0.14 s | 3.93 s | 3.86 MB | 4.35 MB |
| Generated 3072×2048 | 8.03 s | 0.20 s | 0.29 s | 6.45 s | 9.30 MB | 7.06 MB |

icat draws nothing until all the data has arrived, and returns the prompt then. With picat the
whole image usually arrives 0.1–0.7 s later than with icat, because its three previews add 11–36%
to the data sent, and the rest is sent at 90% of the link's rate. It arrives sooner only for the
largest generated image, which both programs must shrink to fit the window. There icat sends raw
pixels compressed with the general-purpose zlib compressor, while picat sends PNG (Portable
Network Graphics), which first predicts each pixel from its neighbours and compresses only the
difference; on a smooth generated image that makes the data about a quarter smaller. On the photo
the prediction does not help, and zlib on raw pixels comes out about 10% smaller than PNG. When an
image is a PNG that already fits the window, icat sends the file unchanged.

## License

MIT; see [LICENSE](LICENSE).
