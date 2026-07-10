#!/usr/bin/env python3

from __future__ import annotations

import argparse
import atexit
import select
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import numpy as np
import pyModeS as pms
from scipy.signal import firwin, lfilter
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if __package__:
    from .mapscii.mapscii_py.rich_map import (
        DEFAULT_SOURCE as DEFAULT_MAP_SOURCE,
        DEFAULT_STYLE as DEFAULT_MAP_STYLE,
        MapMarker,
        MapView,
    )
else:
    from mapscii.mapscii_py.rich_map import (
        DEFAULT_SOURCE as DEFAULT_MAP_SOURCE,
        DEFAULT_STYLE as DEFAULT_MAP_STYLE,
        MapMarker,
        MapView,
    )


DEFAULT_INPUT_SAMPLE_RATE = 3_000_000
SAMPLE_RATE = 6_000_000

DEFAULT_RECEIVER_LATITUDE = 47.4979
DEFAULT_RECEIVER_LONGITUDE = 19.0402
DEFAULT_MAP_ZOOM = 9.0

INTERPOLATOR_TAPS = 63
MAX_INTERPOLATOR_CUTOFF_HZ = 1_350_000
INPUT_FORMATS = ("cf32", "cu8")

SHORT_FRAME_BITS = 56
LONG_FRAME_BITS = 112
HALF_BIT_SAMPLES = 3
BIT_SAMPLES = 6

SHORT_DFS = {0, 4, 5, 11}
LONG_DFS = {16, 17, 18, 19, 20, 21, 24}
SUPPORTED_DFS = SHORT_DFS | LONG_DFS

PREAMBLE_SAMPLES = 48
MAX_FRAME_DATA_SAMPLES = LONG_FRAME_BITS * BIT_SAMPLES
REQUIRED_SAMPLES = PREAMBLE_SAMPLES + MAX_FRAME_DATA_SAMPLES

CRC_POLYNOMIAL = 0xFFF409

CHUNK_SAMPLES = 300_000
NOISE_TIME_CONSTANT_SECONDS = 0.5
MIN_NOISE_LEVEL = 1e-12

# Four 0.5 us pulses in the 8 us Mode-S preamble at 6 MS/s.
PREAMBLE_PULSE_RANGES = (
    (0, 3),
    (6, 9),
    (21, 24),
    (27, 30),
)


class StreamingInterpolator:
    def __init__(self, input_sample_rate: int) -> None:
        if input_sample_rate <= 0:
            raise ValueError(
                "input sample rate must be positive"
            )
        if SAMPLE_RATE % input_sample_rate != 0:
            raise ValueError(
                "input sample rate must divide "
                f"{SAMPLE_RATE} exactly"
            )

        self.interpolation = (
            SAMPLE_RATE // input_sample_rate
        )
        self.input_sample_rate = input_sample_rate

        if self.interpolation == 1:
            self.taps = np.array([1.0])
            self.state = np.empty(
                0,
                dtype=np.complex128,
            )
            self.delay_samples = 0
            return

        cutoff_hz = min(
            MAX_INTERPOLATOR_CUTOFF_HZ,
            input_sample_rate * 0.45,
        )
        self.taps = firwin(
            INTERPOLATOR_TAPS,
            cutoff=cutoff_hz,
            fs=SAMPLE_RATE,
        )

        # Compensate for zero insertion during interpolation.
        self.taps *= self.interpolation

        self.state = np.zeros(
            len(self.taps) - 1,
            dtype=np.complex128,
        )

        self.delay_samples = (len(self.taps) - 1) // 2

    def process(self, samples: np.ndarray) -> np.ndarray:
        if samples.size == 0:
            return np.empty(0, dtype=np.complex64)

        if self.interpolation == 1:
            return samples.astype(
                np.complex64,
                copy=False,
            )

        upsampled = np.zeros(
            samples.size * self.interpolation,
            dtype=np.complex64,
        )
        upsampled[0::self.interpolation] = samples

        filtered, self.state = lfilter(
            self.taps,
            [1.0],
            upsampled,
            zi=self.state,
        )

        return filtered.astype(np.complex64, copy=False)


