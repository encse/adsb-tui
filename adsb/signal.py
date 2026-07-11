from __future__ import annotations

import numpy as np
from scipy.signal import firwin, lfilter

from .constants import (
    BIT_SAMPLES,
    HALF_BIT_SAMPLES,
    INTERPOLATOR_TAPS,
    MAX_INTERPOLATOR_CUTOFF_HZ,
    MIN_NOISE_LEVEL,
    PREAMBLE_PULSE_RANGES,
    PREAMBLE_SAMPLES,
    REQUIRED_SAMPLES,
    SAMPLE_RATE,
    SHORT_DFS,
    SHORT_FRAME_BITS,
    LONG_FRAME_BITS,
    SUPPORTED_DFS,
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
        self.activity_db = 0.0

    def update(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return

        chunk_duration = samples.size / self.sample_rate

        measured_baseline = float(np.median(samples))
        measured_noise = float(
            np.median(
                np.abs(samples - measured_baseline)
            )
        )
        measured_noise = max(
            measured_noise,
            MIN_NOISE_LEVEL,
        )

        if self.baseline is None or self.noise_level is None:
            self.baseline = measured_baseline
            self.noise_level = measured_noise
        else:
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

        measured_activity = measure_chunk_activity_db(
            signal=samples,
            baseline=measured_baseline,
            noise_level=measured_noise,
        )

        if measured_activity > self.activity_db:
            activity_time_constant = 0.1
        else:
            activity_time_constant = 0.8

        activity_alpha = 1.0 - np.exp(
            -chunk_duration / activity_time_constant
        )

        self.activity_db += activity_alpha * (
            measured_activity - self.activity_db
        )

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

def measure_chunk_activity_db(
    signal: np.ndarray,
    baseline: float,
    noise_level: float,
) -> float:
    noise_level = max(
        noise_level,
        MIN_NOISE_LEVEL,
    )

    # Express every sample relative to the tracked noise distribution.
    normalized = (
        signal - baseline
    ) / noise_level

    # Ignore ordinary noise and retain only meaningful excursions.
    excess = np.maximum(
        normalized - 3.0,
        0.0,
    )

    # This combines signal strength and the duration of activity.
    activity = float(
        np.mean(excess * excess)
    )

    return 10.0 * np.log10(
        1.0 + activity
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
