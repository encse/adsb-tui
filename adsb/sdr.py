from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from types import ModuleType
from typing import Iterator
import warnings

import numpy as np


@dataclass(frozen=True)
class DeviceProfile:
    driver: str
    sample_rate: int
    gains: dict[str, float] = field(default_factory=dict)
    total_gain: float | None = None


DEVICE_PROFILES = {
    "airspy": DeviceProfile(
        driver="airspy",
        sample_rate=3_000_000,
        gains={"LNA": 14.0, "MIX": 14.0, "VGA": 14.0},
    ),
    "rtlsdr": DeviceProfile(
        driver="rtlsdr",
        sample_rate=2_000_000,
        total_gain=40.0,
    ),
}


class SdrError(RuntimeError):
    pass


def _load_soapy_sdr() -> ModuleType:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=DeprecationWarning,
                message="builtin type .* has no __module__ attribute",
            )
            import SoapySDR
    except ImportError as error:
        raise SdrError(
            "SoapySDR is not installed in this Python environment. "
            "Install soapysdr and the selected device module with conda."
        ) from error

    return SoapySDR


class SoapySdrSource:
    def __init__(
        self,
        device_type: str,
        frequency_hz: int,
    ) -> None:
        try:
            self.profile = DEVICE_PROFILES[device_type]
        except KeyError as error:
            supported = ", ".join(DEVICE_PROFILES)
            raise ValueError(
                f"unsupported SDR device {device_type!r}; use {supported}"
            ) from error

        self.device_type = device_type
        self.frequency_hz = frequency_hz
        self.sample_rate = self.profile.sample_rate
        self.label = device_type
        self._soapy: ModuleType | None = None
        self._device = None
        self._stream = None

    def __enter__(self) -> SoapySdrSource:
        soapy = _load_soapy_sdr()
        direction = soapy.SOAPY_SDR_RX

        device = None
        stream = None

        try:
            matches = soapy.Device.enumerate(
                {"driver": self.profile.driver}
            )

            if not matches:
                raise SdrError(
                    f"no connected {self.device_type} device was found. "
                    "Check the USB connection, close other SDR programs, "
                    "and verify detection with 'SoapySDRUtil --find'."
                )

            device_info = matches[0]

            try:
                self.label = device_info["label"]
            except (KeyError, TypeError):
                self.label = self.device_type

            device = soapy.Device(device_info)
            device.setSampleRate(direction, 0, self.sample_rate)
            device.setFrequency(direction, 0, self.frequency_hz)

            if self.profile.total_gain is not None:
                device.setGain(direction, 0, self.profile.total_gain)

            for name, value in self.profile.gains.items():
                device.setGain(direction, 0, name, value)

            stream = device.setupStream(
                direction,
                soapy.SOAPY_SDR_CF32,
                [0],
            )
            self._stream = stream
            device.activateStream(stream)
        except SdrError:
            self._close_device(device, stream)
            self._stream = None
            raise
        except Exception as error:
            self._close_device(device, stream)
            self._stream = None
            raise SdrError(
                f"could not configure the {self.device_type} device: {error}"
            ) from error

        self._soapy = soapy
        self._device = device
        self._stream = stream
        return self

    @property
    def gain_summary(self) -> str:
        if self.profile.gains:
            return " · ".join(
                f"{name} {value:g} dB"
                for name, value in self.profile.gains.items()
            )

        if self.profile.total_gain is not None:
            return f"gain {self.profile.total_gain:g} dB"

        return "automatic gain"

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def chunks(self, chunk_samples: int) -> Iterator[np.ndarray]:
        if self._device is None or self._stream is None:
            raise RuntimeError("SDR stream is not active")
        if chunk_samples <= 0:
            raise ValueError("chunk_samples must be positive")

        buffer = np.empty(chunk_samples, dtype=np.complex64)

        while True:
            result = self._device.readStream(
                self._stream,
                [buffer],
                buffer.size,
                timeoutUs=1_000_000,
            )

            if result.ret > 0:
                yield buffer[:result.ret].copy()
                continue

            assert self._soapy is not None

            if result.ret in (
                self._soapy.SOAPY_SDR_TIMEOUT,
                self._soapy.SOAPY_SDR_OVERFLOW,
            ):
                continue

            raise SdrError(
                f"SoapySDR readStream failed with error {result.ret}"
            )

    def close(self) -> None:
        self._close_device(self._device, self._stream)
        self._stream = None
        self._device = None
        self._soapy = None

    @staticmethod
    def _close_device(device, stream) -> None:
        if device is None or stream is None:
            return

        with suppress(Exception):
            device.deactivateStream(stream)

        device.closeStream(stream)
