# MapSCII Python renderer

An initial Python port of MapSCII's vector-tile renderer. It deliberately uses
the original MapSCII endpoint and style files so its output can be compared to
the JavaScript application before it is embedded in the ADS-B TUI.

## Install

```sh
python3 -m pip install -r requirements.txt
```

## Run

```sh
python3 mapscii.py
```

Arrow keys or `hjkl` move the map, `a` zooms in, `z` zooms out, and `q` exits.
Render one non-interactive frame with:

```sh
python3 mapscii.py --once --width 80 --height 24
```

The original MapSCII code is MIT licensed. Map data attribution: OpenStreetMap
contributors.

## Rich Panel

`MapView` is a Rich renderable. It adapts its map width to the space offered by
the surrounding layout and keeps a fixed number of terminal rows:

```python
from rich.console import Console
from rich.panel import Panel

from mapscii_py.rich_map import MapView

map_view = MapView(
    latitude=47.4979,
    longitude=19.0402,
    zoom=8,
    height=16,
)

Console().print(Panel(map_view, title="Budapest airspace", padding=0))
```

There is also a standalone demo:

```sh
python3 panel_demo.py --width 72 --height 18 --zoom 8
```


## Origin and license

This Python implementation is derived from
[MapSCII](https://github.com/rastapasta/mapscii), originally created by
Michael Straßburger and the MapSCII contributors.

The renderer was ported and adapted for Rich-based Python applications.
It remains available under the MIT License; see `LICENSE`.