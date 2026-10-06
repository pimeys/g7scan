# g7scan

Rebuild a Dexcom G7 sensor's pairing code from the values printed on the
applicator label, in a form a phone scanner reads instantly.

Juggluco (and other G7 readers) pair a sensor by scanning the GS1 DataMatrix
on the applicator. When the camera will not lock onto that small label, the
pairing does not have to fail: every field inside the symbol is also printed
in clear text on the same label, and any code carrying the same GS1 bytes
pairs the sensor.

g7scan asks for the printed values, rebuilds the GS1 element string, and:

- opens the code fullscreen on a white page — the display a phone scanner
  reads instantly: vector drawing, real quiet zone, nothing to zoom or align
- draws a small block in the terminal as the fallback
- can write a PNG, for printing or viewing at 100%

## Usage

    ./g7scan.py                          # asks for each printed value
    ./g7scan.py --no-show                # terminal block only
    ./g7scan.py --symbology datamatrix   # the applicator's own symbol form
    ./g7scan.py --png code.png           # also save a PNG

Or pass everything directly:

    ./g7scan.py --gtin 00386270004871 --serial 123456789012 \
                --mfg 2026-01-15 --exp 2026-10-15 --code 1234

| flag | AI | where it is on the label |
|---|---|---|
| `--gtin` | (01) | the line starting with `(01)`, 14 digits |
| `--serial` | (21) | the line starting with `(21)` |
| `--mfg` | (11) | left of the two dates under the pairing code |
| `--exp` | (17) | right of the two dates |
| `--code` | (240) | the 4 characters printed large |

You can paste the whole `(01)…` / `(21)…` line at a prompt; the AI prefix is
stripped. Values are validated before anything is drawn: GTIN length, real
dates, expiry not earlier than production (the label prints them left and
right), pairing code exactly 4 characters.

## Requirements

- [zint](https://zint.org.uk) — Arch: `pacman -S zint`, Debian: `apt install zint`
- Python Pillow — Arch: `python-pillow`, Debian: `python3-pil`
- a Chromium-based browser, for the fullscreen window

## Notes

- The payload is the GS1 element string `01…21…<GS>11…17…240…`, with the GS
  separator after the variable-length AI (21) — byte-identical to what the
  applicator's DataMatrix carries. zint builds it with `--gs1 --gs1parens`.
- A quiet zone is not optional. zint's `--border` draws 1D binding bars,
  not whitespace (`--whitesp` is the quiet zone); g7scan pads its own quiet
  zone into the matrix so every output carries one.
- zint's PNG rasteriser draws some symbologies at 2 px per module even at
  `--scale=1`; the grid is compressed back to one cell per module first.
- The default output is a QR code: GS1 packs the long digit runs tightly,
  so a QR is roughly half the size of the equivalent DataMatrix.
  `--symbology datamatrix` draws the applicator's own symbol form instead.
- In the terminal, one half block is one module — a terminal cell is about
  twice as tall as it is wide, so half blocks are square and `--scale 1`
  stays scannable.
- Every rendering (window, terminal, PNG) is verified by decoding it back
  with [zxing-cpp](https://github.com/zxing-cpp/zxing) and comparing bytes.
