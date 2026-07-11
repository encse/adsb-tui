from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

    def advance_time(self, timestamp_seconds: float) -> None:
        self.latest_timestamp_seconds = max(
            self.latest_timestamp_seconds,
            timestamp_seconds,
        )

    def update(
        self,
        decoded: dict[str, Any],
        timestamp_seconds: float,
        confidence: float,
    ) -> None:
        icao = decoded.get("icao")

        if not icao:
            return

        self.advance_time(timestamp_seconds)

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
