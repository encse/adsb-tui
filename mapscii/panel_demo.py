#!/usr/bin/env python3

import argparse
from pathlib import Path

from rich.console import Console

from mapscii_py.rich_map import DEFAULT_STYLE, MapView


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a MapSCII viewport in a Rich Panel")
    parser.add_argument("--lat", type=float, default=47.4979)
    parser.add_argument("--lon", type=float, default=19.0402)
    parser.add_argument("--zoom", type=float, default=8.0)
    parser.add_argument("--width", type=int, default=72)
    parser.add_argument("--height", type=int, default=18)
    parser.add_argument("--style", type=Path, default=DEFAULT_STYLE)
    parser.add_argument("--source", default="http://mapscii.me/")
    parser.add_argument("--cache-directory", type=Path)
    args = parser.parse_args()

    view = MapView(
        latitude=args.lat,
        longitude=args.lon,
        zoom=args.zoom,
        height=args.height,
        source=args.source,
        style=args.style,
        cache_directory=args.cache_directory,
    )

    Console().print(
        view.panel(
            title="[bold cyan]BUDAPEST AIRSPACE[/]",
            width=args.width,
        )
    )


if __name__ == "__main__":
    main()