class SignalLevelTracker:
    def __init__(
        self,
        sample_rate: float,
        time_constant_seconds: float,
    ) -> None:
        self.sample_rate = sample_rate
        self.time_constant_seconds = time_constant_seconds

        self.baseline: float | None = None
        self.noise_level: float | None = None

    def update(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return

        measured_baseline = float(np.median(samples))
        measured_noise = float(
            np.median(np.abs(samples - measured_baseline))
        )
        measured_noise = max(measured_noise, MIN_NOISE_LEVEL)

        if self.baseline is None or self.noise_level is None:
            self.baseline = measured_baseline
            self.noise_level = measured_noise
            return

        chunk_duration = samples.size / self.sample_rate
        alpha = 1.0 - np.exp(
            -chunk_duration / self.time_constant_seconds
        )

        self.baseline += alpha * (
            measured_baseline - self.baseline
        )
        self.noise_level += alpha * (
            measured_noise - self.noise_level
        )
        self.noise_level = max(
            self.noise_level,
            MIN_NOISE_LEVEL,
        )


@dataclass
class PositionFrame:
    message: str
    timestamp_seconds: float


@dataclass
class AircraftState:
    icao: str
    callsign: str | None = None
    category: int | None = None
    altitude_ft: int | float | None = None
    speed_kt: int | float | None = None
    ground_speed_kt: int | float | None = None
    track_deg: float | None = None
    heading_deg: float | None = None
    vertical_rate_fpm: int | float | None = None
    latitude: float | None = None
    longitude: float | None = None
    cpr: str | None = None
    squawk: str | None = None
    source: str = "unknown"
    last_type: str = "unknown"
    last_tc: int | None = None
    last_seen_seconds: float = 0.0
    first_seen_seconds: float = 0.0
    frame_count: int = 0
    confidence: float = 0.0

    def update(
        self,
        decoded: dict[str, Any],
        timestamp_seconds: float,
        confidence: float,
    ) -> None:
        if self.frame_count == 0:
            self.first_seen_seconds = timestamp_seconds

        self.last_seen_seconds = timestamp_seconds
        self.frame_count += 1
        self.confidence = confidence
        self.last_type = decoded.get("type", self.last_type)
        self.last_tc = decoded.get("tc", self.last_tc)

        for field_name in (
            "callsign",
            "category",
            "altitude_ft",
            "speed_kt",
            "ground_speed_kt",
            "track_deg",
            "heading_deg",
            "vertical_rate_fpm",
            "latitude",
            "longitude",
            "cpr",
            "squawk",
            "source",
        ):
            value = decoded.get(field_name)

            if value is not None:
                setattr(self, field_name, value)


class AircraftTracker:
    def __init__(self) -> None:
        self.aircraft: dict[str, AircraftState] = {}
        self.latest_timestamp_seconds = 0.0

    def update(
        self,
        decoded: dict[str, Any],
        timestamp_seconds: float,
        confidence: float,
    ) -> None:
        icao = decoded.get("icao")

        if not icao:
            return

        self.latest_timestamp_seconds = max(
            self.latest_timestamp_seconds,
            timestamp_seconds,
        )

        state = self.aircraft.get(icao)

        if state is None:
            state = AircraftState(icao=icao)
            self.aircraft[icao] = state

        state.update(
            decoded,
            timestamp_seconds,
            confidence,
        )

    def active_aircraft(
        self,
        stale_seconds: float,
    ) -> list[AircraftState]:
        active = [
            state
            for state in self.aircraft.values()
            if (
                self.latest_timestamp_seconds
                - state.last_seen_seconds
                <= stale_seconds
            )
        ]

        active.sort(key=lambda state: state.icao)
        return active


class ScrollController:
    def __init__(self) -> None:
        self.offset = 0
        self.stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._tty = None
        self._old_settings = None

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

            if key in (b"q", b"Q"):
                self.stop_requested.set()
            elif key in (b"j", b"\x1b[B"):
                self.move(1)
            elif key in (b"k", b"\x1b[A"):
                self.move(-1)
            elif key in (b" ", b"\x1b[6~"):
                self.move(5)
            elif key in (b"b", b"\x1b[5~"):
                self.move(-5)
            elif key in (b"g", b"\x1b[H", b"\x1b[1~"):
                self.set_offset(0)
            elif key in (b"G", b"\x1b[F", b"\x1b[4~"):
                self.set_offset(1_000_000_000)


class AdsbTui:
    AIRCRAFT_PANEL_HEIGHT = 7
    DEFAULT_MAP_PAGE_SIZE = 2
    HEADER_HEIGHT = 4
    FOOTER_HEIGHT = 1
    MAP_BORDER_HEIGHT = 2
    MINIMUM_MAP_HEIGHT = 2

    def __init__(
        self,
        console: Console,
        stale_seconds: float,
        page_size: int,
        scroll: ScrollController,
        map_view: MapView | None,
        auto_map_height: bool,
    ) -> None:
        self.console = console
        self.stale_seconds = stale_seconds
        self.configured_page_size = page_size
        self.scroll = scroll
        self.map_view = map_view
        self.auto_map_height = auto_map_height
        self.map_initialized_from_aircraft = False

    def fit_map_to_console(self) -> None:
        if (
            self.map_view is None
            or not self.auto_map_height
        ):
            return

        aircraft_panels = (
            self.configured_page_size
            if self.configured_page_size > 0
            else self.DEFAULT_MAP_PAGE_SIZE
        )
        reserved_lines = (
            self.HEADER_HEIGHT
            + self.FOOTER_HEIGHT
            + self.MAP_BORDER_HEIGHT
            + aircraft_panels * self.AIRCRAFT_PANEL_HEIGHT
        )
        map_height = max(
            self.MINIMUM_MAP_HEIGHT,
            self.console.size.height - reserved_lines,
        )
        self.map_view.set_height(map_height)

    def page_size(self) -> int:
        if self.configured_page_size > 0:
            return self.configured_page_size

        if self.map_view is not None:
            return self.DEFAULT_MAP_PAGE_SIZE

        map_lines = (
            self.map_view.height + 2
            if self.map_view is not None
            else 0
        )
        available_lines = max(
            0,
            self.console.size.height - 8 - map_lines,
        )

        return max(1, available_lines // 8)

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
            if age < 10.0
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
        input_sample_rate: int,
        total_input_samples: int,
        candidates: int,
        valid_frames: int,
        baseline: float | None,
        noise_level: float | None,
        parser_errors: int,
    ) -> Group:
        self.fit_map_to_console()

        all_aircraft = tracker.active_aircraft(
            self.stale_seconds,
        )

        page_size = self.page_size()
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
                "[bold bright_cyan]SDR ADS-B[/]\n"
                "[dim]1090 MHz · "
                f"{input_sample_rate / 1_000_000:g} "
                "→ 6 MS/s[/]"
            ),
            (
                (
                    f"[bold white]{len(all_aircraft)}[/] aircraft\n"
                    f"[dim]showing "
                    f"{scroll_offset + 1 if all_aircraft else 0}"
                    f"–{min(scroll_offset + len(aircraft), len(all_aircraft))}[/]"
                )
            ),
            (
                f"[bold white]{duration_seconds:,.1f}s[/] captured\n"
                f"[dim]{candidates:,} candidates[/]"
            ),
        )

        baseline_text = (
            "—" if baseline is None else f"{baseline:.7f}"
        )
        noise_text = (
            "—" if noise_level is None else f"{noise_level:.7f}"
        )

        header_panel = Panel(
            header,
            title="[bold cyan]✈ ADS-B AIRSPACE MONITOR[/]",
            subtitle=(
                f"[dim]baseline {baseline_text} · "
                f"noise {noise_text} · "
                f"parser errors {parser_errors}[/]"
            ),
            border_style="bright_cyan",
            padding=(0, 1),
        )

        map_panel = None

        if self.map_view is not None:
            aircraft_markers = [
                MapMarker(
                    latitude=state.latitude,
                    longitude=state.longitude,
                    label=state.callsign or state.icao,
                    style="bold white",
                )
                for state in all_aircraft
                if (
                    state.latitude is not None
                    and state.longitude is not None
                )
            ]

            if (
                aircraft_markers
                and not self.map_initialized_from_aircraft
            ):
                first_marker = aircraft_markers[0]
                self.map_view.latitude = first_marker.latitude
                self.map_view.longitude = first_marker.longitude
                self.map_view.zoom = DEFAULT_MAP_ZOOM
                self.map_initialized_from_aircraft = True

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

        footer = Text(
            " ↑/k ↓/j scroll  ·  PgUp/b PgDn/Space page"
            "  ·  g/G top/bottom  ·  q or Ctrl+C stop",
            style="dim",
        )

        renderables = [header_panel]

        if map_panel is not None:
            renderables.append(map_panel)

        renderables.extend((body, footer))
        return Group(*renderables)


