from __future__ import annotations

import sys
from pathlib import Path
from typing import BinaryIO, Iterator

import numpy as np

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
