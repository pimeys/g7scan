#!/usr/bin/env python3
"""Rebuild a Dexcom G7 applicator barcode from the label's printed text.

Juggluco pairs a Dexcom G7 by reading the GS1 DataMatrix on the applicator.
Every field inside that symbol is also printed in clear text on the same
label, so the symbol can be regenerated when the camera scan will not
cooperate: you type the printed values and the tool opens the code
fullscreen on a white page - the display a phone scanner reads instantly -
plus a small block in the terminal as the fallback.

The default code is a QR carrying the same GS1 bytes as the applicator's
DataMatrix: GS1 packs the long digit runs tightly enough that a QR comes
out roughly half the size, which --symbology datamatrix draws instead.
Every drawing carries its own white quiet zone - the camera needs one.

Dexcom G7 label, and where each field is:

     +-----------------------------------------------------+
     |   ########            Dexcom G7 Sensor             |
     |   #  ##  #                                          |
     |   #  ##  #                1234                      |  <- (240) pairing code
     |   ########                                          |
     |                                                     |
     |                    2026-01-15    2026-10-15         |  <- (11) then (17)
     |                                                     |
     |   (01)00386270004871                                |  <- (01) GTIN
     |   (21)123456789012                                  |  <- (21) serial
     |   LBL-1001234 REV002                                |  <- not in the symbol
     +-----------------------------------------------------+

The order of the two dates is whatever the symbol carries: the left date is
AI (11) and the right date is AI (17).  The tool checks that the right date
is not earlier than the left one, so a swap is caught before you scan.

Requires zint(1) and Pillow; the fullscreen window needs a Chromium-based
browser.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from itertools import groupby
from math import gcd

GS = "\x1d"

SYMBOLOGY_IDS = {"datamatrix": "71", "qr": "58"}

# The fullscreen page show_code() writes; kept in place so the browser
# window survives the script, overwritten on the next run.
SHOW_PAGE = "/tmp/g7scan-code.html"

BLACK = "\u2588"  # full block
UPPER = "\u2580"  # upper half block
LOWER = "\u2584"  # lower half block
EMPTY = " "

# Black modules on a white background, forced regardless of the terminal
# theme: an inverted symbol will not decode, and the white also forms the
# quiet zone the detector needs.
PAPER = "\x1b[30;47m"
RESET = "\x1b[0m"


class FieldError(ValueError):
    """A typed value that cannot be used as-is."""


# --------------------------------------------------------------------------
# parsing the printed values
# --------------------------------------------------------------------------


def normalize_gtin(raw: str) -> str:
    """AI (01): exactly 14 digits."""
    digits = re.sub(r"\D", "", strip_ai(raw, "01"))
    if len(digits) == 13:  # some label revisions drop the leading zero
        digits = "0" + digits
    if len(digits) != 14:
        raise FieldError(
            f"GTIN must be 14 digits, got {len(digits)}; "
            "copy the number from the (01) line"
        )
    return digits


def normalize_serial(raw: str) -> str:
    """AI (21): up to 20 alphanumeric characters, 12 on a G7."""
    value = re.sub(r"[^A-Za-z0-9]", "", strip_ai(raw, "21"))
    if not 1 <= len(value) <= 20:
        raise FieldError(
            f"serial must be 1-20 letters/digits, got {len(value)}; "
            "copy the value from the (21) line"
        )
    return value


def normalize_date(raw: str, what: str) -> tuple[str, date]:
    """AI (11)/(17): YYMMDD, also accept YYYY-MM-DD as printed."""
    value = strip_ai(raw, "11").strip()
    match = re.fullmatch(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", value)
    if match:
        year, month, day = (int(g) for g in match.groups())
    else:
        digits = re.sub(r"\D", "", value)
        match = re.fullmatch(r"(\d{2})(\d{2})(\d{2})", digits)
        if not match:
            raise FieldError(
                f"{what}: expected YYYY-MM-DD or YYMMDD as printed, got {raw!r}"
            )
        yy, month, day = (int(g) for g in match.groups())
        year = 2000 + yy if yy < 50 else 1900 + yy
    try:
        when = date(year, month, day)
    except ValueError as exc:
        raise FieldError(f"{what}: {exc}") from None
    return f"{when:%y%m%d}", when


def normalize_code(raw: str) -> str:
    """AI (240): the 4 characters printed large on the applicator."""
    value = re.sub(r"[^A-Za-z0-9]", "", strip_ai(raw, "240"))
    if len(value) != 4:
        raise FieldError(
            f"pairing code must be exactly 4 characters, got {len(value)}"
        )
    return value


def strip_ai(raw: str, ai: str) -> str:
    """Drop a leading '(01)' style AI if the user pasted the whole line."""
    return re.sub(rf"^\s*\(\s*{ai}\s*\)\s*", "", raw.strip())


# --------------------------------------------------------------------------
# building and drawing the symbol
# --------------------------------------------------------------------------


def build_payload(gtin: str, serial: str, mfg: str, expiry: str, code: str) -> str:
    """GS1 element string: AI (21) is variable length, so GS separates it."""
    return f"01{gtin}21{serial}{GS}11{mfg}17{expiry}240{code}"


def build_ai_text(gtin: str, serial: str, mfg: str, expiry: str, code: str) -> str:
    """The same data in the bracketed form zint's --gs1parens understands."""
    return f"(01){gtin}(21){serial}(11){mfg}(17){expiry}(240){code}"


