import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

from rich.console import Console, ConsoleOptions, RenderResult
from rich.panel import Panel
from rich.text import Text

from .renderer import Renderer
from .style import Styler
from .tiles import HttpTileSource
from .utils import (
    base_zoom,
    lonlat_to_tile,
    normalize,
    tile_size_at_zoom,
)


DEFAULT_SOURCE = "http://mapscii.me/"
DEFAULT_STYLE = Path(__file__).resolve().parents[1] / "styles" / "dark.json"


@dataclass(frozen=True)
class MapMarker:
    latitude: float
    longitude: float
    label: str
    style: str = "bold white"
    symbol: str = "✈"


class MapView:
    """A fixed-height MapSCII viewport that can be placed in any Rich layout."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        zoom: float,
        *,
        height: int = 16,
        width: Optional[int] = None,
        source: str = DEFAULT_SOURCE,
        style: Path = DEFAULT_STYLE,
        use_braille: bool = True,
        auto_fit: bool = False,
        cache_directory: Optional[Path] = None,
        persist_cache: bool = True,
    ) -> None:
        if height <= 0:
            raise ValueError("height must be positive")
        if width is not None and width <= 0:
            raise ValueError("width must be positive")

        self.latitude = latitude
        self.longitude = longitude
        self.zoom = zoom
        self.height = height
        self.width = width
        self.use_braille = use_braille
        self.auto_fit = auto_fit

        self.styler = Styler.from_file(style)
        self.tile_source = HttpTileSource(
            source,
            self.styler,
            cache_directory=cache_directory,
            persist=persist_cache,
        )
        self._cached_key: Optional[Tuple[object, ...]] = None
        self._cached_text: Optional[Text] = None
        self._cached_error = False
        self._markers: Tuple[MapMarker, ...] = ()

    def set_center(self, latitude: float, longitude: float) -> None:
        longitude, latitude = normalize(longitude, latitude)
        if (latitude, longitude) != (self.latitude, self.longitude):
            self.latitude = latitude
            self.longitude = longitude
            self.invalidate()

    def set_zoom(self, zoom: float) -> None:
        if zoom != self.zoom:
            self.zoom = zoom
            self.invalidate()

    def set_height(self, height: int) -> None:
        if height <= 0:
            raise ValueError("height must be positive")
        if height != self.height:
            self.height = height
            self.invalidate()

    def set_markers(self, markers: Iterable[MapMarker]) -> None:
        self._markers = tuple(markers)

    def invalidate(self) -> None:
        self._cached_key = None
        self._cached_text = None
        self._cached_error = False

    def render_text(self, available_width: int) -> Text:
        columns = self.width if self.width is not None else available_width
        columns = max(2, min(columns, available_width))
        render_zoom = self._fitted_zoom(columns)
        cache_key = (
            columns,
            self.height,
            self.latitude,
            self.longitude,
            render_zoom,
            self.use_braille,
        )
        if cache_key != self._cached_key or self._cached_text is None:
            try:
                renderer = Renderer(
                    self.tile_source,
                    self.styler,
                    width=columns * 2,
                    height=self.height * 4,
                    use_braille=self.use_braille,
                )
                ansi_frame = renderer.draw(
                    self.latitude,
                    self.longitude,
                    render_zoom,
                ).rstrip("\n")
                rendered = Text.from_ansi(ansi_frame)
                self._cached_error = False
            except Exception as error:
                rendered = Text(
                    f"Map unavailable: {error}",
                    style="bold red",
                    justify="center",
                )
                self._cached_error = True

            rendered.no_wrap = True
            rendered.overflow = "crop"

            self._cached_key = cache_key
            self._cached_text = rendered

        if self._cached_error:
            return self._cached_text.copy()

        return self._overlay_markers(
            self._cached_text.copy(),
            columns,
            render_zoom,
        )

    def _overlay_markers(
        self,
        background: Text,
        columns: int,
        render_zoom: float,
    ) -> Text:
        if not self._markers:
            return background

        lines = list(background.split("\n"))

        for marker in self._markers:
            column, row = self._project_marker(
                marker,
                columns,
                render_zoom,
            )

            if row < 0 or row >= len(lines):
                continue

            label = (
                marker.symbol
                + " "
                + " ".join(marker.label.split())
            )

            if column < 0:
                label = label[-column:]
                column = 0

            if column >= columns or not label:
                continue

            label = label[:columns - column]
            line = lines[row]
            lines[row] = (
                line[:column]
                + Text(label, style=marker.style)
                + line[column + len(label):]
            )

        rendered = Text()

        for index, line in enumerate(lines):
            if index:
                rendered.append("\n")
            rendered.append_text(line)

        rendered.no_wrap = True
        rendered.overflow = "crop"
        return rendered

    def _project_marker(
        self,
        marker: MapMarker,
        columns: int,
        render_zoom: float,
    ) -> Tuple[int, int]:
        zoom = base_zoom(render_zoom)
        center_x, center_y = lonlat_to_tile(
            self.longitude,
            self.latitude,
            zoom,
        )
        marker_x, marker_y = lonlat_to_tile(
            marker.longitude,
            marker.latitude,
            zoom,
        )

        grid_size = 2 ** zoom
        delta_x = marker_x - center_x

        if delta_x > grid_size / 2:
            delta_x -= grid_size
        elif delta_x < -grid_size / 2:
            delta_x += grid_size

        tile_size = tile_size_at_zoom(render_zoom)
        pixel_x = columns + delta_x * tile_size
        pixel_y = self.height * 2 + (
            marker_y - center_y
        ) * tile_size

        return (
            math.floor(pixel_x / 2),
            math.floor(pixel_y / 4),
        )

    def _fitted_zoom(self, columns: int) -> float:
        if not self.auto_fit or not self._markers:
            return self.zoom

        center_x, center_y = lonlat_to_tile(
            self.longitude,
            self.latitude,
            0,
        )
        max_delta_x = 0.0
        max_delta_y = 0.0
        longest_label = 0

        for marker in self._markers:
            marker_x, marker_y = lonlat_to_tile(
                marker.longitude,
                marker.latitude,
                0,
            )
            delta_x = abs(marker_x - center_x)
            delta_x = min(delta_x, 1.0 - delta_x)
            max_delta_x = max(max_delta_x, delta_x)
            max_delta_y = max(
                max_delta_y,
                abs(marker_y - center_y),
            )
            longest_label = max(
                longest_label,
                len(marker.label) + 2,
            )

        # Canvas dimensions are 2x4 virtual pixels per terminal cell.
        horizontal_label_space = longest_label * 2
        usable_half_width = max(
            4,
            columns - horizontal_label_space - 4,
        )
        usable_half_height = max(
            4,
            self.height * 2 - 4,
        )

        zoom_x = self.zoom
        zoom_y = self.zoom

        if max_delta_x > 0:
            zoom_x = math.log2(
                usable_half_width
                / (256 * max_delta_x)
            )

        if max_delta_y > 0:
            zoom_y = math.log2(
                usable_half_height
                / (256 * max_delta_y)
            )

        fitted = min(self.zoom, zoom_x, zoom_y)
        fitted = min(18.0, max(0.0, fitted))

        # Quantizing down prevents tiny position updates from redrawing tiles.
        return math.floor(fitted * 5) / 5

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console
        yield self.render_text(options.max_width)

    def panel(
        self,
        *,
        title: str = "[bold cyan]MAP[/]",
        border_style: str = "bright_cyan",
        width: Optional[int] = None,
    ) -> Panel:
        return Panel(
            self,
            title=title,
            title_align="left",
            subtitle="[dim]© OpenStreetMap contributors[/]",
            border_style=border_style,
            padding=0,
            width=width,
            height=self.height + 2,
        )
