"""Convert the emulator's PBM frame dumps to PNG (stdlib only).

usage: python3 -m quine.png DIR [scale]   converts every DIR/*.pbm to .png
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ON = (120, 255, 140)  # lit pixel
OFF = (8, 14, 10)


def read_pbm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    parts = data.split(b"\n", 2)
    assert parts[0] == b"P4"
    w, h = map(int, parts[1].split())
    return w, h, parts[2]


def write_png(path: Path, w: int, h: int, bits: bytes, scale: int = 2):
    stride = (w + 7) // 8
    rows = []
    for y in range(h):
        line = bytearray()
        for x in range(w):
            on = (bits[y * stride + x // 8] >> (7 - x % 8)) & 1
            line += bytes(ON if on else OFF) * scale
        rows.extend([b"\x00" + bytes(line)] * scale)
    raw = b"".join(rows)

    def chunk(tag, payload):
        c = tag + payload
        return struct.pack(">I", len(payload)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w * scale, h * scale, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main(argv):
    d = Path(argv[1])
    scale = int(argv[2]) if len(argv) > 2 else 2
    for p in sorted(d.glob("*.pbm")):
        w, h, bits = read_pbm(p)
        write_png(p.with_suffix(".png"), w, h, bits, scale)
    print(f"converted {len(list(d.glob('*.pbm')))} frames in {d}")


if __name__ == "__main__":
    main(sys.argv)
