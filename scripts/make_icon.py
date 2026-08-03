#!/usr/bin/env python3
"""Draw Hearth's application icon and write desktop/tauri/icons/icon.ico.

The .ico is committed, because a build should not depend on regenerating
artwork. This script is committed with it so the artwork is source rather
than a binary somebody has to open a paint program to change.

Standard library only, like everything else here: the image is rasterised
into a bytearray, encoded as PNG by hand (zlib plus four chunks), and packed
into an ICO directory. That is a few dozen lines and no dependency, which is
a better trade than adding an imaging library to a repository that has
otherwise managed without one.

Windows picks a different size from the .ico for each place it draws the
app -- 16 px in the title bar, 32 in the taskbar, 48 in Explorer's medium
view, 256 in the Alt-Tab switcher and the installer -- so every size is
rendered separately at its own resolution rather than scaled from one
bitmap. Each is drawn at 4x and box-filtered down, which is what keeps the
16 px version from turning into four brown pixels.

    python scripts/make_icon.py            write desktop/tauri/icons/icon.ico
    python scripts/make_icon.py --self-test
"""

import argparse
import math
import os
import struct
import sys
import zlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_PATH = os.path.join(REPO_ROOT, "desktop", "tauri", "icons", "icon.ico")

#: Sizes Windows actually asks for. 256 is the one the installer and the
#: Alt-Tab switcher use, and it is stored as a PNG inside the ICO (the
#: format allows it and every Windows since Vista reads it).
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Supersampling factor. 4x is the point where the flame's edge stops
#: staircasing at 16 px; 8x is indistinguishable and four times the work.
SS = 4

# Colours. Warm fire on a near-black ember background, which reads as an
# icon rather than as a screenshot at 16 px.
BG_TOP = (38, 26, 20)
BG_BOTTOM = (16, 12, 10)
FLAME_TOP = (255, 214, 130)
FLAME_MID = (255, 138, 61)
FLAME_BOTTOM = (214, 58, 34)
CORE_TOP = (255, 248, 226)
CORE_BOTTOM = (255, 186, 92)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _gradient(colors, t):
    """Sample a two or three stop vertical gradient at t in [0, 1]."""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    if len(colors) == 2:
        return _lerp(colors[0], colors[1], t)
    if t < 0.5:
        return _lerp(colors[0], colors[1], t * 2)
    return _lerp(colors[1], colors[2], (t - 0.5) * 2)


def _flame_half_width(y):
    """Half width of the flame at height `y`, in a unit square.

    y is 0 at the bottom of the flame and 1 at the tip. The shape is a
    circular bulb below the waist and a tapering tip above it, which is the
    smallest description that still reads as fire rather than as a leaf.
    """
    waist = 0.34
    radius = 0.30
    if y <= waist:
        # Lower bulb: a circle centred on the waist, so the widest point sits
        # a third of the way up rather than at the base.
        dy = (waist - y) / waist
        return radius * math.sqrt(max(0.0, 1.0 - dy * dy * 0.86))
    up = (y - waist) / (1.0 - waist)
    return radius * (1.0 - up) ** 0.62


def _flame_lean(y):
    """How far the flame's centreline drifts at height `y`.

    A perfectly symmetrical flame looks like a lightbulb. A small lean, back
    towards the middle at the tip, is what gives it motion.
    """
    waist = 0.34
    if y <= waist:
        return 0.0
    up = (y - waist) / (1.0 - waist)
    return 0.085 * math.sin(math.pi * up ** 0.85)


def _inside_flame(x, y, scale=1.0, lift=0.0):
    """True when unit-square point (x, y) is inside the flame body."""
    if scale != 1.0:
        # Shrink about the base so the inner core sits inside the outer
        # flame at every height rather than poking out of the tip.
        y = (y - lift) / scale
        if y < 0.0 or y > 1.0:
            return False
        x = x / scale
    if y < 0.0 or y > 1.0:
        return False
    return abs(x - _flame_lean(y)) <= _flame_half_width(y)


