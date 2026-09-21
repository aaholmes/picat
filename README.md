# picat

Shows an image in the [kitty](https://sw.kovidgoyal.net/kitty/) terminal quickly over a slow
connection, such as ssh. It first sends a small preview (a quarter of the width, so 1/16 of the
pixels) stretched to the final size, then sends the full-resolution version, which kitty draws
over the preview in place. The image is scaled to fit the terminal window, so no more pixels are
sent than can be shown.

```sh
uv tool install -e .     # installs the `picat` command
picat image.png
some-command | picat -
```

It works inside tmux, using kitty's Unicode placeholders (text cells that tmux treats as ordinary
characters and kitty draws the image over), provided tmux has `set -g allow-passthrough on`.

On a 10 Mbit/s link, the preview of a 3072×2048 image arrives in about 0.2 s and the full image,
sized to a 2000×1050-pixel window, in about 3 s.
