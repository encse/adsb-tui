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


@dataclass(frozen=True)
class GainSetting:
    name: str
    value: float
    minimum: float
    maximum: float
    step: float


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

        self.overflow_count = 0
        self.underflow_count = 0

        self._soapy: ModuleType | None = None
        self._device = None
        self._stream = None
        self._log_handler = None
        self._gain_names: tuple[str, ...] = ()
        self._gain_settings: tuple[GainSetting, ...] = ()

    def _handle_soapy_log(
        self,
        level: int,
        message: str,
    ) -> None:
        if self._soapy is None:
            return

        if level == self._soapy.SOAPY_SDR_SSI:
            print("xxxx", message)

            indicators = message.strip()

            self.overflow_count += indicators.count("O")
            self.underflow_count += indicators.count("U")
            return

        # Ignore other SoapySDR messages here, or forward them to your
        # application logger if needed.

    def __enter__(self) -> SoapySdrSource:
        soapy = _load_soapy_sdr()
        self._soapy = soapy

        self._log_handler = self._handle_soapy_log
        soapy.registerLogHandler(self._log_handler)

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
                device.setGain(
                    direction,
                    0,
                    self.profile.total_gain,
                )

            for name, value in self.profile.gains.items():
                device.setGain(
                    direction,
                    0,
                    name,
                    value,
                )

            self._gain_names = tuple(device.listGains(direction, 0))
            self._gain_settings = self._read_gain_settings(device, soapy)

            stream = device.setupStream(
                direction,
                soapy.SOAPY_SDR_CF32,
                [0],
            )

            device.activateStream(stream)

        except SdrError:
            self._close_device(device, stream)
            self._restore_log_handler()
            raise
        except Exception as error:
            self._close_device(device, stream)
            self._restore_log_handler()
            raise SdrError(
                f"could not configure the {self.device_type} device: {error}"
            ) from error

        self._device = device
        self._stream = stream
        return self

    @property
    def gain_summary(self) -> str:
        settings = self.gain_settings
        if settings:
            return " · ".join(
                f"{setting.name} {setting.value:g} dB"
                for setting in settings
            )

        return "automatic gain"

    @property
    def gain_settings(self) -> tuple[GainSetting, ...]:
        return self._gain_settings

    def _read_gain_settings(
        self,
        device,
        soapy: ModuleType,
    ) -> tuple[GainSetting, ...]:
        direction = soapy.SOAPY_SDR_RX
        settings = []
        for name in self._gain_names:
            gain_range = device.getGainRange(
                direction,
                0,
                name,
            )
            step = float(gain_range.step())
            settings.append(
                GainSetting(
                    name=name,
                    value=float(device.getGain(direction, 0, name)),
                    minimum=float(gain_range.minimum()),
                    maximum=float(gain_range.maximum()),
                    step=step if step > 0 else 1.0,
                )
            )
        return tuple(settings)

    def adjust_gain(self, name: str, steps: int) -> None:
        if self._device is None or self._soapy is None:
            raise RuntimeError("SDR stream is not active")

        setting = next(
            item for item in self.gain_settings if item.name == name
        )
        value = setting.value + steps * setting.step
        value = min(setting.maximum, max(setting.minimum, value))
        self._device.setGain(
            self._soapy.SOAPY_SDR_RX,
            0,
            name,
            value,
        )
        self._gain_settings = self._read_gain_settings(
            self._device,
            self._soapy,
        )

    def _restore_log_handler(self) -> None:
        if self._soapy is not None:
            with suppress(Exception):
                self._soapy.registerLogHandler(None)

        self._log_handler = None
        
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
        self._gain_names = ()
        self._gain_settings = ()
        self._restore_log_handler()
        self._soapy = None

    @staticmethod
    def _close_device(device, stream) -> None:
        if device is None or stream is None:
            return

        with suppress(Exception):
            device.deactivateStream(stream)

        device.closeStream(stream)