@dataclass
class AddressCandidate:
    count: int
    last_seen_seconds: float


class ModeSAddressValidator:
    def __init__(
        self,
        promotion_hits: int = 2,
        candidate_window_seconds: float = 10.0,
        duplicate_window_seconds: float = 0.001,
    ) -> None:
        self.promotion_hits = promotion_hits
        self.candidate_window_seconds = candidate_window_seconds
        self.duplicate_window_seconds = duplicate_window_seconds

        self.known_addresses: set[str] = set()
        self.candidates: dict[str, AddressCandidate] = {}

    def trust(self, icao: str) -> None:
        self.known_addresses.add(icao)
        self.candidates.pop(icao, None)

    def accept_recovered(
        self,
        icao: str,
        timestamp_seconds: float,
    ) -> bool:
        if icao in self.known_addresses:
            return True

        previous = self.candidates.get(icao)

        if previous is None:
            self.candidates[icao] = AddressCandidate(
                count=1,
                last_seen_seconds=timestamp_seconds,
            )
            return False

        age = timestamp_seconds - previous.last_seen_seconds

        if age < self.duplicate_window_seconds:
            return False

        if age > self.candidate_window_seconds:
            previous.count = 1
            previous.last_seen_seconds = timestamp_seconds
            return False

        previous.count += 1
        previous.last_seen_seconds = timestamp_seconds

        if previous.count < self.promotion_hits:
            return False

        self.trust(icao)
        return True


