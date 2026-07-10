import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .canvas import Canvas
from .style import Styler
from .tiles import Feature, HttpTileSource, Tile
from .utils import base_zoom, lonlat_to_tile, tile_size_at_zoom, xterm_color


DRAW_ORDER_LOW = ("admin", "water", "country_label", "marine_label")
DRAW_ORDER = (
    "landuse", "water", "marine_label", "building", "road", "admin",
    "country_label", "state_label", "water_label", "place_label",
    "rail_station_label", "poi_label", "road_label", "housenum_label",
)


@dataclass
class VisibleTile:
    xyz: Tuple[int, int, int]
    zoom: float
    position: Tuple[float, float]
    size: float
    data: Optional[Tile] = None


class LabelBuffer:
    def __init__(self) -> None:
        self.areas: List[Tuple[float, float, float, float]] = []

    def clear(self) -> None:
        self.areas.clear()

    def reserve(self, text: str, x: int, y: int, margin: int = 5) -> bool:
        column, row = x // 2, y // 4
        area = (column - margin, row - margin / 2, column + len(text) + margin, row + margin / 2)
        if any(self._intersects(area, existing) for existing in self.areas):
            return False
        self.areas.append(area)
        return True

    @staticmethod
    def _intersects(left: Tuple[float, ...], right: Tuple[float, ...]) -> bool:
        return not (left[2] < right[0] or left[0] > right[2] or left[3] < right[1] or left[1] > right[3])


class Renderer:
    def __init__(
        self,
        tile_source: HttpTileSource,
        styler: Styler,
        width: int,
        height: int,
        use_braille: bool = True,
    ) -> None:
        self.tile_source = tile_source
        self.styler = styler
        self.canvas = Canvas(width, height, use_braille)
        self.width = self.canvas.buffer.width
        self.height = self.canvas.buffer.height
        self.labels = LabelBuffer()
        self.seen_labels: Set[str] = set()

    def draw(self, latitude: float, longitude: float, zoom: float) -> str:
        self.canvas.clear()
        self.labels.clear()
        self.seen_labels.clear()
        background = self.styler.background()
        self.canvas.set_background(xterm_color(background) if background else None)

        tiles = self.visible_tiles(latitude, longitude, zoom)
        for tile in tiles:
            tile.data = self.tile_source.get_tile(*tile.xyz)
        self._render_tiles(tiles)
        return self.canvas.frame()

    def visible_tiles(self, latitude: float, longitude: float, zoom: float) -> List[VisibleTile]:
        z = base_zoom(zoom)
        center_x, center_y = lonlat_to_tile(longitude, latitude, z)
        tile_size = tile_size_at_zoom(zoom)
        radius_x = math.ceil(self.width / tile_size / 2) + 1
        radius_y = math.ceil(self.height / tile_size / 2) + 1
        grid_size = 2 ** z
        visible: List[VisibleTile] = []

        for raw_y in range(math.floor(center_y) - radius_y, math.floor(center_y) + radius_y + 1):
            for raw_x in range(math.floor(center_x) - radius_x, math.floor(center_x) + radius_x + 1):
                position_x = self.width / 2 - (center_x - raw_x) * tile_size
                position_y = self.height / 2 - (center_y - raw_y) * tile_size
                if raw_y < 0 or raw_y >= grid_size:
                    continue
                if position_x + tile_size < 0 or position_y + tile_size < 0:
                    continue
                if position_x > self.width or position_y > self.height:
                    continue
                visible.append(VisibleTile(
                    xyz=(z, raw_x % grid_size, raw_y),
                    zoom=zoom,
                    position=(position_x, position_y),
                    size=tile_size,
                ))
        return visible

    def _render_tiles(self, tiles: Sequence[VisibleTile]) -> None:
        draw_order = DRAW_ORDER_LOW if tiles and tiles[0].zoom < 2 else DRAW_ORDER
        labels: List[Tuple[VisibleTile, Feature, int]] = []
        for layer_name in draw_order:
            for tile in tiles:
                if tile.data is None or layer_name not in tile.data.layers:
                    continue
                extent, features = tile.data.layers[layer_name]
                for feature in features:
                    if feature.kind == "symbol":
                        labels.append((tile, feature, extent))
                    else:
                        self._draw_feature(tile, feature, extent)

        for tile, feature, extent in sorted(labels, key=lambda item: item[1].sort):
            self._draw_feature(tile, feature, extent)

    def _draw_feature(self, tile: VisibleTile, feature: Feature, extent: int) -> None:
        if feature.min_zoom is not None and tile.zoom < feature.min_zoom:
            return
        if feature.max_zoom is not None and tile.zoom > feature.max_zoom:
            return
        scale = extent / tile.size

        if feature.kind == "line":
            points = self._scale_points(tile, feature.geometry, scale)
            if len(points) >= 2:
                self.canvas.polyline(points, feature.color, feature.width)
        elif feature.kind == "fill":
            rings = [self._scale_points(tile, ring, scale) for ring in feature.geometry]
            self.canvas.polygon([ring for ring in rings if len(ring) >= 3], feature.color)
        elif feature.kind == "symbol":
            text = feature.label or "◉"
            if text in self.seen_labels and feature.label:
                return
            for x, y in self._scale_points(tile, feature.geometry, scale):
                start_x = x - len(text)
                if self.labels.reserve(text, start_x, y):
                    self.canvas.text(text, start_x, y, feature.color)
                    self.seen_labels.add(text)
                    return

    def _scale_points(
        self,
        tile: VisibleTile,
        points: Sequence[Tuple[float, float]],
        scale: float,
    ) -> List[Tuple[int, int]]:
        output: List[Tuple[int, int]] = []
        previous: Optional[Tuple[int, int]] = None
        for source_x, source_y in points:
            point = (
                math.floor(tile.position[0] + source_x / scale),
                math.floor(tile.position[1] + source_y / scale),
            )
            if point != previous:
                output.append(point)
                previous = point
        return output

