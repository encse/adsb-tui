import argparse
import math
import os
import select
import shutil
import sys
import termios
import tty
from pathlib import Path
from typing import Optional, Sequence, Tuple

from .renderer import Renderer
from .style import Styler
from .tiles import HttpTileSource
from .utils import base_zoom, lonlat_to_tile, normalize, tile_size_at_zoom, tile_to_lonlat


DEFAULT_SOURCE = "http://mapscii.me/"
DEFAULT_LATITUDE = 52.51298
DEFAULT_LONGITUDE = 13.42012
DEFAULT_STYLE = Path(__file__).resolve().parents[1] / "styles" / "dark.json"


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MapSCII's vector-tile renderer ported to Python")
    parser.add_argument("--latitude", "--lat", type=float, default=DEFAULT_LATITUDE)
    parser.add_argument("--longitude", "--lon", type=float, default=DEFAULT_LONGITUDE)
    parser.add_argument("--zoom", "-z", type=float)
    parser.add_argument("--width", "-w", type=int, help="map width in terminal columns")
    parser.add_argument("--height", type=int, help="map height in terminal rows")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--style", type=Path, default=DEFAULT_STYLE)
    parser.add_argument("--ascii", action="store_true", help="use block characters instead of Braille")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--cache-directory", type=Path)
    parser.add_argument("--once", action="store_true", help="render one frame and exit")
    return parser.parse_args(argv)


class Application:
    def __init__(self, arguments: argparse.Namespace) -> None:
        columns, rows = shutil.get_terminal_size((80, 24))
        self.columns = arguments.width or columns
        self.rows = arguments.height or max(2, rows - 1)
        self.latitude = arguments.latitude
        self.longitude = arguments.longitude
        pixel_width = self.columns * 2
        pixel_height = self.rows * 4
        self.minimum_zoom = 4 - math.log2(4096 / pixel_width)
        self.zoom = arguments.zoom if arguments.zoom is not None else self.minimum_zoom
        self.zoom = max(self.minimum_zoom, min(18.0, self.zoom))

        styler = Styler.from_file(arguments.style)
        source = HttpTileSource(
            arguments.source,
            styler,
            cache_directory=arguments.cache_directory,
            persist=not arguments.no_cache,
        )
        self.renderer = Renderer(source, styler, pixel_width, pixel_height, not arguments.ascii)

    def frame(self) -> str:
        return self.renderer.draw(self.latitude, self.longitude, self.zoom)

    def draw(self, clear: bool = True) -> None:
        prefix = "\x1b[2J\x1b[H" if clear else ""
        footer = "center: {:.3f}, {:.3f}  zoom: {:.2f}  arrows/hjkl move  a/z zoom  q quit  © OSM\n".format(
            self.latitude, self.longitude, self.zoom
        )
        sys.stdout.write(prefix + self.frame() + "\x1b[0m" + footer)
        sys.stdout.flush()

    def move(self, dx: float, dy: float) -> None:
        z = base_zoom(self.zoom)
        center_x, center_y = lonlat_to_tile(self.longitude, self.latitude, z)
        size = tile_size_at_zoom(self.zoom)
        lon, lat = tile_to_lonlat(center_x + dx / size, center_y + dy / size, z)
        self.longitude, self.latitude = normalize(lon, lat)

    def run(self) -> None:
        old_settings = termios.tcgetattr(sys.stdin.fileno())
        try:
            tty.setcbreak(sys.stdin.fileno())
            self.draw()
            while True:
                key = os.read(sys.stdin.fileno(), 1)
                if key == b"\x1b":
                    while select.select([sys.stdin], [], [], 0.01)[0]:
                        key += os.read(sys.stdin.fileno(), 1)
                if key in (b"q", b"Q"):
                    return
                if key in (b"a",):
                    self.zoom = min(18.0, self.zoom + 0.2)
                elif key in (b"z", b"y"):
                    self.zoom = max(self.minimum_zoom, self.zoom - 0.2)
                elif key in (b"h", b"\x1b[D"):
                    self.move(-16, 0)
                elif key in (b"l", b"\x1b[C"):
                    self.move(16, 0)
                elif key in (b"k", b"\x1b[A"):
                    self.move(0, -12)
                elif key in (b"j", b"\x1b[B"):
                    self.move(0, 12)
                else:
                    continue
                self.draw()
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            sys.stdout.write("\x1b[0m\n")


def main(argv: Optional[Sequence[str]] = None) -> None:
    arguments = parse_arguments(argv)
    application = Application(arguments)
    if arguments.once:
        application.draw(clear=False)
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit("interactive mode requires a TTY; use --once for redirected output")
        application.run()
