# ADS-B TUI

![screenshot](screenshot.jpg)

An ADS-B decoder for the terminal, built around a Python port of [mapscii](https://github.com/rastapasta/mapscii).

The decoder is know to work with `Airspy mini` and `RTL-SDR` dongles.

## Installation

I recommend using [conda](https://docs.conda.io/projects/conda/en/latest/user-guide/install/index.html) to create a clean and throwaway environment.

```
conda create -n adsb -c conda-forge python=3.13
conda activate adsb
python3 -m pip install -r requirements.txt
```

## Airspy

```
conda install -c conda-forge airspy
```

`airspy_rx -t 0` produces `FLOAT32_IQ`, which corresponds to the decoder's
`cf32` format:

```sh
airspy_rx -f 1090 -a 3000000 -t 0 -l 14 -m 14 -v 14 -r - 2> /dev/null | python3 adsb_tui.py -
```

## RTL-SDR
```
conda install -c conda-forge rtl-sdr
```

Create a named pipe once:

```sh
mkfifo buffer.cu8
```

Start the receiver in one terminal. It waits until the decoder opens the FIFO:

```sh
rtl_sdr -f 1090000000 -s 2000000 -g 40 buffer.cu8
```

Start the TUI in another terminal:

```sh
python3 adsb_tui.py \
    --sample-rate 2000000 \
    --input-format cu8 \
    buffer.cu8
```

The FIFO can be reused between runs. Remove it with `rm buffer.cu8` when
it is no longer needed.


## Receiver position and map

Set the receiver position with `--receiver-lat` and `--receiver-lon`:

```sh
python3 adsb_tui.py \
    --receiver-lat 47.4979 \
    --receiver-lon 19.0402 \
    buffer.cu8
```