def _rounded(x, y, size, radius):
    """True when pixel (x, y) is inside a rounded square of side `size`."""
    r = radius
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def render(size):
    """Render one RGBA image of `size` pixels a side. Returns bytes."""
    big = size * SS
    radius = big * 0.22
    # Accumulate at supersampled resolution, then box filter. Straight
    # averaging of premultiplied-by-coverage samples; the background is
    # opaque inside the rounded square, so the only alpha edge is the
    # square's own corner and this stays correct there.
    acc = [[0, 0, 0, 0] for _ in range(size * size)]

    flame_bottom = 0.14
    flame_height = 0.76

    for py in range(big):
        y_px = py + 0.5
        for px in range(big):
            x_px = px + 0.5
            if not _rounded(x_px, y_px, big, radius):
                continue
            ty = y_px / big
            r, g, b = _gradient((BG_TOP, BG_BOTTOM), ty)

            # Flame coordinates: x centred, y measured up from the flame's
            # base as a fraction of its height.
            fx = (x_px / big) - 0.5
            fy = (1.0 - ty - flame_bottom) / flame_height
            if _inside_flame(fx / 1.0, fy):
                r, g, b = _gradient((FLAME_TOP, FLAME_MID, FLAME_BOTTOM), 1.0 - fy)
                if _inside_flame(fx, fy, scale=0.52, lift=0.05):
                    r, g, b = _gradient((CORE_TOP, CORE_BOTTOM), 1.0 - fy)

            cell = acc[(py // SS) * size + (px // SS)]
            cell[0] += r
            cell[1] += g
            cell[2] += b
            cell[3] += 255

    samples = SS * SS
    out = bytearray(size * size * 4)
    for i, cell in enumerate(acc):
        alpha = cell[3] // samples
        if alpha == 0:
            continue
        # Divide colour by the number of COVERED samples, not by all of
        # them, or the rounded corners fade to black instead of to
        # transparent.
        covered = cell[3] / 255.0
        out[i * 4 + 0] = min(255, int(cell[0] / covered))
        out[i * 4 + 1] = min(255, int(cell[1] / covered))
        out[i * 4 + 2] = min(255, int(cell[2] / covered))
        out[i * 4 + 3] = alpha
    return bytes(out)


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png(rgba, size):
    """Encode raw RGBA rows as a PNG. Filter type 0 on every scanline."""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def ico(images):
    """Pack {size: png bytes} into an ICO file."""
    entries = sorted(images.items())
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = len(header) + 16 * len(entries)
    directory = b""
    body = b""
    for size, data in entries:
        # 256 is stored as 0 in the one-byte width and height fields.
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        body += data
    return header + directory + body


def build(path=None):
    path = path or ICON_PATH
    images = {size: png(render(size), size) for size in SIZES}
    blob = ico(images)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(blob)
    return path, len(blob)


def _self_test():
    # The flame is a closed shape: solid on the centreline, empty outside,
    # and it tapers rather than widening.
    assert _inside_flame(0.0, 0.3)
    assert not _inside_flame(0.9, 0.3)
    assert not _inside_flame(0.0, 1.6)
    assert _flame_half_width(0.9) < _flame_half_width(0.34)
    assert _flame_half_width(0.05) < _flame_half_width(0.34)

    # The core is inside the flame everywhere, not poking out of the tip.
    for i in range(1, 100):
        y = i / 100.0
        if _inside_flame(0.0, y, scale=0.52, lift=0.05):
            assert _inside_flame(0.0, y), y

    size = 32
    rgba = render(size)
    assert len(rgba) == size * size * 4
    # Corners are transparent (rounded square), the middle is not.
    assert rgba[3] == 0, "the top-left corner should be outside the rounded square"
    mid = ((size // 2) * size + (size // 2)) * 4
    assert rgba[mid + 3] == 255, "the middle of the icon should be opaque"
    # There is actually fire in there: some pixel is strongly warm.
    warm = max(rgba[i] - rgba[i + 2] for i in range(0, len(rgba), 4))
    assert warm > 100, "the icon has no warm pixels, so it has no flame"

    blob = ico({size: png(rgba, size)})
    assert blob[:4] == b"\x00\x00\x01\x00", "ICO magic"
    count = struct.unpack("<H", blob[4:6])[0]
    assert count == 1
    length, offset = struct.unpack("<II", blob[14:22])
    assert blob[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
    assert offset + length == len(blob)

    # A 256 entry records its size as 0, which is the format's convention
    # and the thing that silently truncates to a 0x0 icon if forgotten.
    big = ico({256: b"x" * 10})
    assert big[6] == 0 and big[7] == 0
    print("hearth-make-icon self-test OK")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="make_icon.py", description="Draw Hearth's icon.")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--out", default=None, help="where to write the .ico")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return _self_test()
    path, size = build(args.out)
    print("wrote {} ({} bytes)".format(path, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
