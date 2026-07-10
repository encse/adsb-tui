from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pyModeS as pms

from .constants import CRC_POLYNOMIAL


@dataclass
class PositionFrame:
    message: str
    timestamp_seconds: float


def modes_crc(frame: bytes) -> int:
    value = int.from_bytes(frame, byteorder="big")
    bit_count = len(frame) * 8

    for bit_position in range(bit_count - 1, 23, -1):
        if value & (1 << bit_position):
            value ^= CRC_POLYNOMIAL << (bit_position - 24)

    return value & 0xFFFFFF


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
