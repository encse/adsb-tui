from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Iterable

import numpy as np
import pyModeS as pms
from rich.console import Console, Group
from rich.live import Live

from mapscii_py.rich_map import MapView

from adsb.sdr import SoapySdrSource

from .constants import (
    BIT_SAMPLES,
    CHUNK_SAMPLES,
    DEFAULT_MAP_ZOOM,
    PREAMBLE_SAMPLES,
    REQUIRED_SAMPLES,
    SAMPLE_RATE,
)
from .parser import (
    AdsbFrameParser,
    ModeSAddressValidator,
    modes_crc,
)
from .signal import (
    SignalLevelTracker,
    StreamingInterpolator,
    decode_mode_s_candidate,
    find_preamble_candidates,
)
from .tracking import AircraftTracker
from .tui import AdsbTui, ScrollController

def process_stream(
    source: SoapySdrSource,
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
    
    input_chunks=source.chunks(CHUNK_SAMPLES)
    device_label=source.label
    input_sample_rate=source.sample_rate


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
    scroll = ScrollController(
        map_visible=map_height != 0,
        gain_names=tuple(
            setting.name for setting in source.gain_settings
        ),
    )
    scroll.start()

    auto_map_height = map_height in (-1, 0)
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
            device_label=device_label,
            gain_summary=source.gain_summary,
            gain_settings=source.gain_settings,
            input_sample_rate=input_sample_rate,
            total_input_samples=total_input_samples,
            candidates=candidates,
            valid_frames=valid_frames,
            activity_db=tracker.activity_db,
            sdr_overflow_errors=source.overflow_count,
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
        for input_chunk in input_chunks:
            if scroll.stop_requested.is_set():
                break

            for gain_name, steps in scroll.pop_gain_actions():
                source.adjust_gain(gain_name, steps)

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
