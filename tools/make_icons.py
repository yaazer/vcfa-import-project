#!/usr/bin/env python3
"""The app icon: a "V" drops the VM into the automation platform.

One geometry on the proof's 64-unit grid, in two cuts:
  full   -- 40 px and up: V, open hexagon, the VM with its two bars
  small  -- 32 px and down: heavier strokes, a solid VM, no bars

This script writes the static SVGs the console serves (the favicon and the
master) and, for the Windows build, an .ico rasterised here with the standard
library only. appIcon() in core.js draws the same shapes inline so the console
can recolour them per theme -- keep the numbers in step.

    python tools/make_icons.py                     # rewrite the static SVGs
    python tools/make_icons.py --ico out.ico       # + a multi-size .ico
    python tools/make_icons.py --png out.png 512   # a PNG, to compare with the proof
"""
from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "vcfaimport" / "web" / "static"

# Colourways from the proof. Navy is the default.
VARIANTS = {
    "navy":  {"tile": "#0E2F4E", "v": "#49AFD9", "hex": "#FFFFFF", "vm": "#49AFD9", "bar": "#0E2F4E"},
    "blue":  {"tile": "#0072A3", "v": "#A8E1FA", "hex": "#FFFFFF", "vm": "#A8E1FA", "bar": "#0072A3"},
    "light": {"tile": "#FFFFFF", "v": "#0072A3", "hex": "#0E2F4E", "vm": "#0E2F4E", "bar": "#FFFFFF"},
    "mono":  {"tile": "#26292E", "v": "#FFFFFF", "hex": "#FFFFFF", "vm": "#FFFFFF", "bar": "#26292E"},
}

TILE_R = 14
CUTS = {
    "full": {
        "hex": ([(25, 22.4), (15, 27.5), (15, 45.5), (32, 55), (49, 45.5), (49, 27.5), (39, 22.4)], 3.5),
        "v": ([(21, 11), (32, 23), (43, 11)], 4.0),
        "vm": (24.5, 31, 15, 14, 3),
        "bars": ([((28, 35.2), (36, 35.2)), ((28, 40.6), (36, 40.6))], 1.9),
    },
    "small": {
        "hex": ([(24, 23), (13.5, 28), (13.5, 46), (32, 56.5), (50.5, 46), (50.5, 28), (40, 23)], 5.2),
        "v": ([(19.5, 9.5), (32, 23), (44.5, 9.5)], 5.6),
        "vm": (23.5, 31, 17, 16, 3.5),
        "bars": ([], 0),
    },
}


def svg(cut: str = "full", variant: str = "navy") -> str:
    g, c = CUTS[cut], VARIANTS[variant]
    pts = lambda ps: " ".join("{:g} {:g}".format(x, y) for x, y in ps)
    hex_pts, hex_w = g["hex"]
    v_pts, v_w = g["v"]
    x, y, w, h, r = g["vm"]
    bars, bar_w = g["bars"]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">',
        '<rect width="64" height="64" rx="{}" fill="{}"/>'.format(TILE_R, c["tile"]),
        '<g fill="none" stroke-linecap="round" stroke-linejoin="round">',
        '<path d="M{}" stroke="{}" stroke-width="{:g}"/>'.format(pts(hex_pts), c["hex"], hex_w),
        '<path d="M{}" stroke="{}" stroke-width="{:g}"/>'.format(pts(v_pts), c["v"], v_w),
        '</g>',
        '<rect x="{:g}" y="{:g}" width="{:g}" height="{:g}" rx="{:g}" fill="{}"/>'.format(x, y, w, h, r, c["vm"]),
    ]
    if bars:
        parts.append('<path d="{}" stroke="{}" stroke-width="{:g}" stroke-linecap="round"/>'.format(
            " ".join("M{:g} {:g}H{:g}".format(a[0], a[1], b[0]) for a, b in bars), c["bar"], bar_w))
    parts.append("</svg>")
    return "".join(parts) + "\n"


# ------------------------------------------------------------------ raster
def _hex(c: str) -> tuple:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _in_rrect(px, py, x, y, w, h, r):
    if px < x or py < y or px > x + w or py > y + h:
        return False
    cx = min(max(px, x + r), x + w - r)
    cy = min(max(py, y + r), y + h - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def render(size: int, cut: str | None = None, variant: str = "navy", ss: int = 4) -> bytes:
    """RGBA pixels, supersampled: round caps and joins are just distance to segments."""
    cut = cut or ("small" if size <= 32 else "full")
    g, col = CUTS[cut], {k: _hex(v) for k, v in VARIANTS[variant].items()}
    hex_pts, hex_w = g["hex"]
    v_pts, v_w = g["v"]
    bars, bar_w = g["bars"]
    segs = lambda ps: list(zip(ps, ps[1:]))
    shapes = [  # painter's order: (colour, inside(x, y))
        (col["tile"], lambda x, y: _in_rrect(x, y, 0, 0, 64, 64, TILE_R)),
        (col["hex"], lambda x, y, s=segs(hex_pts): any(_seg_dist(x, y, *a, *b) <= hex_w / 2 for a, b in s)),
        (col["v"], lambda x, y, s=segs(v_pts): any(_seg_dist(x, y, *a, *b) <= v_w / 2 for a, b in s)),
        (col["vm"], lambda x, y: _in_rrect(x, y, *g["vm"])),
        (col["bar"], lambda x, y: any(_seg_dist(x, y, *a, *b) <= bar_w / 2 for a, b in bars)),
    ]
    out = bytearray()
    k = 64.0 / size
    offs = [(i + 0.5) / ss for i in range(ss)]
    for j in range(size):
        for i in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]         # premultiplied
            for oy in offs:
                for ox in offs:
                    x, y = (i + ox) * k, (j + oy) * k
                    rgb = None
                    for c, inside in shapes:
                        if inside(x, y):
                            rgb = c
                    if rgb:
                        acc[0] += rgb[0]; acc[1] += rgb[1]; acc[2] += rgb[2]; acc[3] += 1
            n = ss * ss
            a = acc[3] / n
            if a:
                out += bytes((round(acc[0] / acc[3]), round(acc[1] / acc[3]), round(acc[2] / acc[3]), round(a * 255)))
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def png(size: int, rgba: bytes) -> bytes:
    raw = b"".join(b"\0" + rgba[r * size * 4:(r + 1) * size * 4] for r in range(size))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(sizes=(16, 24, 32, 48, 64, 128, 256)) -> bytes:
    """A Vista-style .ico: every size a PNG."""
    images = [png(s, render(s)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + entries + b"".join(images)


def write_static() -> list:
    written = []
    for name, text in (("appicon.svg", svg("full")), ("favicon.svg", svg("small"))):
        path = STATIC / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8", newline="\n")
            written.append(path)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ico", help="also write a multi-size .ico here")
    ap.add_argument("--png", nargs=2, metavar=("PATH", "SIZE"), help="also write one PNG")
    ap.add_argument("--variant", default="navy", choices=sorted(VARIANTS))
    args = ap.parse_args()
    for p in write_static():
        print("  wrote", p.relative_to(ROOT))
    if args.ico:
        Path(args.ico).write_bytes(ico())
        print("  wrote", args.ico)
    if args.png:
        s = int(args.png[1])
        Path(args.png[0]).write_bytes(png(s, render(s, variant=args.variant)))
        print("  wrote", args.png[0])


if __name__ == "__main__":
    main()
