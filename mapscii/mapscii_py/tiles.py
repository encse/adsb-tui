import gzip
import hashlib
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .style import Styler, paint_value
from .utils import xterm_color


@dataclass
class Feature:
    layer: str
    kind: str
    geometry: Any
    color: Optional[int]
    width: int
    label: Optional[str]
    sort: int
    min_zoom: Optional[float]
    max_zoom: Optional[float]
    bounds: Tuple[float, float, float, float]


@dataclass
class Tile:
    layers: Dict[str, Tuple[int, List[Feature]]]

    @classmethod
    def decode(cls, data: bytes, styler: Styler, language: str = "en") -> "Tile":
        try:
            import mapbox_vector_tile
        except ImportError as error:
            raise RuntimeError(
                "Missing dependency: install it with "
                "'python3 -m pip install -r requirements.txt'"
            ) from error

        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        decoded = mapbox_vector_tile.decode(
            data,
            default_options={"y_coord_down": True, "geojson": True},
        )
        layers: Dict[str, Tuple[int, List[Feature]]] = {}
        for layer_name, layer in decoded.items():
            extent = int(layer.get("extent", 4096))
            features: List[Feature] = []
            for raw_feature in layer.get("features", []):
                properties = dict(raw_feature.get("properties", {}))
                geometry = raw_feature.get("geometry") or {}
                geometry_type = geometry.get("type", "")
                properties["$type"] = cls._mapbox_type(geometry_type)
                style = styler.style_for(layer_name, properties)
                if not style:
                    continue
                color_value = paint_value(style, "line-color", "fill-color", "text-color")
                color = xterm_color(color_value) if color_value else None
                width_value = paint_value(style, "line-width") or 1
                try:
                    width = max(1, int(round(float(width_value))))
                except (TypeError, ValueError):
                    width = 1
                label = None
                if style.get("type") == "symbol":
                    label = (
                        properties.get("name_" + language)
                        or properties.get("name_en")
                        or properties.get("name")
                        or properties.get("house_num")
                    )
                sort = properties.get("localrank") or properties.get("scalerank") or 0
                for normalized in cls._normalize_geometry(geometry_type, geometry.get("coordinates", []), style.get("type")):
                    points = list(cls._all_points(normalized))
                    if not points:
                        continue
                    xs = [point[0] for point in points]
                    ys = [point[1] for point in points]
                    features.append(Feature(
                        layer=layer_name,
                        kind=style.get("type", "line"),
                        geometry=normalized,
                        color=color,
                        width=width,
                        label=str(label) if label else None,
                        sort=int(sort),
                        min_zoom=style.get("minzoom"),
                        max_zoom=style.get("maxzoom"),
                        bounds=(min(xs), min(ys), max(xs), max(ys)),
                    ))
            layers[layer_name] = (extent, features)
        return cls(layers)

    @staticmethod
    def _mapbox_type(geometry_type: str) -> str:
        if "Point" in geometry_type:
            return "Point"
        if "LineString" in geometry_type:
            return "LineString"
        if "Polygon" in geometry_type:
            return "Polygon"
        return geometry_type

    @staticmethod
    def _normalize_geometry(geometry_type: str, coordinates: Any, style_type: str) -> Iterable[Any]:
        if style_type == "symbol":
            if geometry_type == "Point":
                yield [tuple(coordinates)]
            elif geometry_type == "MultiPoint":
                yield [tuple(point) for point in coordinates]
            elif geometry_type == "LineString":
                yield [tuple(point) for point in coordinates]
            elif geometry_type == "MultiLineString":
                for line in coordinates:
                    yield [tuple(point) for point in line]
            return

        if geometry_type == "LineString":
            yield [tuple(point) for point in coordinates]
        elif geometry_type == "MultiLineString":
            for line in coordinates:
                yield [tuple(point) for point in line]
        elif geometry_type == "Polygon":
            if style_type == "fill":
                yield [[tuple(point) for point in ring] for ring in coordinates]
            else:
                for ring in coordinates:
                    yield [tuple(point) for point in ring]
        elif geometry_type == "MultiPolygon":
            for polygon in coordinates:
                if style_type == "fill":
                    yield [[tuple(point) for point in ring] for ring in polygon]
                else:
                    for ring in polygon:
                        yield [tuple(point) for point in ring]

    @classmethod
    def _all_points(cls, geometry: Any) -> Iterable[Tuple[float, float]]:
        if not geometry:
            return
        first = geometry[0]
        if isinstance(first, tuple):
            for point in geometry:
                yield point
        else:
            for part in geometry:
                yield from cls._all_points(part)


class HttpTileSource:
    def __init__(
        self,
        source: str,
        styler: Styler,
        cache_directory: Optional[Path] = None,
        persist: bool = True,
        memory_size: int = 32,
    ) -> None:
        self.source = source.rstrip("/") + "/"
        self.styler = styler
        self.persist = persist
        self.cache_directory = cache_directory or Path.home() / ".cache" / "mapscii-python"
        self.memory_size = memory_size
        self.memory: "OrderedDict[Tuple[int, int, int], Tile]" = OrderedDict()

    def get_tile(self, z: int, x: int, y: int) -> Tile:
        key = (z, x, y)
        cached = self.memory.get(key)
        if cached is not None:
            self.memory.move_to_end(key)
            return cached

        data = self._load_data(z, x, y)
        tile = Tile.decode(data, self.styler)
        self.memory[key] = tile
        while len(self.memory) > self.memory_size:
            self.memory.popitem(last=False)
        return tile

    def _load_data(self, z: int, x: int, y: int) -> bytes:
        cache_file = self._cache_file(z, x, y)
        if self.persist and cache_file.exists():
            return cache_file.read_bytes()

        url = self.source + "{}/{}/{}.pbf".format(z, x, y)
        request = urllib.request.Request(url, headers={"User-Agent": "mapscii-python/0.1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
        if self.persist:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(data)
        return data

    def _cache_file(self, z: int, x: int, y: int) -> Path:
        source_id = hashlib.sha1(self.source.encode("utf-8")).hexdigest()[:12]
        return self.cache_directory / source_id / str(z) / str(x) / (str(y) + ".pbf")
