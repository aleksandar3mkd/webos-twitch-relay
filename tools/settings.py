"""Where your server and TV are, for the tools in this folder.

Each value comes from the command line, else from the environment, else from a `.env` file
in the project folder (copy `.env.example`). Nothing here is needed to run the server itself."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def setting(name, default=None):
    if os.environ.get(name):
        return os.environ[name]
    try:
        lines = (ROOT / '.env').read_text().splitlines()
    except OSError:
        return default
    for line in lines:
        key, found, value = line.partition('=')
        if found and key.strip() == name and not line.lstrip().startswith('#'):
            return value.split(' #')[0].strip().strip('"\'') or default
    return default