class AdsbFrameParser:
    def __init__(self) -> None:
        self.even_positions: dict[str, PositionFrame] = {}
        self.odd_positions: dict[str, PositionFrame] = {}

    def parse(
        self,
        frame: bytes,
        timestamp_seconds: float,
    ) -> dict[str, Any]:
        message = frame.hex().upper()
        downlink_format = pms.df(message)
        icao = pms.icao(message)

        decoded: dict[str, Any] = {
            "frame": message,
            "df": downlink_format,
            "icao": icao,
            "source": "ADS-B" if downlink_format in (17, 18) else "Mode-S",
        }

        if downlink_format in (17, 18):
            type_code = pms.adsb.typecode(message)

            decoded["tc"] = type_code
            decoded["type"] = self.type_name(type_code)

            if 1 <= type_code <= 4:
                self.parse_identification(message, decoded)
            elif 5 <= type_code <= 8:
                self.parse_surface_position(message, decoded)
            elif 9 <= type_code <= 18:
                self.parse_airborne_position(
                    message,
                    timestamp_seconds,
                    decoded,
                )
            elif type_code == 19:
                self.parse_velocity(message, decoded)
            elif 20 <= type_code <= 22:
                self.parse_airborne_position(
                    message,
                    timestamp_seconds,
                    decoded,
                )

            return decoded

        decoded["type"] = self.mode_s_type_name(downlink_format)

        if downlink_format in (0, 4, 16, 20):
            try:
                altitude = pms.altcode(message)
            except Exception:
                altitude = None

            if altitude is not None:
                decoded["altitude_ft"] = altitude

        if downlink_format in (5, 21):
            try:
                squawk = pms.idcode(message)
            except Exception:
                squawk = None

            if squawk is not None:
                decoded["squawk"] = squawk

        if downlink_format in (20, 21):
            self.parse_comm_b(message, decoded)

        return decoded

    @staticmethod
    def mode_s_type_name(downlink_format: int) -> str:
        names = {
            0: "ACAS short air-air surveillance",
            4: "surveillance altitude reply",
            5: "surveillance identity reply",
            11: "all-call reply",
            16: "ACAS long air-air surveillance",
            20: "Comm-B altitude reply",
            21: "Comm-B identity reply",
            24: "Comm-D extended message",
        }

        return names.get(
            downlink_format,
            f"Mode-S DF{downlink_format}",
        )

    @staticmethod
    def parse_comm_b(
        message: str,
        decoded: dict[str, Any],
    ) -> None:
        commb = getattr(pms, "commb", None)

        if commb is None:
            return

        try:
            is_bds20 = commb.is20(message)
        except Exception:
            is_bds20 = False

        if not is_bds20:
            return

        try:
            callsign = commb.cs20(message)
        except Exception:
            callsign = None

        if callsign:
            decoded["callsign"] = callsign.rstrip("_ ")

    @staticmethod
    def type_name(type_code: int) -> str:
        if 1 <= type_code <= 4:
            return "aircraft identification"
        if 5 <= type_code <= 8:
            return "surface position"
        if 9 <= type_code <= 18:
            return "airborne position, barometric altitude"
        if type_code == 19:
            return "airborne velocity"
        if 20 <= type_code <= 22:
            return "airborne position, GNSS altitude"
        if type_code == 28:
            return "aircraft status"
        if type_code == 29:
            return "target state and status"
        if type_code == 31:
            return "aircraft operational status"

        return "unknown"

    @staticmethod
    def parse_identification(
        message: str,
        decoded: dict[str, Any],
    ) -> None:
        callsign = pms.adsb.callsign(message)

        if callsign is not None:
            decoded["callsign"] = callsign.rstrip("_ ")

        category = pms.adsb.category(message)

        if category is not None:
            decoded["category"] = category

    @staticmethod
    def parse_surface_position(
        message: str,
        decoded: dict[str, Any],
    ) -> None:
        decoded["cpr"] = (
            "odd" if pms.adsb.oe_flag(message) else "even"
        )

        try:
            velocity = pms.adsb.surface_velocity(message)
        except Exception:
            velocity = None

        if velocity is None:
            return

        if len(velocity) >= 1 and velocity[0] is not None:
            decoded["ground_speed_kt"] = velocity[0]

        if len(velocity) >= 2 and velocity[1] is not None:
            decoded["track_deg"] = velocity[1]

    def parse_airborne_position(
        self,
        message: str,
        timestamp_seconds: float,
        decoded: dict[str, Any],
    ) -> None:
        icao = decoded["icao"]
        is_odd = bool(pms.adsb.oe_flag(message))

        decoded["cpr"] = "odd" if is_odd else "even"

        altitude = pms.adsb.altitude(message)

        if altitude is not None:
            decoded["altitude_ft"] = altitude

        current = PositionFrame(
            message=message,
            timestamp_seconds=timestamp_seconds,
        )

        if is_odd:
            self.odd_positions[icao] = current
        else:
            self.even_positions[icao] = current

        even = self.even_positions.get(icao)
        odd = self.odd_positions.get(icao)

        if even is None or odd is None:
            return

        if abs(
            even.timestamp_seconds - odd.timestamp_seconds
        ) > 10.0:
            return

        try:
            position = pms.adsb.position(
                even.message,
                odd.message,
                even.timestamp_seconds,
                odd.timestamp_seconds,
            )
        except Exception:
            return

        if position is None:
            return

        latitude, longitude = position

        decoded["latitude"] = latitude
        decoded["longitude"] = longitude

    @staticmethod
    def parse_velocity(
        message: str,
        decoded: dict[str, Any],
    ) -> None:
        try:
            velocity = pms.adsb.velocity(
                message,
                source=True,
            )
        except Exception:
            return

        if velocity is None:
            return

        if len(velocity) >= 1 and velocity[0] is not None:
            decoded["speed_kt"] = velocity[0]

        if len(velocity) >= 2 and velocity[1] is not None:
            angle = velocity[1]
            speed_type = velocity[3] if len(velocity) >= 4 else None

            if speed_type == "GS":
                decoded["track_deg"] = angle
            else:
                decoded["heading_deg"] = angle

        if len(velocity) >= 3 and velocity[2] is not None:
            decoded["vertical_rate_fpm"] = velocity[2]

        if len(velocity) >= 4 and velocity[3] is not None:
            decoded["speed_type"] = velocity[3]

        if len(velocity) >= 5 and velocity[4] is not None:
            decoded["direction_source"] = velocity[4]

        if len(velocity) >= 6 and velocity[5] is not None:
            decoded["vertical_rate_source"] = velocity[5]


