#!/usr/bin/env python3
"""Draw the web app icons (purple gradient, white play symbol) as PNGs, without image libraries."""
from pathlib import Path
import struct
import zlib

ROOT = Path(__file__).resolve().parent.parent
TOP, BOTTOM = (155, 123, 255), (91, 63, 224)      # the player's logo colours
SAMPLES = 4                                       # per axis, for smooth edges


def inside_play(u, v, scale):
    # A rounded-looking play triangle, nudged right so it looks centred.
    left, right, half = 0.5 - 0.17 * scale, 0.5 + 0.21 * scale, 0.22 * scale
    return left <= u <= right and abs(v - 0.5) <= (right - u) / (right - left) * half


def icon(size, scale=1.0):
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            t = (x + y) / (2 * size)                  # diagonal gradient
            base = [round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)]
            hits = sum(inside_play((x + (i + .5) / SAMPLES) / size, (y + (j + .5) / SAMPLES) / size, scale)
                       for i in range(SAMPLES) for j in range(SAMPLES))
            alpha = hits / SAMPLES ** 2
            row.extend(round(c + (255 - c) * alpha) for c in base)
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows), 9)) + chunk(b'IEND', b''))


def main():
    folder = ROOT / 'public' / 'icons'
    folder.mkdir(parents=True, exist_ok=True)
    for name, size, scale in (('apple-touch-icon.png', 180, 1.0), ('icon-192.png', 192, 1.0),
                              ('icon-512.png', 512, 1.0), ('icon-maskable-512.png', 512, 0.8),
                              ('favicon-32.png', 32, 1.15)):
        (folder / name).write_bytes(icon(size, scale))
        print('wrote', folder / name)


if __name__ == '__main__':
    main()
