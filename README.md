# picat

Shows an image in the [kitty](https://sw.kovidgoyal.net/kitty/) terminal quickly over a slow
connection, such as ssh. A tiny preview (1/16 of the width) appears almost at once, then a sharper
one (1/4 of the width), both stretched to the final size. The full-resolution image then arrives in
horizontal strips, each replacing the preview where it lands, while a bar below shows the time
left. The image is scaled to fit the terminal window, so no more pixels are sent than can be shown.

```sh
uv tool install -e .     # installs the `picat` command
picat image.png
some-command | picat -
```

It works inside tmux, using kitty's Unicode placeholders (text cells that tmux treats as ordinary
characters and kitty draws the image over), provided tmux has `set -g allow-passthrough on`.

For a 6250×4010 photo shown in a 2000×1250-pixel window, over a simulated 10 Mbit/s link, the
first preview appears after about 0.05 s and the full image after about 1.2 s.