def modes_crc(frame: bytes) -> int:
    value = int.from_bytes(frame, byteorder="big")
    bit_count = len(frame) * 8

    for bit_position in range(
        bit_count - 1,
        23,
        -1,
    ):
        if value & (1 << bit_position):
            value ^= (
                CRC_POLYNOMIAL
                << (bit_position - 24)
            )

    return value & 0xFFFFFF


def bits_to_bytes(bits: np.ndarray) -> bytes:
    return np.packbits(
        bits.astype(np.uint8)
    ).tobytes()


def decode_bits(
    signal: np.ndarray,
    data_start: int,
    baseline: float,
    frame_bits: int,
) -> tuple[np.ndarray, float]:
    frame_data_samples = frame_bits * BIT_SAMPLES
    end = data_start + frame_data_samples
    samples = signal[data_start:end]

    if samples.size != frame_data_samples:
        raise ValueError(
            "Not enough samples for a complete frame"
        )

    samples = np.maximum(
        samples - baseline,
        0.0,
    )

    symbols = samples.reshape(
        frame_bits,
        BIT_SAMPLES,
    )

    first_half_energy = np.sum(
        symbols[:, :HALF_BIT_SAMPLES],
        axis=1,
    )
    second_half_energy = np.sum(
        symbols[:, HALF_BIT_SAMPLES:],
        axis=1,
    )

    bits = (
        first_half_energy > second_half_energy
    ).astype(np.uint8)

    differences = np.abs(
        first_half_energy - second_half_energy
    )
    totals = (
        first_half_energy
        + second_half_energy
        + 1e-12
    )

    confidence = float(
        np.mean(differences / totals)
    )

    return bits, confidence


def decode_mode_s_candidate(
    signal: np.ndarray,
    data_start: int,
    baseline: float,
) -> tuple[bytes, int, float] | None:
    header_bits, _ = decode_bits(
        signal,
        data_start,
        baseline,
        SHORT_FRAME_BITS,
    )

    downlink_format = int(
        np.packbits(header_bits[:8])[0] >> 3
    )

    if downlink_format not in SUPPORTED_DFS:
        return None

    frame_bits = (
        SHORT_FRAME_BITS
        if downlink_format in SHORT_DFS
        else LONG_FRAME_BITS
    )

    bits, confidence = decode_bits(
        signal,
        data_start,
        baseline,
        frame_bits,
    )

    return bits_to_bytes(bits), downlink_format, confidence


def range_sums(
    prefix: np.ndarray,
    start: int,
    end: int,
    count: int,
) -> np.ndarray:
    return (
        prefix[end:end + count]
        - prefix[start:start + count]
    )


