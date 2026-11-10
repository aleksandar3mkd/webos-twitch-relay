#!/usr/bin/env python3
"""Generate simple native launcher icons and package the app using LG's CLI.

The TV app opens your server's address. Give it with --server or MY_STREAMS_URL (see .env.example);
without one the app asks for it on the TV the first time."""
import argparse
import json
from pathlib import Path
import re
import struct
import subprocess
import zlib

from settings import setting

ROOT = Path(__file__).resolve().parent.parent


def icon(size):
    # Original geometric play icon, drawn in code; no third-party branding.
    pixels = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            u, v = x / size, y / size
            inside = 0.34 <= u <= 0.73 and abs(v - 0.5) <= (0.73 - u) * 0.78
            row.extend((240, 234, 255) if inside else (51, 62, 104))
        pixels.append(bytes(row))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(pixels))) + chunk(b'IEND', b''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default=setting('MY_STREAMS_URL', ''),
                        help='Address of your My Streams server, like http://192.168.1.50:8765')
    args = parser.parse_args()
    server = args.server.strip().rstrip('/')
    # The TV app loads the player over plain HTTP on your home network.
    if server and not re.fullmatch(r'http://[a-zA-Z0-9.-]+(:[0-9]{1,5})?', server):
        raise SystemExit('The server address must look like http://192.168.1.50:8765')
    app = ROOT / 'webos'
    (app / 'server.js').write_text('var MY_STREAMS_SERVER = %s;\n' % json.dumps(server))
    print('Server address in the TV app:', server or '(none: the app will ask on the TV)')
    (app / 'icon.png').write_bytes(icon(80))
    (app / 'largeIcon.png').write_bytes(icon(130))
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    cli = ROOT / 'node_modules/.bin/ares-package'
    if not cli.exists():
        raise SystemExit('First run npm ci to install the LG packaging tool.')
    subprocess.run([str(cli), '--check', str(app)], check=True)
    subprocess.run([str(cli), str(app), '--outdir', str(output)], check=True)
    info = json.loads((app / 'appinfo.json').read_text())
    print('Package ready:', output / (info['id'] + '_' + info['version'] + '_all.ipk'))


if __name__ == '__main__':
    main()
