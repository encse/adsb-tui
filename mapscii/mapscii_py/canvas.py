import math
from typing import Iterable, List, Optional, Sequence, Tuple


BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)
RESET = "\x1b[39;49m"


class BrailleBuffer:
    def __init__(self, width: int, height: int, use_braille: bool = True) -> None:
        self.width = max(2, width - width % 2)
        self.height = max(4, height - height % 4)
        self.columns = self.width // 2
        self.rows = self.height // 4
        self.use_braille = use_braille
        self.global_background: Optional[int] = None
        self.clear()

    def clear(self) -> None:
        size = self.columns * self.rows
        self.pixels = bytearray(size)
        self.characters: List[Optional[str]] = [None] * size
        self.foreground: List[Optional[int]] = [None] * size
        self.background: List[Optional[int]] = [None] * size

    def _index(self, x: int, y: int) -> int:
        return x // 2 + self.columns * (y // 4)

    def set_pixel(self, x: int, y: int, color: Optional[int]) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        index = self._index(x, y)
        self.pixels[index] |= BRAILLE_BITS[y & 3][x & 1]
        if color is not None:
            self.foreground[index] = color

    def set_character(self, character: str, x: int, y: int, color: Optional[int]) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        index = self._index(x, y)
        self.characters[index] = character
        if color is not None:
            self.foreground[index] = color

    def write_text(self, text: str, x: int, y: int, color: Optional[int], center: bool = False) -> None:
        if center:
            x -= len(text)
        for offset, character in enumerate(text):
            self.set_character(character, x + offset * 2, y, color)

    def set_background(self, color: Optional[int]) -> None:
        self.global_background = color

    def _color_sequence(self, foreground: Optional[int], background: Optional[int]) -> str:
        background = background if background is not None else self.global_background
        if foreground is not None and background is not None:
            return "\x1b[38;5;{};48;5;{}m".format(foreground, background)
        if foreground is not None:
            return "\x1b[49;38;5;{}m".format(foreground)
        if background is not None:
            return "\x1b[39;48;5;{}m".format(background)
        return RESET

    def frame(self) -> str:
        output: List[str] = []
        current_color: Optional[str] = None
        for row in range(self.rows):
            skip = 0
            for column in range(self.columns):
                index = row * self.columns + column
                color = self._color_sequence(self.foreground[index], self.background[index])
                if color != current_color:
                    output.append(color)
                    current_color = color

                character = self.characters[index]
                if character is not None:
                    output.append(character)
                    skip += max(0, len(character) - 1)
                elif skip:
                    skip -= 1
                elif self.use_braille:
                    output.append(chr(0x2800 + self.pixels[index]))
                else:
                    output.append(self._block_character(self.pixels[index]))
            output.append(RESET + "\n")
        return "".join(output)

    @staticmethod
    def _block_character(mask: int) -> str:
        if mask == 0:
            return " "
        upper = mask & (0x01 | 0x02 | 0x08 | 0x10)
        lower = mask & (0x04 | 0x40 | 0x20 | 0x80)
        if upper and lower:
            return "█"
        if upper:
            return "▀"
        return "▄"


class Canvas:
    def __init__(self, width: int, height: int, use_braille: bool = True) -> None:
        self.width = width
        self.height = height
        self.buffer = BrailleBuffer(width, height, use_braille)

    def clear(self) -> None:
        self.buffer.clear()

    def frame(self) -> str:
        return self.buffer.frame()

    def set_background(self, color: Optional[int]) -> None:
        self.buffer.set_background(color)

    def text(self, text: str, x: int, y: int, color: Optional[int], center: bool = False) -> None:
        self.buffer.write_text(text, x, y, color, center)

    def polyline(self, points: Sequence[Tuple[int, int]], color: Optional[int], width: int = 1) -> None:
        for start, end in zip(points, points[1:]):
            self.line(start, end, color, width)

    def line(self, start: Tuple[int, int], end: Tuple[int, int], color: Optional[int], width: int = 1) -> None:
        x0, y0 = start
        x1, y1 = end
        radius = max(0, int(round(width)) - 1) // 2
        for x, y in self._bresenham(x0, y0, x1, y1):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    self.buffer.set_pixel(x + dx, y + dy, color)

    def polygon(self, rings: Sequence[Sequence[Tuple[int, int]]], color: Optional[int]) -> None:
        edges = []
        for ring in rings:
            if len(ring) < 3:
                continue
            closed = list(ring)
            if closed[0] != closed[-1]:
                closed.append(closed[0])
            edges.extend(zip(closed, closed[1:]))

        for y in range(self.height):
            intersections: List[float] = []
            scan_y = y + 0.5
            for (x1, y1), (x2, y2) in edges:
                if y1 == y2:
                    continue
                if min(y1, y2) <= scan_y < max(y1, y2):
                    intersections.append(x1 + (scan_y - y1) * (x2 - x1) / (y2 - y1))
            intersections.sort()
            for left, right in zip(intersections[0::2], intersections[1::2]):
                for x in range(max(0, math.ceil(left)), min(self.width - 1, math.floor(right)) + 1):
                    self.buffer.set_pixel(x, y, color)

    @staticmethod
    def _bresenham(x0: int, y0: int, x1: int, y1: int) -> Iterable[Tuple[int, int]]:
        dx = abs(x1 - x0)
        sx = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0)
        sy = 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            yield x0, y0
            if x0 == x1 and y0 == y1:
                return
            twice_error = 2 * error
            if twice_error >= dy:
                error += dy
                x0 += sx
            if twice_error <= dx:
                error += dx
                y0 += sy

