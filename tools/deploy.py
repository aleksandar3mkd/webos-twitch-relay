#!/usr/bin/env python3
"""Build and deploy the committed source from this computer to your server over SSH.

For people who edit the code on one computer and run it on another. The server must have been
set up once (see "Install the server" in the README). Give the server with --host or MY_STREAMS_SSH."""
import argparse
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile

from settings import setting

ROOT = Path(__file__).resolve().parent.parent


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=setting('MY_STREAMS_SSH', ''), help='SSH destination, like you@your-server')
    parser.add_argument('--dry-run', action='store_true', help='Show the revision and files without deploying')
    args = parser.parse_args()
    if not args.host:
        raise SystemExit('Say where to deploy: --host you@your-server, or MY_STREAMS_SSH in .env (see .env.example).')
    if args.host.startswith('-'):
        raise SystemExit('Invalid SSH destination')
    folder = setting('MY_STREAMS_DIR', 'apps/my-streams')
    address = setting('MY_STREAMS_URL', 'http://<your-server>:8765').rstrip('/')
    if git('status', '--porcelain'):
        raise SystemExit('Commit your source changes before deploying. Dependencies and dist/ are ignored.')
    revision = git('rev-parse', 'HEAD')
    info = json.loads((ROOT / 'webos/appinfo.json').read_text())
    package_name = info['id'] + '_' + info['version'] + '_all.ipk'
    print('Target:', args.host, '~/' + folder, flush=True)
    print('Revision:', revision, flush=True)
    if args.dry_run:
        print('Committed source files:', len(git('ls-files').splitlines()))
        print('Generated package:', package_name)
        return
    subprocess.run([sys.executable, str(ROOT / 'tools/build_tv_app.py')], cwd=ROOT, check=True)
    if git('status', '--porcelain'):
        raise SystemExit('The build changed tracked files. Review and commit them before deploying.')
    source = subprocess.check_output(['git', 'archive', '--format=tar', revision], cwd=ROOT)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as destination:
        with tarfile.open(fileobj=io.BytesIO(source), mode='r:') as archive:
            for member in archive:
                destination.addfile(member, archive.extractfile(member) if member.isfile() else None)
        destination.add(ROOT / 'dist' / package_name, arcname='dist/' + package_name)
    # The folder is quoted; the SSH destination is passed as a separate argv item.
    command = '''set -eu
cd "$HOME"/%s
''' % shlex.quote(folder) + '''
test -x .venv/bin/python
tar -xzf -
.venv/bin/python -m pip install -r requirements-server.txt
.venv/bin/python -m py_compile server.py
mkdir -p "$HOME/.config/systemd/user"
cp deploy/my-streams.service "$HOME/.config/systemd/user/my-streams.service"
systemctl --user daemon-reload
systemctl --user restart my-streams.service
.venv/bin/python -c 'import json,time,urllib.request
for attempt in range(40):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health",timeout=2) as response:
            assert json.load(response)["ok"]
        break
    except Exception:
        if attempt == 39: raise
        time.sleep(0.5)
print("Health check passed.")'
'''
    command += '\nprintf "%s\\n" ' + shlex.quote(revision) + ' > .runtime/DEPLOYED_COMMIT\n'
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                    '-o', 'StrictHostKeyChecking=yes', args.host, command],
                   input=buffer.getvalue(), cwd=ROOT, check=True)
    print('Deployed:', revision, flush=True)
    print('Player:', address + '/')
    print('TV package:', address + '/downloads/my-streams.ipk')
    print('Reopen My Streams on the TV to load the updated player.')


if __name__ == '__main__':
    main()
