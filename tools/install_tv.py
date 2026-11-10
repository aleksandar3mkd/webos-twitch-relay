#!/usr/bin/env python3
"""Build and install the launcher using an already-paired LG CLI device."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from settings import setting

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', default=setting('MY_STREAMS_TV', 'tv'), help='Previously paired LG CLI device name')
    parser.add_argument('--server', default=setting('MY_STREAMS_URL', ''),
                        help='Address of your My Streams server, like http://192.168.1.50:8765')
    args = parser.parse_args()
    if args.device.startswith('-'):
        raise SystemExit('Invalid device name')
    subprocess.run([sys.executable, str(ROOT / 'tools/build_tv_app.py'), '--server', args.server], cwd=ROOT, check=True)
    info = json.loads((ROOT / 'webos/appinfo.json').read_text())
    package = ROOT / 'dist' / (info['id'] + '_' + info['version'] + '_all.ipk')
    cli = ROOT / 'node_modules/.bin'
    subprocess.run([str(cli / 'ares-launch'), '--device', args.device, '--close', info['id']],
                   cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([str(cli / 'ares-install'), '--device', args.device, str(package)], cwd=ROOT, check=True)
    subprocess.run([str(cli / 'ares-launch'), '--device', args.device, info['id']], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
