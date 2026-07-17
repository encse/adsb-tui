#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path

from adsb.constants import (
    ADSB_FREQUENCY_HZ,
    CHUNK_SAMPLES,
    DEFAULT_RECEIVER_LATITUDE,
    DEFAULT_RECEIVER_LONGITUDE,
    NOISE_TIME_CONSTANT_SECONDS,
    SAMPLE_RATE,
)
from adsb.processing import process_stream
from adsb.sdr import SdrError, SoapySdrSource
from mapscii_py.rich_map import DEFAULT_SOURCE as DEFAULT_MAP_SOURCE

DEFAULT_MAP_STYLE = Path(__file__).resolve().parent / "style.json"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Streaming Mode-S decoder using a SoapySDR device"
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
        "--list-size",
        type=int,
        default=2,
        help="Number of aircraft panels shown below the visible map",
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

    if args.refresh_rate <= 0:
        raise ValueError(
            "--refresh-rate must be positive"
        )

    if args.stale_seconds <= 0:
        raise ValueError(
            "--stale-seconds must be positive"
        )

    if args.list_size <= 0:
        raise ValueError(
            "--list-size must be positive"
        )


    try:
        with SoapySdrSource(
            None,
            ADSB_FREQUENCY_HZ,
        ) as source:
            if SAMPLE_RATE % source.sample_rate != 0:
                raise ValueError(
                    f"{source.device_type} sample rate must divide "
                    f"{SAMPLE_RATE} exactly"
                )

            process_stream(
                source=source,
                noise_time_constant_seconds=NOISE_TIME_CONSTANT_SECONDS,
                refresh_rate=args.refresh_rate,
                stale_seconds=args.stale_seconds,
                list_size=args.list_size,
                receiver_latitude=args.receiver_lat,
                receiver_longitude=args.receiver_lon,
                map_source=args.map_source,
                map_style=args.map_style,
            )
    except SdrError as error:
        raise SystemExit(f"SDR error: {error}") from None


if __name__ == "__main__":
    main()