def zint_matrix(ai_text: str, symbology: str) -> list[list[bool]]:
    """Render the symbol with zint and read it back as one cell per module.

    zint's PNG rasteriser draws some symbologies with more than one pixel
    per module even at --scale=1 (a QR comes out at 2x), so the grid is
    compressed by the largest pitch common to every run first.  zint's
    --border draws binding bars, not whitespace, so what comes back here is
    the bare symbol; pad_matrix() adds the quiet zone.
    """
    exe = shutil.which("zint")
    if exe is None:
        sys.exit("g7scan: zint(1) is required but was not found in PATH")
    try:
        from PIL import Image
    except ImportError:
        sys.exit("g7scan: Pillow is required (python3 -m pip install pillow)")

    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "symbol.png")
        cmd = [
            exe,
            "-b", SYMBOLOGY_IDS[symbology],
            "--gs1",
            "--gs1parens",
            "--gs1nocheck",
            "--scale=1",
            "-o", png,
            "-d", ai_text,
        ]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0 or not os.path.exists(png):
            sys.exit(f"g7scan: zint failed\n{done.stdout}{done.stderr}".rstrip())
        image = Image.open(png).convert("L")
        width, height = image.size
        pixels = image.load()
        grid = [[pixels[x, y] < 128 for x in range(width)] for y in range(height)]

    pitch = 0
    for line in grid:
        pitch = gcd(pitch, *(len(list(group)) for _, group in groupby(line)))
    for column in zip(*grid):
        pitch = gcd(pitch, *(len(list(group)) for _, group in groupby(column)))
    if pitch > 1 and width % pitch == height % pitch == 0:
        grid = [[row[x * pitch] for x in range(width // pitch)]
                for row in (grid[y * pitch] for y in range(height // pitch))]
    return grid


def pad_matrix(matrix: list[list[bool]], border: int) -> list[list[bool]]:
    """Add the quiet zone: `border` blank modules on all four sides.

    Without it the dark modules sit directly against whatever surrounds the
    drawing - a dark terminal or an image viewer - and the detector gives
    up.  The padding lives in the matrix so the drawing and the PNG carry
    the same quiet zone.
    """
    if border <= 0:
        return matrix
    blank = [False] * (len(matrix[0]) + 2 * border)
    side = [False] * border
    return ([list(blank) for _ in range(border)]
            + [side + row + side for row in matrix]
            + [list(blank) for _ in range(border)])


def render_blocks(matrix: list[list[bool]], scale: int) -> list[str]:
    """Draw a module bitmap with half blocks: one module is square on screen.

    A terminal cell is about twice as tall as it is wide, so one half block is
    about as wide as it is tall.  A module therefore takes `scale` cells
    across and `scale` half blocks down, which needs `scale / 2` cell rows.
    """
    half_rows: list[list[bool]] = []
    for row in matrix:
        widened = [module for module in row for _ in range(scale)]
        half_rows.extend([widened] * scale)

    lines = []
    for index in range(0, len(half_rows), 2):
        top = half_rows[index]
        bottom = half_rows[index + 1] if index + 1 < len(half_rows) else [False] * len(top)
        line = []
        for upper, lower in zip(top, bottom):
            if upper and lower:
                line.append(BLACK)
            elif upper:
                line.append(UPPER)
            elif lower:
                line.append(LOWER)
            else:
                line.append(EMPTY)
        lines.append("".join(line))
    return lines


# --------------------------------------------------------------------------
# prompting
# --------------------------------------------------------------------------


class Field:
    def __init__(self, label, ai, where, example, parse):
        self.label = label
        self.ai = ai
        self.where = where
        self.example = example
        self.parse = parse


FIELDS = [
    Field("GTIN", "(01)",
          "printed under the square block, on the line starting with (01)",
          "(01)00386270004871", normalize_gtin),
    Field("Serial number", "(21)",
          "printed under the square block, on the line starting with (21)",
          "(21)123456789012", normalize_serial),
    Field("Manufacturing date", "(11)",
          "the LEFT of the two dates printed under the 4-character code",
          "2026-01-15", lambda raw: normalize_date(raw, "manufacturing date")),
    Field("Expiry date", "(17)",
          "the RIGHT of the two dates printed under the 4-character code",
          "2026-10-15", lambda raw: normalize_date(raw, "expiry date")),
    Field("Pairing code", "(240)",
          "the 4 characters printed large beside 'Dexcom G7 Sensor', above the dates",
          "1234", normalize_code),
]


def ask(field: Field) -> object:
    print()
    print(f"{field.label}  [AI {field.ai}]")
    print(f"   where      : {field.where}")
    print(f"   looks like : {field.example}")
    while True:
        try:
            raw = input(f"   {field.label.lower()} > ")
        except EOFError:
            sys.exit("\ng7scan: input ended before all fields were given")
        except KeyboardInterrupt:
            sys.exit("\ng7scan: cancelled")
        if not raw.strip():
            print("   (nothing entered - try again)")
            continue
        try:
            value = field.parse(raw)
        except FieldError as exc:
            print(f"   {exc}")
            continue
        shown = value[0] if isinstance(value, tuple) else value
        print(f"   ok: {shown}")
        return value


def collect(args) -> dict:
    if args.gtin:
        try:
            mfg, mfg_date = normalize_date(args.mfg, "manufacturing date")
            expiry, expiry_date = normalize_date(args.exp, "expiry date")
            values = {
                "gtin": normalize_gtin(args.gtin),
                "serial": normalize_serial(args.serial),
                "mfg": mfg,
                "expiry": expiry,
                "code": normalize_code(args.code),
            }
        except FieldError as exc:
            sys.exit(f"g7scan: {exc}")
        check_dates(mfg_date, expiry_date)
        return values

    print(LABEL_MAP)
    print("Enter the printed values.  Nothing is inferred: every field is used")
    print("exactly as you type it.")
    values = {}
    parsed = [ask(field) for field in FIELDS]
    values["gtin"], values["serial"] = parsed[0], parsed[1]
    (values["mfg"], mfg_date), (values["expiry"], expiry_date) = parsed[2], parsed[3]
    values["code"] = parsed[4]
    check_dates(mfg_date, expiry_date)
    return values


def check_dates(mfg_date: date, expiry_date: date) -> None:
    if expiry_date < mfg_date:
        sys.exit(
            "g7scan: the right-hand date is earlier than the left-hand one.\n"
            "        The left date is AI (11) and the right one is AI (17);\n"
            "        if your label prints them the other way round, enter them swapped."
        )


LABEL_MAP = r"""
Dexcom G7 label - the four values this tool needs
=================================================

     +-----------------------------------------------------+
     |   ########            Dexcom G7 Sensor             |
     |   #  ##  #                                          |
     |   #  ##  #                1234                      |  <-- 5) pairing code
     |   ########                                          |
     |                                                     |
     |                    2026-01-15    2026-10-15         |  <-- 3) 4) dates
     |                                                     |
     |   (01)00386270004871                                |  <-- 1) GTIN
     |   (21)123456789012                                  |  <-- 2) serial
     |   LBL-1001234 REV002                                |  (not in the symbol)
     +-----------------------------------------------------+

  The square block is the GS1 DataMatrix itself; these are the same bytes it
  carries, printed as text.  LBL-/REV numbers are not part of the symbol.
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="g7scan",
        description="Rebuild a Dexcom G7 applicator barcode from the label text "
                    "and draw a small QR code in the terminal, ready to scan with Juggluco.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=LABEL_MAP + """
examples:
  g7scan.py                                  ask for every field, draw a small QR
  g7scan.py --scale 2                        draw it bigger
  g7scan.py --png g7.png                     also save it as a PNG
  g7scan.py --symbology datamatrix           the applicator's own symbol form
  g7scan.py --gtin 00386270004871 --serial 123456789012 \\
            --mfg 2026-01-15 --exp 2026-10-15 --code 1234
""",
    )
    parser.add_argument("--gtin", help="AI (01), 14 digits")
    parser.add_argument("--serial", help="AI (21) value")
    parser.add_argument("--mfg", help="AI (11) date, YYYY-MM-DD or YYMMDD")
    parser.add_argument("--exp", help="AI (17) date, YYYY-MM-DD or YYMMDD")
    parser.add_argument("--code", help="AI (240), the 4-character pairing code")
    parser.add_argument("--symbology", choices=sorted(SYMBOLOGY_IDS),
                        default="qr",
                        help="qr (small, default) or datamatrix (the applicator's own form)")
    parser.add_argument("--scale", type=int,
                        help="cells per module (default: 1, small; use 2-3 if it will not read)")
    parser.add_argument("--border", type=int, default=4,
                        help="quiet zone in modules (default: 4)")
    parser.add_argument("--png", metavar="FILE",
                        help="also write the symbol to FILE as a PNG")
    parser.add_argument("--png-scale", type=int, default=16, metavar="PX",
                        help="pixels per module for --png (default: 16)")
    parser.add_argument("--no-color", action="store_true",
                        help="do not force a white background (only for a light terminal)")
    parser.add_argument("--no-show", action="store_true",
                        help="do not open the fullscreen window (the terminal block is drawn anyway)")
    args = parser.parse_args(argv)

    missing = [f for f in ("gtin", "serial", "mfg", "exp", "code")
               if getattr(args, f) is None]
    if missing and len(missing) != 5:
        parser.error("give all of --gtin --serial --mfg --exp --code, or none of them")

    values = collect(args)

    payload = build_payload(**values)
    ai_text = build_ai_text(**values)

    modules = zint_matrix(ai_text, args.symbology)
    matrix = pad_matrix(modules, args.border)
    scale = max(1, args.scale or 1)
    lines = render_blocks(matrix, scale)

    if args.png:
        write_png(matrix, args.png, max(1, args.png_scale or 16))

    columns = len(lines[0]) if lines else 0
    terminal = shutil.get_terminal_size((100, 30)).columns

    print()
    print(f"symbol   : {args.symbology}  ({len(modules[0])} modules, "
          f"{args.border}-module quiet zone, drawn {scale}x)")
    print(f"payload  : {payload.replace(GS, '<GS>')}")
    print()
    if columns >= terminal:
        if scale > 1:
            hint = f"use --scale {scale - 1}"
        else:
            hint = "widen the terminal"
        print(f"! the symbol is {columns} columns wide but this terminal is {terminal} "
              f"columns - {hint}, or it will wrap and will not scan.")

    if not args.no_color:
        sys.stdout.write(PAPER)
    sys.stdout.write("\n".join(lines))
    sys.stdout.write(RESET if not args.no_color else "")
    sys.stdout.write("\n")

    print()
    print("Scan it with Juggluco: left menu -> Photo, fill the frame with the code,")
    print("hold still a moment.  Then accept the Bluetooth pairing prompt quickly")
    print("(about 5 seconds, or you wait another 5 minutes) and keep the screen on.")
    if not args.no_show:
        show_code(matrix, f"Dexcom G7 {values['serial']} - pairing code {values['code']}")
    return 0


def write_png(matrix: list[list[bool]], path: str, module_px: int) -> None:
    """Write the symbol as a black-on-white PNG, quiet zone included.

    The PNG is for 100% viewing or printing.  Viewers that zoom out
    interpolate the modules into grey, which no detector can read, so the
    default size is deliberately small enough to need no zooming.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit("g7scan: Pillow is required (python3 -m pip install pillow)")
    width = len(matrix[0]) * module_px
    height = len(matrix) * module_px
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                draw.rectangle((x * module_px, y * module_px,
                                (x + 1) * module_px - 1, (y + 1) * module_px - 1),
                               fill=0)
    image.save(path)
    print(f"png      : {path} ({width}x{height} - view at 100%, zooming out blurs it)")


def show_code(matrix: list[list[bool]], title: str) -> None:
    """Open the symbol on a fullscreen white page, crisp at any size.

    A vector drawing on pure white is the display a phone scanner reads
    instantly: no viewer chrome, no zooming, no interpolation, and the whole
    page is quiet zone.  The page lives in /tmp and is not deleted, so the
    browser window keeps working after this script has exited.
    """
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("no display here - scan the block drawn above instead")
        return
    exe = next((b for b in ("chromium", "chromium-browser", "google-chrome",
                            "google-chrome-stable") if shutil.which(b)), None)
    if exe is None:
        print("no browser found - scan the block drawn above instead")
        return

    cells = len(matrix)
    rects = "".join(
        f'<rect x="{x}" y="{y}" width="1" height="1"/>'
        for y, row in enumerate(matrix)
        for x, dark in enumerate(row)
        if dark
    )
    page = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>{title}</title>"
        "<style>html,body{margin:0;height:100%;background:#fff}"
        "body{display:flex;align-items:center;justify-content:center;overflow:hidden}"
        "svg{height:94vh;max-width:94vw}</style>"
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {cells} {cells}"'
        ' shape-rendering="crispEdges">'
        f'<rect width="{cells}" height="{cells}" fill="#fff"/>'
        f'<g fill="#000">{rects}</g></svg>'
    )
    with open(SHOW_PAGE, "w", encoding="utf-8") as handle:
        handle.write(page)
    subprocess.Popen(
        [exe, f"--app=file://{SHOW_PAGE}", "--start-fullscreen",
         "--force-device-scale-factor=1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"showing : fullscreen window - {title}")
    print("          hold the phone 20-30 cm from the screen and fill the frame")


if __name__ == "__main__":
    sys.exit(main())
