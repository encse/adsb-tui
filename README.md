# ADS-B decoder

The decoder uses a fixed 6 MS/s internal timing grid. Input samples are
converted to `complex64` and interpolated to that rate. The input sample rate
must currently divide 6 MHz exactly; the intended device configurations are:

| Receiver | Input rate | Input format | Interpolation |
| --- | ---: | --- | ---: |
| Airspy | 3 MS/s | `cf32` | 2x |
| RTL-SDR | 2 MS/s | `cu8` | 3x |

## Receiver position and map

Set the fixed receiver position with `--receiver-lat` and `--receiver-lon`:

```sh
python3 adsb/adsb_tui.py \
    --receiver-lat 47.4979 \
    --receiver-lon 19.0402 \
    buffer.iq
```

The receiver is shown as a red `● RX` marker and remains at the center of the
map. `--map-zoom` is the maximum zoom level; the map automatically zooms out
when necessary so that every active aircraft with a decoded position fits in
the viewport.

## Airspy

`airspy_rx -t 0` produces `FLOAT32_IQ`, which corresponds to the decoder's
`cf32` format:

```sh
airspy_rx -f 1090000000 -a 3000000 -t 0 -r - 2> /dev/null |
python3 adsb/adsb_tui.py \
    --sample-rate 3000000 \
    --input-format cf32 \
    -
```

These are also the defaults, so the two input options may be omitted for
Airspy.

## RTL-SDR

`rtl_sdr` produces unsigned, interleaved 8-bit I/Q samples. Feed those bytes
directly to the decoder as `cu8`; no external format converter is needed:

Create a named pipe once:

```sh
mkfifo /tmp/adsb-rtl.cu8
```

Start the receiver in one terminal. It waits until the decoder opens the FIFO:

```sh
rtl_sdr -f 1090000000 -s 2000000 -g 40 /tmp/adsb-rtl.cu8
```

Start the TUI in another terminal:

```sh
python3 adsb/adsb_tui.py \
    --sample-rate 2000000 \
    --input-format cu8 \
    /tmp/adsb-rtl.cu8
```

The FIFO can be reused between runs. Remove it with `rm /tmp/adsb-rtl.cu8` when
it is no longer needed.