def find_preamble_candidates(
    signal: np.ndarray,
    baseline: float,
    noise_level: float,
) -> np.ndarray:
    candidate_count = (
        signal.size - REQUIRED_SAMPLES + 1
    )

    if candidate_count <= 0:
        return np.empty(0, dtype=np.int64)

    prefix = np.empty(
        signal.size + 1,
        dtype=np.float64,
    )
    prefix[0] = 0.0

    np.cumsum(
        signal,
        dtype=np.float64,
        out=prefix[1:],
    )

    pulse_sums = [
        range_sums(
            prefix,
            start,
            end,
            candidate_count,
        )
        for start, end in PREAMBLE_PULSE_RANGES
    ]

    combined_high_sum = np.zeros(
        candidate_count,
        dtype=np.float64,
    )

    for pulse_sum in pulse_sums:
        combined_high_sum += pulse_sum

    full_window_sum = range_sums(
        prefix,
        0,
        PREAMBLE_SAMPLES,
        candidate_count,
    )

    high_sample_count = sum(
        end - start
        for start, end in PREAMBLE_PULSE_RANGES
    )
    low_sample_count = (
        PREAMBLE_SAMPLES - high_sample_count
    )

    high_level = (
        combined_high_sum / high_sample_count
    )
    low_level = (
        full_window_sum - combined_high_sum
    ) / low_sample_count

    mask = (
        high_level
        > baseline + noise_level * 6.0
    )
    mask &= (
        high_level
        > low_level + noise_level * 3.0
    )

    pulse_threshold = (
        baseline + noise_level * 4.0
    )

    for pulse_sum, pulse_range in zip(
        pulse_sums,
        PREAMBLE_PULSE_RANGES,
    ):
        pulse_width = (
            pulse_range[1] - pulse_range[0]
        )
        pulse_level = pulse_sum / pulse_width
        mask &= pulse_level > pulse_threshold

    return np.flatnonzero(mask)


def format_decoded_frame(
    decoded: dict[str, Any],
    timestamp_us: float,
    confidence: float,
) -> str:
    fields = [
        f"time={timestamp_us / 1_000_000:10.3f}s",
        f"ICAO={decoded.get('icao', 'UNKNOWN')}",
        f"DF={decoded.get('df', '?')}",
    ]

    if "tc" in decoded:
        fields.append(f"TC={decoded['tc']}")

    fields.append(
        f"type={decoded.get('type', 'unknown')!r}"
    )

    callsign = decoded.get("callsign")

    if callsign:
        fields.append(f"callsign={callsign!r}")

    category = decoded.get("category")

    if category is not None:
        fields.append(f"category={category}")

    altitude = decoded.get("altitude_ft")

    if altitude is not None:
        fields.append(f"altitude={altitude}ft")

    speed = decoded.get("speed_kt")

    if speed is not None:
        fields.append(f"speed={speed}kt")

    ground_speed = decoded.get("ground_speed_kt")

    if ground_speed is not None:
        fields.append(
            f"ground_speed={ground_speed}kt"
        )

    track = decoded.get("track_deg")

    if track is not None:
        fields.append(f"track={track:.1f}deg")

    heading = decoded.get("heading_deg")

    if heading is not None:
        fields.append(f"heading={heading:.1f}deg")

    vertical_rate = decoded.get(
        "vertical_rate_fpm"
    )

    if vertical_rate is not None:
        fields.append(
            f"vertical_rate={vertical_rate:+d}ft/min"
        )

    latitude = decoded.get("latitude")
    longitude = decoded.get("longitude")

    if latitude is not None and longitude is not None:
        fields.append(
            f"position={latitude:.6f},{longitude:.6f}"
        )
    else:
        cpr = decoded.get("cpr")

        if cpr is not None:
            fields.append(f"CPR={cpr}")

    speed_type = decoded.get("speed_type")

    if speed_type is not None:
        fields.append(f"speed_type={speed_type}")

    fields.append(f"confidence={confidence:.3f}")
    fields.append(f"frame={decoded['frame']}")

    return " ".join(fields)


def read_iq_chunks(
    source: BinaryIO,
    chunk_samples: int,
    input_format: str,
) -> Iterator[np.ndarray]:
    if input_format == "cf32":
        sample_size_bytes = np.dtype(
            np.complex64
        ).itemsize
    elif input_format == "cu8":
        sample_size_bytes = 2
    else:
        raise ValueError(
            f"unsupported input format: {input_format}"
        )

    chunk_size_bytes = (
        chunk_samples * sample_size_bytes
    )

    remainder = b""

    while True:
        data = source.read(chunk_size_bytes)

        if not data:
            break

        data = remainder + data

        complete_size = (
            len(data) // sample_size_bytes
        ) * sample_size_bytes

        complete_data = data[:complete_size]
        remainder = data[complete_size:]

        if complete_data:
            if input_format == "cf32":
                samples = np.frombuffer(
                    complete_data,
                    dtype=np.complex64,
                ).copy()

                if not np.all(np.isfinite(samples)):
                    invalid_count = int(
                        np.count_nonzero(
                            ~np.isfinite(samples)
                        )
                    )
                    raise RuntimeError(
                        f"Input contains {invalid_count} "
                        "NaN or Inf samples"
                    )
            else:
                components = np.frombuffer(
                    complete_data,
                    dtype=np.uint8,
                ).astype(np.float32)
                components -= 127.5
                components /= 127.5
                samples = components.view(
                    np.complex64
                )

            yield samples

    if remainder:
        print(
            f"Warning: ignored "
            f"{len(remainder)} trailing bytes",
            file=sys.stderr,
        )


def open_input(
    path: str,
) -> tuple[BinaryIO, bool]:
    if path == "-":
        return sys.stdin.buffer, False

    return Path(path).open("rb"), True


