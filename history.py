"""What one person watched and how far they got, kept on the server so every device shares it."""
import hashlib
import json
import os
import threading
import time

LIMIT = 500             # oldest entries drop off after this many videos
CONTINUE = 12           # videos in the "Continue watching" row
STARTED_AFTER = 30      # seconds in before a video counts as started
SAVE_EVERY = 30         # progress while playing is written at most this often


def key_of(url):
    """One entry per video. The server only ever passes a link in its one tidy form."""
    return str(url or '')


def finished_at(position, duration):
    """The end credits do not need watching: the last minute (or tenth of a short video) counts as finished."""
    return bool(duration) and duration - position < min(60, duration * 0.1)


class WatchHistory:
    def __init__(self, path, images):
        self.path = path
        self.images = images
        self.lock = threading.Lock()
        self.changes = 0
        self.saved = 0
        try:
            self.entries = list(json.loads(path.read_text()).get('videos') or [])
        except (OSError, ValueError, AttributeError):
            self.entries = []

    def save(self):
        """Callers hold the lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'videos': self.entries}))
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        self.saved = time.time()

    def find(self, url):
        key = key_of(url)
        return next((entry for entry in self.entries if entry['key'] == key), None)

    def started(self, url, info, stream=None):
        """A video began playing (info: what yt-dlp found). Moves it to the top and keeps its position."""
        if not url:
            return
        stream = stream or {}
        thumb = info.get('thumbnail')
        with self.lock:
            old = self.find(url) or {}
            entry = {'key': key_of(url), 'url': url,
                     'title': str(info.get('title') or stream.get('title') or old.get('title') or '')[:150],
                     'name': str(info.get('channel') or info.get('uploader') or stream.get('name')
                                 or old.get('name') or '')[:80],
                     'platform': stream.get('platform') or old.get('platform') or 'Video',
                     'duration': info.get('duration') or old.get('duration'),
                     'thumb': thumb or old.get('thumb'),
                     'position': old.get('position', 0), 'finished': old.get('finished', False),
                     'watched': int(time.time())}
            self.entries = [entry] + [e for e in self.entries if e['key'] != entry['key']][:LIMIT - 1]
            self.save()
            self.changes += 1

    def progress(self, url, position, duration=None, event='tick'):
        """A screen reports how far it is. Returns False for videos that are not in the history."""
        try:
            position = max(0, int(float(position)))
            duration = int(float(duration)) if duration else None
        except (TypeError, ValueError):
            raise ValueError('Position must be a number of seconds.')
        with self.lock:
            entry = self.find(url)
            if not entry:
                return False
            if duration and duration > 0 and not entry.get('duration'):
                entry['duration'] = duration
            total = entry.get('duration')
            if total:
                position = min(position, total)
            finished = event == 'ended' or finished_at(position, total)
            changed = finished != entry.get('finished')
            if finished:
                entry['finished'], entry['position'] = True, 0
            elif position >= STARTED_AFTER or event != 'tick':
                # Watching it again from the start: it is in progress again, not finished.
                entry['finished'], entry['position'] = False, position
            entry['watched'] = int(time.time())
            if changed or event != 'tick':
                self.changes += 1     # the library shows the new spot
            if changed or event != 'tick' or time.time() - self.saved > SAVE_EVERY:
                self.save()
            return True

    def position(self, url):
        """Where to continue, or None when this video was never watched here."""
        with self.lock:
            entry = self.find(url)
            return entry['position'] if entry else None

    def remove(self, url):
        with self.lock:
            key = key_of(url)
            if not any(entry['key'] == key for entry in self.entries):
                raise ValueError('That video is not in your history.')
            self.entries = [entry for entry in self.entries if entry['key'] != key]
            self.save()
            self.changes += 1

    def clear(self):
        with self.lock:
            self.entries = []
            self.save()
            self.changes += 1

    def card(self, entry):
        """The entry as the library draws it."""
        name = 'h-' + hashlib.sha1(entry['key'].encode()).hexdigest()[:12]
        duration = entry.get('duration')
        return {'id': name, 'url': entry['url'], 'name': entry.get('name') or entry.get('platform') or 'Video',
                'platform': entry.get('platform'), 'kind': 'video', 'title': entry.get('title'),
                'duration': duration, 'position': entry.get('position', 0), 'finished': entry.get('finished', False),
                'progress': progress_of(entry), 'watched': entry.get('watched'), 'source': 'history',
                'thumb': self.images.register(name, entry.get('thumb'), 1)}

    def items(self, limit=LIMIT):
        with self.lock:
            entries = list(self.entries[:limit])
        return [self.card(entry) for entry in entries]

    def continuing(self):
        """Started but not finished, newest first."""
        with self.lock:
            entries = [e for e in self.entries if not e.get('finished') and e.get('position', 0) >= STARTED_AFTER]
        return [self.card(entry) for entry in entries[:CONTINUE]]


def progress_of(entry):
    """How much of it was watched, 0 to 1 (finished videos count as all)."""
    if entry.get('finished'):
        return 1
    duration = entry.get('duration')
    return round(min(1, entry.get('position', 0) / duration), 3) if duration else 0
