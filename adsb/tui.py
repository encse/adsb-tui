from __future__ import annotations

import atexit
import math
import select
import termios
import threading
import time
import tty

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from mapscii_py.rich_map import MapMarker, MapView

from .constants import DEFAULT_MAP_ZOOM
from .sdr import GainSetting
from .tracking import AircraftState, AircraftTracker


class ModalOverlay:
    """Render a centered modal on top of an existing Rich renderable."""

    def __init__(self, background, dialog, *, width: int) -> None:
        self.background = background
        self.dialog = dialog
        self.width = width

    def __rich_console__(self, console, options):
        canvas_width = options.max_width
        canvas_height = options.max_height
        dialog_width = min(self.width, canvas_width)

        background_lines = console.render_lines(
            self.background,
            options.update(
                width=canvas_width,
                height=canvas_height,
            ),
            pad=True,
        )
        dialog_lines = console.render_lines(
            self.dialog,
            options.update(
                width=dialog_width,
                height=None,
            ),
            pad=True,
        )

        left = max(0, (canvas_width - dialog_width) // 2)
        top = max(0, (canvas_height - len(dialog_lines)) // 2)

        for row, background_line in enumerate(background_lines):
            dialog_row = row - top
            if 0 <= dialog_row < len(dialog_lines):
                parts = list(
                    Segment.divide(
                        background_line,
                        (left, left + dialog_width, canvas_width),
                    )
                )
                yield from parts[0]
                yield from dialog_lines[dialog_row]
                yield from parts[2]
            else:
                yield from background_line
            yield Segment.line()

class ScrollController:
    def __init__(
        self,
        *,
        map_visible: bool = True,
        gain_names: tuple[str, ...] = (),
    ) -> None:
        self.offset = 0
        self.map_visible = map_visible
        self.list_visible = True
        self.stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._tty = None
        self._old_settings = None
        self.gain_names = gain_names
        self.gain_dialog_visible = False
        self.gain_selection = 0
        self._gain_actions: list[tuple[str, int]] = []

    def start(self) -> None:
        try:
            self._tty = open("/dev/tty", "rb", buffering=0)
            self._old_settings = termios.tcgetattr(
                self._tty.fileno()
            )
            tty.setcbreak(self._tty.fileno())
            atexit.register(self.close)

            threading.Thread(
                target=self._read_loop,
                daemon=True,
            ).start()
        except OSError:
            self._tty = None
            self._old_settings = None

    def close(self) -> None:
        if (
            self._tty is not None
            and self._old_settings is not None
        ):
            try:
                termios.tcsetattr(
                    self._tty.fileno(),
                    termios.TCSADRAIN,
                    self._old_settings,
                )
            except (OSError, termios.error):
                pass

        if self._tty is not None:
            try:
                self._tty.close()
            except OSError:
                pass

        self._tty = None
        self._old_settings = None

    def get_offset(self) -> int:
        with self._lock:
            return self.offset

    def set_offset(self, value: int) -> None:
        with self._lock:
            self.offset = max(0, value)

    def move(self, delta: int) -> None:
        with self._lock:
            self.offset = max(0, self.offset + delta)

    def clamp(self, total: int, page_size: int) -> None:
        maximum = max(0, total - page_size)

        with self._lock:
            self.offset = min(self.offset, maximum)

    def visibility(self) -> tuple[bool, bool]:
        with self._lock:
            return self.map_visible, self.list_visible

    def toggle_map(self) -> None:
        with self._lock:
            self.map_visible = not self.map_visible

    def toggle_list(self) -> None:
        with self._lock:
            self.list_visible = not self.list_visible

    def gain_dialog_state(self) -> tuple[bool, int]:
        with self._lock:
            return self.gain_dialog_visible, self.gain_selection

    def pop_gain_actions(self) -> list[tuple[str, int]]:
        with self._lock:
            actions = self._gain_actions
            self._gain_actions = []
            return actions

    def _read_loop(self) -> None:
        assert self._tty is not None

        while not self.stop_requested.is_set():
            readable, _, _ = select.select(
                [self._tty],
                [],
                [],
                0.1,
            )

            if not readable:
                continue

            key = self._tty.read(1)

            if key == b"\x1b":
                time.sleep(0.005)

                while True:
                    readable, _, _ = select.select(
                        [self._tty],
                        [],
                        [],
                        0,
                    )

                    if not readable:
                        break

                    key += self._tty.read(1)

            with self._lock:
                gain_dialog_visible = self.gain_dialog_visible

            if gain_dialog_visible:
                with self._lock:
                    if key in (b"g", b"G", b"\x1b"):
                        self.gain_dialog_visible = False
                    elif key in (b"j", b"\x1b[B"):
                        self.gain_selection = min(
                            len(self.gain_names) - 1,
                            self.gain_selection + 1,
                        )
                    elif key in (b"k", b"\x1b[A"):
                        self.gain_selection = max(
                            0,
                            self.gain_selection - 1,
                        )
                    elif key in (b"h", b"-", b"\x1b[D"):
                        self._gain_actions.append(
                            (self.gain_names[self.gain_selection], -1)
                        )
                    elif key in (b"l", b"+", b"=", b"\x1b[C"):
                        self._gain_actions.append(
                            (self.gain_names[self.gain_selection], 1)
                        )
                continue

            if key in (b"q", b"Q"):
                self.stop_requested.set()
            elif key in (b"g", b"G") and self.gain_names:
                with self._lock:
                    self.gain_dialog_visible = True
            elif key in (b"m", b"M"):
                self.toggle_map()
            elif key in (b"l", b"L"):
                self.toggle_list()
            elif key in (b"j", b"\x1b[B"):
                self.move(1)
            elif key in (b"k", b"\x1b[A"):
                self.move(-1)
            elif key in (b" ", b"\x1b[6~"):
                self.move(5)
            elif key in (b"b", b"\x1b[5~"):
                self.move(-5)
            elif key in (b"\x1b[H", b"\x1b[1~"):
                self.set_offset(0)
            elif key in (b"G", b"\x1b[F", b"\x1b[4~"):
                self.set_offset(1_000_000_000)


class AdsbTui:
    AIRCRAFT_INACTIVE_SECONDS = 10.0
    AIRCRAFT_PANEL_HEIGHT = 7
    HEADER_HEIGHT = 4
    FOOTER_HEIGHT = 1
    MAP_BORDER_HEIGHT = 2
    MINIMUM_MAP_HEIGHT = 2
    MINIMUM_RECENTER_DISTANCE_KM = 2.0
    RECENTER_RADIUS_FRACTION = 0.15

    def __init__(
        self,
        console: Console,
        stale_seconds: float,
        list_size: int,
        scroll: ScrollController,
        map_view: MapView | None,
    ) -> None:
        self.console = console
        self.stale_seconds = stale_seconds
        self.configured_list_size = list_size
        self.scroll = scroll
        self.map_view = map_view

    @staticmethod
    def marker_center(
        markers: list[MapMarker],
    ) -> tuple[float, float]:
        """Return the center of the smallest box containing the markers."""
        latitudes = [marker.latitude for marker in markers]
        longitudes = sorted(
            marker.longitude % 360.0 for marker in markers
        )

        latitude = (min(latitudes) + max(latitudes)) / 2.0

        if len(longitudes) == 1:
            longitude = longitudes[0]
        else:
            gaps = [
                longitudes[index + 1] - longitudes[index]
                for index in range(len(longitudes) - 1)
            ]
            gaps.append(longitudes[0] + 360.0 - longitudes[-1])
            largest_gap_index = max(
                range(len(gaps)),
                key=gaps.__getitem__,
            )
            arc_start = longitudes[
                (largest_gap_index + 1) % len(longitudes)
            ]
            arc_length = 360.0 - gaps[largest_gap_index]
            longitude = (arc_start + arc_length / 2.0) % 360.0

        if longitude >= 180.0:
            longitude -= 360.0

        return latitude, longitude

    @staticmethod
    def distance_km(
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> float:
        first_latitude, first_longitude = map(math.radians, first)
        second_latitude, second_longitude = map(math.radians, second)
        latitude_delta = second_latitude - first_latitude
        longitude_delta = second_longitude - first_longitude
        longitude_delta = (
            longitude_delta + math.pi
        ) % (2.0 * math.pi) - math.pi
        haversine = (
            math.sin(latitude_delta / 2.0) ** 2
            + math.cos(first_latitude)
            * math.cos(second_latitude)
            * math.sin(longitude_delta / 2.0) ** 2
        )
        return 6371.0 * 2.0 * math.asin(
            min(1.0, math.sqrt(haversine))
        )

    def should_recenter_map(
        self,
        markers: list[MapMarker],
        target: tuple[float, float],
    ) -> bool:
        assert self.map_view is not None
        current = (self.map_view.latitude, self.map_view.longitude)
        center_distance = self.distance_km(current, target)
        marker_radius = max(
            self.distance_km(
                target,
                (marker.latitude, marker.longitude),
            )
            for marker in markers
        )
        threshold = max(
            self.MINIMUM_RECENTER_DISTANCE_KM,
            marker_radius * self.RECENTER_RADIUS_FRACTION,
        )
        return center_distance >= threshold

    def fit_map_to_console(
        self,
        map_visible: bool,
        list_visible: bool,
    ) -> None:
        if self.map_view is None or not map_visible:
            return

        if not list_visible:
            reserved_lines = (
                self.HEADER_HEIGHT
                + self.FOOTER_HEIGHT
                + self.MAP_BORDER_HEIGHT
            )
            map_height = max(
                self.MINIMUM_MAP_HEIGHT,
                self.console.size.height - reserved_lines,
            )
            self.map_view.set_height(map_height)
            return

        reserved_lines = (
            self.HEADER_HEIGHT
            + self.FOOTER_HEIGHT
            + self.MAP_BORDER_HEIGHT
            + self.configured_list_size * self.AIRCRAFT_PANEL_HEIGHT
        )
        map_height = max(
            self.MINIMUM_MAP_HEIGHT,
            self.console.size.height - reserved_lines,
        )
        self.map_view.set_height(map_height)

    def page_size(self, map_visible: bool) -> int:
        if map_visible:
            return self.configured_list_size

        available_lines = max(
            0,
            self.console.size.height
            - self.HEADER_HEIGHT
            - self.FOOTER_HEIGHT,
        )
        return max(
            1,
            available_lines // self.AIRCRAFT_PANEL_HEIGHT,
        )

    @staticmethod
    def format_number(
        value: int | float | None,
        suffix: str,
        decimals: int = 0,
    ) -> str:
        if value is None:
            return "—"

        return f"{value:,.{decimals}f}{suffix}"

    @staticmethod
    def signal_style(confidence: float) -> str:
        if confidence >= 0.65:
            return "bold green"
        if confidence >= 0.45:
            return "bold yellow"

        return "bold red"

    @staticmethod
    def render_scrollbar(
        total: int,
        page_size: int,
        offset: int,
        height: int,
    ) -> Text:
        if total <= page_size or height <= 0:
            return Text("\n".join(" " for _ in range(height)))

        thumb_height = max(
            1,
            round(height * page_size / total),
        )
        thumb_height = min(height, thumb_height)

        maximum_offset = total - page_size
        maximum_thumb_offset = height - thumb_height
        thumb_offset = round(
            maximum_thumb_offset * offset / maximum_offset
        )

        lines = []

        for row in range(height):
            if thumb_offset <= row < thumb_offset + thumb_height:
                lines.append("█")
            else:
                lines.append("│")

        return Text(
            "\n".join(lines),
            style="bright_black",
            no_wrap=True,
        )

    def render_aircraft(
        self,
        state: AircraftState,
        latest_timestamp_seconds: float,
    ) -> Panel:
        age = max(
            0.0,
            latest_timestamp_seconds - state.last_seen_seconds,
        )

        callsign = state.callsign or "UNKNOWN"
        title = Text()
        title.append("✈ ", style="bold cyan")
        title.append(callsign, style="bold white")
        title.append("  ")
        title.append(state.icao, style="bold bright_black")

        speed = (
            state.ground_speed_kt
            if state.ground_speed_kt is not None
            else state.speed_kt
        )
        direction = (
            state.track_deg
            if state.track_deg is not None
            else state.heading_deg
        )
        direction_label = (
            "Track"
            if state.track_deg is not None
            else "Heading"
        )

        position = "—"

        if (
            state.latitude is not None
            and state.longitude is not None
        ):
            position = (
                f"{state.latitude:.5f}, "
                f"{state.longitude:.5f}"
            )
        elif state.cpr is not None:
            position = f"CPR {state.cpr}, waiting for pair"

        grid = Table.grid(
            expand=True,
            padding=(0, 1),
        )
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        grid.add_row(
            (
                "[bold cyan]Altitude[/]  "
                f"{self.format_number(state.altitude_ft, ' ft')}"
            ),
            (
                "[bold cyan]Speed[/]     "
                f"{self.format_number(speed, ' kt')}"
            ),
        )
        grid.add_row(
            (
                f"[bold cyan]{direction_label}[/]     "
                f"{self.format_number(direction, '°', 1)}"
            ),
            (
                "[bold cyan]Vertical[/]  "
                f"{self.format_number(state.vertical_rate_fpm, ' ft/min')}"
            ),
        )
        last_code = (
            f"TC {state.last_tc}"
            if state.last_tc is not None
            else state.source
        )

        grid.add_row(
            (
                "[bold cyan]Position[/]  "
                f"{position}"
            ),
            (
                "[bold cyan]Last data[/] "
                f"{last_code} · {state.last_type}"
            ),
        )

        grid.add_row(
            (
                "[bold cyan]Squawk[/]    "
                f"{state.squawk or '—'}"
            ),
            (
                "[bold cyan]Source[/]    "
                f"{state.source}"
            ),
        )

        signal_style = self.signal_style(state.confidence)

        grid.add_row(
            (
                "[dim]Frames[/] "
                f"{state.frame_count:,}  "
                "[dim]Age[/] "
                f"{age:.1f}s"
            ),
            (
                "[dim]Confidence[/] "
                f"[{signal_style}]"
                f"{state.confidence:.3f}"
                "[/]"
            ),
        )

        border_style = (
            "green"
            if age < 2.0
            else "yellow"
            if age < self.AIRCRAFT_INACTIVE_SECONDS
            else "bright_black"
        )

        return Panel(
            grid,
            title=title,
            title_align="left",
            border_style=border_style,
            padding=(0, 1),
        )

    def render(
        self,
        tracker: AircraftTracker,
        *,
        device_label: str,
        gain_summary: str,
        gain_settings: tuple[GainSetting, ...],
        input_sample_rate: int,
        total_input_samples: int,
        candidates: int,
        valid_frames: int,
        activity_db: float,
        sdr_errors: int,
        parser_errors: int,
    ) -> Group | ModalOverlay:
        map_visible, list_visible = self.scroll.visibility()
        self.fit_map_to_console(map_visible, list_visible)

        all_aircraft = tracker.active_aircraft(
            self.stale_seconds,
        )

        page_size = self.page_size(map_visible)
        self.scroll.clamp(
            len(all_aircraft),
            page_size,
        )
        scroll_offset = self.scroll.get_offset()

        aircraft = all_aircraft[
            scroll_offset:scroll_offset + page_size
        ]

        header = Table.grid(
            expand=True,
            padding=(0, 1),
        )
        header.add_column(ratio=1)
        header.add_column(ratio=1)
        header.add_column(ratio=1)

        duration_seconds = (
            total_input_samples / input_sample_rate
        )

        header.add_row(
            (
                f"[bold bright_cyan]{escape(device_label)}[/]\n"
                "[dim]1090 MHz · "
                f"{input_sample_rate / 1_000_000:g} MS/s · "
                f"{escape(gain_summary)}[/]"
            ),
            (
                f"[bold white]{len(all_aircraft)}[/] aircraft\n"
                f"[dim]{valid_frames:,} valid frames[/]"
            ),
            (
                f"[bold white]{duration_seconds:,.1f}s[/] captured\n"
                f"[dim]{candidates:,} candidates[/]"
            ),
        )

        activity_text = f"{activity_db:.1f} dB"

        header_panel = Panel(
            header,
            title="[bold cyan]✈ ADS-B AIRSPACE MONITOR[/]",
            subtitle=(
                f"[dim]signal {activity_text} · "
                f"SDR errors {sdr_errors} · "
                f"parser errors {parser_errors}[/]"
            ),
            border_style="bright_cyan",
            padding=(0, 1),
        )

        gain_dialog_visible, gain_selection = (
            self.scroll.gain_dialog_state()
        )
        dialog = None
        if gain_dialog_visible:
            gain_table = Table.grid(padding=(0, 2))
            gain_table.add_column(width=2)
            gain_table.add_column(min_width=8)
            gain_table.add_column(justify="right", min_width=9)
            gain_table.add_column(style="dim")
            for index, setting in enumerate(gain_settings):
                selected = index == gain_selection
                gain_table.add_row(
                    "[bold cyan]›[/]" if selected else "",
                    (
                        f"[bold]{escape(setting.name)}[/]"
                        if selected
                        else escape(setting.name)
                    ),
                    f"[bold white]{setting.value:g} dB[/]",
                    (
                        f"{setting.minimum:g}…{setting.maximum:g} dB"
                        f"  step {setting.step:g}"
                    ),
                )

            dialog = Panel(
                gain_table,
                title="[bold cyan]SDR GAIN[/]",
                subtitle=(
                    "[dim]↑/↓ select · ←/→ or −/+ adjust"
                    " · g/Esc close[/]"
                ),
                border_style="bright_cyan",
                padding=(1, 2),
            )

        map_panel = None

        if self.map_view is not None and map_visible:
            aircraft_markers = [
                MapMarker(
                    latitude=state.latitude,
                    longitude=state.longitude,
                    label=state.callsign or state.icao,
                    style=(
                        "bright_black"
                        if (
                            tracker.latest_timestamp_seconds
                            - state.last_seen_seconds
                            >= self.AIRCRAFT_INACTIVE_SECONDS
                        )
                        else "bold white"
                    ),
                )
                for state in all_aircraft
                if (
                    state.latitude is not None
                    and state.longitude is not None
                )
            ]

            if aircraft_markers:
                latitude, longitude = self.marker_center(
                    aircraft_markers
                )
                target_center = (latitude, longitude)
                if self.should_recenter_map(
                    aircraft_markers,
                    target_center,
                ):
                    self.map_view.set_center(*target_center)
                self.map_view.set_zoom(DEFAULT_MAP_ZOOM)

            self.map_view.set_markers(aircraft_markers)
            map_panel = self.map_view.panel(
                title=(
                    "[bold cyan]ADS-B MAP[/] "
                    f"[dim]{len(aircraft_markers)} positioned[/]"
                ),
            )

        panels = [
            self.render_aircraft(
                state,
                tracker.latest_timestamp_seconds,
            )
            for state in aircraft
        ]

        if not panels:
            panels.append(
                Panel(
                    Text(
                        "Waiting for CRC-valid ADS-B frames…",
                        justify="center",
                        style="dim",
                    ),
                    border_style="bright_black",
                )
            )

        scrollbar_height = (
            len(panels) * self.AIRCRAFT_PANEL_HEIGHT
        )
        body = Table.grid(expand=True, padding=0)
        body.add_column(ratio=1)
        body.add_column(width=1, no_wrap=True)
        body.add_row(
            Group(*panels),
            self.render_scrollbar(
                len(all_aircraft),
                page_size,
                scroll_offset,
                scrollbar_height,
            ),
        )

        if list_visible:
            if all_aircraft:
                range_text = (
                    f"showing {scroll_offset + 1}–"
                    f"{min(scroll_offset + len(aircraft), len(all_aircraft))}"
                    f" of {len(all_aircraft)}"
                )
            else:
                range_text = "0 aircraft"

            footer_text = (
                f" {range_text}  ·  ↑/k ↓/j scroll"
                "  ·  PgUp/b PgDn/Space page  ·  Home/End top/bottom"
                "  ·  g gain  ·  m map  ·  l list  ·  q stop"
            )
        else:
            footer_text = (
                " g gain  ·  m map  ·  l list  ·  q or Ctrl+C stop"
            )

        footer = Text(footer_text, style="dim")

        renderables = [header_panel]

        if map_panel is not None:
            renderables.append(map_panel)

        if list_visible:
            renderables.append(body)

        renderables.append(footer)
        background = Group(*renderables)
        if dialog is not None:
            return ModalOverlay(background, dialog, width=68)
        return background