def process_stream(
    source: BinaryIO,
    chunk_samples: int,
    input_sample_rate: int,
    input_format: str,
    noise_time_constant_seconds: float,
    refresh_rate: float,
    stale_seconds: float,
    page_size: int,
    receiver_latitude: float,
    receiver_longitude: float,
    map_height: int,
    map_source: str,
    map_style: Path,
) -> None:
    parser = AdsbFrameParser()
    address_validator = ModeSAddressValidator()
    aircraft_tracker = AircraftTracker()
    interpolator = StreamingInterpolator(
        input_sample_rate
    )

    tracker = SignalLevelTracker(
        sample_rate=SAMPLE_RATE,
        time_constant_seconds=(
            noise_time_constant_seconds
        ),
    )

    console = Console()
    scroll = ScrollController()
    scroll.start()

    map_view = None
    auto_map_height = map_height == -1

    if map_height != 0:
        map_view = MapView(
            latitude=receiver_latitude,
            longitude=receiver_longitude,
            zoom=DEFAULT_MAP_ZOOM,
            height=(
                AdsbTui.MINIMUM_MAP_HEIGHT
                if auto_map_height
                else map_height
            ),
            source=map_source,
            style=map_style,
            auto_fit=True,
        )

    tui = AdsbTui(
        console=console,
        stale_seconds=stale_seconds,
        page_size=page_size,
        scroll=scroll,
        map_view=map_view,
        auto_map_height=auto_map_height,
    )

    magnitude_buffer = np.empty(
        0,
        dtype=np.float32,
    )

    buffer_start_sample = 0

    total_input_samples = 0
    total_output_samples = 0

    candidates = 0
    valid_frames = 0
    parser_errors = 0
    chunk_number = 0

    refresh_interval = 1.0 / refresh_rate
    last_refresh = 0.0

    def render() -> Group:
        return tui.render(
            aircraft_tracker,
            input_sample_rate=input_sample_rate,
            total_input_samples=total_input_samples,
            candidates=candidates,
            valid_frames=valid_frames,
            baseline=tracker.baseline,
            noise_level=tracker.noise_level,
            parser_errors=parser_errors,
        )

    with Live(
        render(),
        console=console,
        refresh_per_second=refresh_rate,
        auto_refresh=False,
        transient=False,
        screen=True,
        vertical_overflow="crop",
    ) as live:
        for input_chunk in read_iq_chunks(
            source,
            chunk_samples,
            input_format,
        ):
            if scroll.stop_requested.is_set():
                break

            chunk_number += 1
            total_input_samples += input_chunk.size

            interpolated = interpolator.process(
                input_chunk
            )
            total_output_samples += interpolated.size

            magnitude_chunk = np.abs(
                interpolated
            ).astype(
                np.float32,
                copy=False,
            )

            tracker.update(magnitude_chunk)

            assert tracker.baseline is not None
            assert tracker.noise_level is not None

            if magnitude_buffer.size == 0:
                magnitude_buffer = (
                    magnitude_chunk.copy()
                )
            else:
                magnitude_buffer = np.concatenate(
                    (
                        magnitude_buffer,
                        magnitude_chunk,
                    )
                )

            candidate_indices = find_preamble_candidates(
                magnitude_buffer,
                tracker.baseline,
                tracker.noise_level,
            )

            next_allowed_index = 0

            for index_value in candidate_indices:
                index = int(index_value)

                if index < next_allowed_index:
                    continue

                candidates += 1

                data_start = index + PREAMBLE_SAMPLES

                candidate = decode_mode_s_candidate(
                    magnitude_buffer,
                    data_start,
                    tracker.baseline,
                )

                if candidate is None:
                    continue

                frame, downlink_format, confidence = candidate

                absolute_output_sample = (
                    buffer_start_sample + index
                )
                corrected_output_sample = (
                    absolute_output_sample
                    - interpolator.delay_samples
                )
                timestamp_seconds = (
                    corrected_output_sample / SAMPLE_RATE
                )

                message = frame.hex().upper()

                try:
                    icao = pms.icao(message)
                except Exception:
                    continue

                if not icao:
                    continue

                if downlink_format in (17, 18):
                    if modes_crc(frame) != 0:
                        continue

                    address_validator.trust(icao)

                elif downlink_format in (4, 5, 20, 21):
                    if icao not in address_validator.known_addresses:
                        continue

                else:
                    continue

                valid_frames += 1

                try:
                    decoded = parser.parse(
                        frame,
                        timestamp_seconds,
                    )

                    aircraft_tracker.update(
                        decoded,
                        timestamp_seconds,
                        confidence,
                    )
                except Exception:
                    parser_errors += 1

                frame_samples = (
                    PREAMBLE_SAMPLES
                    + len(frame) * 8 * BIT_SAMPLES
                )
                next_allowed_index = (
                    index + frame_samples
                )

            consumed_samples = max(
                0,
                magnitude_buffer.size
                - REQUIRED_SAMPLES
                + 1,
            )

            if consumed_samples > 0:
                magnitude_buffer = (
                    magnitude_buffer[
                        consumed_samples:
                    ].copy()
                )
                buffer_start_sample += consumed_samples

            now = time.monotonic()

            if now - last_refresh >= refresh_interval:
                live.update(
                    render(),
                    refresh=True,
                )
                last_refresh = now

        live.update(
            render(),
            refresh=True,
        )

    scroll.close()

    print(
        f"\\nInput samples: {total_input_samples:,}",
        file=sys.stderr,
    )
    print(
        f"Output samples: {total_output_samples:,}",
        file=sys.stderr,
    )
    print(
        f"Duration: "
        f"{total_input_samples / input_sample_rate:.6f} s",
        file=sys.stderr,
    )
    print(
        f"Candidates: {candidates:,}",
        file=sys.stderr,
    )
    print(
        f"Valid CRC frames: {valid_frames:,}",
        file=sys.stderr,
    )
    print(
        f"Parser errors: {parser_errors:,}",
        file=sys.stderr,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Streaming 6 MS/s Mode-S decoder "
            "for CF32 or RTL-SDR CU8 IQ input"
        ),
    )

    parser.add_argument(
        "input",
        nargs="?",
        default="buffer.iq",
        help=(
            "Input IQ file, named pipe, "
            "or '-' for stdin"
        ),
    )

    parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_INPUT_SAMPLE_RATE,
        help=(
            "Input complex sample rate in samples/s; "
            "must divide 6000000 exactly (default: 3000000)"
        ),
    )

    parser.add_argument(
        "--input-format",
        choices=INPUT_FORMATS,
        default="cf32",
        help=(
            "Input IQ format: cf32 for Airspy FLOAT32_IQ, "
            "cu8 for rtl_sdr (default: cf32)"
        ),
    )

    parser.add_argument(
        "--refresh-rate",
        type=float,
        default=8.0,
        help="TUI refresh rate in frames per second",
    )

    parser.add_argument(
        "--stale-seconds",
        type=float,
        default=60.0,
        help="Hide aircraft not seen for this many stream seconds",
    )

    parser.add_argument(
        "--page-size",
        type=int,
        default=0,
        help=(
            "Aircraft panels per page; "
            "0 shows two with the map, or chooses automatically without it"
        ),
    )

    parser.add_argument(
        "--receiver-lat",
        "--map-lat",
        dest="receiver_lat",
        type=float,
        default=DEFAULT_RECEIVER_LATITUDE,
        help=(
            "Initial map center latitude "
            f"(default: {DEFAULT_RECEIVER_LATITUDE})"
        ),
    )

    parser.add_argument(
        "--receiver-lon",
        "--map-lon",
        dest="receiver_lon",
        type=float,
        default=DEFAULT_RECEIVER_LONGITUDE,
        help=(
            "Initial map center longitude "
            f"(default: {DEFAULT_RECEIVER_LONGITUDE})"
        ),
    )

    parser.add_argument(
        "--map-height",
        type=int,
        default=-1,
        help=(
            "Map content height in terminal rows; "
            "-1 fills available space, 0 disables it"
        ),
    )

    parser.add_argument(
        "--map-source",
        default=DEFAULT_MAP_SOURCE,
        help="Vector tile endpoint base URL",
    )

    parser.add_argument(
        "--map-style",
        type=Path,
        default=DEFAULT_MAP_STYLE,
        help="MapSCII style JSON file",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_arguments()

    if args.sample_rate <= 0:
        raise ValueError(
            "--sample-rate must be positive"
        )

    if SAMPLE_RATE % args.sample_rate != 0:
        raise ValueError(
            "--sample-rate must divide "
            f"{SAMPLE_RATE} exactly; use 2000000, "
            "3000000, or 6000000"
        )

    if args.refresh_rate <= 0:
        raise ValueError(
            "--refresh-rate must be positive"
        )

    if args.stale_seconds <= 0:
        raise ValueError(
            "--stale-seconds must be positive"
        )

    if args.page_size < 0:
        raise ValueError(
            "--page-size must be zero or positive"
        )

    if args.map_height < -1:
        raise ValueError(
            "--map-height must be -1, zero, or positive"
        )

    source, should_close = open_input(
        args.input
    )

    try:
        process_stream(
            source=source,
            chunk_samples=CHUNK_SAMPLES,
            input_sample_rate=args.sample_rate,
            input_format=args.input_format,
            noise_time_constant_seconds=NOISE_TIME_CONSTANT_SECONDS,
            refresh_rate=args.refresh_rate,
            stale_seconds=args.stale_seconds,
            page_size=args.page_size,
            receiver_latitude=args.receiver_lat,
            receiver_longitude=args.receiver_lon,
            map_height=args.map_height,
            map_source=args.map_source,
            map_style=args.map_style,
        )
    finally:
        if should_close:
            source.close()


if __name__ == "__main__":
    main()
