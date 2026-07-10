import math
from typing import Dict, Tuple


TILE_RANGE = 14
PROJECT_SIZE = 256
MAX_LATITUDE = 85.0511


def base_zoom(zoom: float) -> int:
    return min(TILE_RANGE, max(0, math.floor(zoom)))


def tile_size_at_zoom(zoom: float) -> float:
    return PROJECT_SIZE * 2 ** (zoom - base_zoom(zoom))


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> Tuple[float, float]:
    lat = min(MAX_LATITUDE, max(-MAX_LATITUDE, lat))
    scale = 2 ** zoom
    x = (lon + 180.0) / 360.0 * scale
    latitude = math.radians(lat)
    y = (1.0 - math.log(math.tan(latitude) + 1.0 / math.cos(latitude)) / math.pi) / 2.0 * scale
    return x, y


def tile_to_lonlat(x: float, y: float, zoom: int) -> Tuple[float, float]:
    scale = 2 ** zoom
    n = math.pi - 2.0 * math.pi * y / scale
    lon = x / scale * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def normalize(lon: float, lat: float) -> Tuple[float, float]:
    while lon < -180.0:
        lon += 360.0
    while lon > 180.0:
        lon -= 360.0
    return lon, min(MAX_LATITUDE, max(-MAX_LATITUDE, lat))


def hex_to_rgb(color: str) -> Tuple[int, int, int]:
    if not isinstance(color, str) or not color.startswith("#"):
        raise ValueError("unsupported color: {!r}".format(color))
    value = color[1:]
    if len(value) == 3:
        value = "".join(component * 2 for component in value)
    if len(value) != 6:
        raise ValueError("unsupported color: {!r}".format(color))
    try:
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore
    except ValueError as error:
        raise ValueError("unsupported color: {!r}".format(color)) from error


def rgb_to_xterm(rgb: Tuple[int, int, int]) -> int:
    r, g, b = rgb
    cube = tuple(round(component / 255 * 5) for component in rgb)
    cube_rgb = tuple(0 if value == 0 else 55 + value * 40 for value in cube)
    cube_distance = sum((left - right) ** 2 for left, right in zip(rgb, cube_rgb))

    gray_index = min(23, max(0, round(((r + g + b) / 3 - 8) / 10)))
    gray_value = 8 + gray_index * 10
    gray_distance = sum((component - gray_value) ** 2 for component in rgb)

    if gray_distance < cube_distance:
        return 232 + gray_index
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


def xterm_color(color: str) -> int:
    return rgb_to_xterm(hex_to_rgb(color))


def point(x: float, y: float) -> Dict[str, float]:
    return {"x": x, "y": y}

