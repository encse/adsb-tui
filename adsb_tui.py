#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path

from adsb.constants import (
    CHUNK_SAMPLES,
    DEFAULT_INPUT_SAMPLE_RATE,
    DEFAULT_RECEIVER_LATITUDE,
    DEFAULT_RECEIVER_LONGITUDE,
    INPUT_FORMATS,
    NOISE_TIME_CONSTANT_SECONDS,
    SAMPLE_RATE,
)
from adsb.io import open_input
from adsb.processing import process_stream
from mapscii.mapscii_py.rich_map import (
    DEFAULT_SOURCE as DEFAULT_MAP_SOURCE,
    DEFAULT_STYLE as DEFAULT_MAP_STYLE,
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
