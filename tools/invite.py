#!/usr/bin/env python3
"""Print a sign-in link for the server owner.

The server only hands out owner invites to requests from the server itself. Run this on the server,
or from another computer with --host (or MY_STREAMS_SSH) to ask it over SSH.
"""
import argparse
import json
import shlex
import subprocess
import sys

from settings import setting

ASK = ('import json, urllib.request; '
       'print(json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/owner-invite", timeout=10))["code"])')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=setting('MY_STREAMS_SSH', ''),
                        help='SSH destination of the server; leave out when you run this on the server')
    parser.add_argument('--address', default=setting('MY_STREAMS_URL', ''),
                        help='Address people use to open My Streams, like http://192.168.1.50:8765')
    parser.add_argument('--port', type=int, default=8765, help='Port the server listens on, on the server')
    args = parser.parse_args()
    if args.host.startswith('-'):
        raise SystemExit('Invalid SSH destination')
    ask = ASK % args.port
    if args.host:
        folder = setting('MY_STREAMS_DIR', 'apps/my-streams')
        command = 'cd "$HOME"/%s && .venv/bin/python -c %s' % (shlex.quote(folder), shlex.quote(ask))
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', args.host, command]
    else:
        command = [sys.executable, '-c', ask]
    code = subprocess.check_output(command, text=True).strip()
    print('Open this on your phone or computer to sign in as the owner (valid for 7 days):')
    print((args.address or 'http://<your-server>:%d' % args.port).rstrip('/') + '/join/' + code)


if __name__ == '__main__':
    main()
