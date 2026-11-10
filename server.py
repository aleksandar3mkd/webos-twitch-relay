#!/usr/bin/env python3
"""Twitch for an old LG webOS TV: live channels, past broadcasts and followed channels, relayed over the home network."""
import argparse
import atexit
from collections import OrderedDict
import gzip
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
import html
import io
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

import accounts
import history
from users import Users, clean_code, show_code

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'
MEDIA = RUNTIME / 'media'
DATA = Path(os.environ.get('MY_STREAMS_DATA') or ROOT / 'data')
MAX_STREAMS = 3              # streams the server relays at the same time (one per person watching)
PUBLIC = False               # set by --public when the server is reachable from the internet (behind HTTPS)
COOKIE = 'ms_device'
# Everything is fetched over IPv4, the way this was tested. --allow-ipv6 is for a server without IPv4.
IPV4_ONLY = True


def ipv4_args(flag):
    return [flag] if IPV4_ONLY else []


def use_ipv4_only():
    """Make this process's own web requests (live checks, followed channels, previews) use IPv4."""
    import urllib3.util.connection
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
# Encoding presets for the TV. Sources are never upscaled: a 720p stream stays 720p.
QUALITIES = {
    '720p': dict(streams='720p,720p60,480p,best', width=1280, height=720, fps=30,
                 bitrate=2200, preset='ultrafast', profile='main', level='3.1'),
    '1080p': dict(streams='1080p60,1080p,best', width=1920, height=1080, fps=30,
                  bitrate=5000, preset='veryfast', profile='high', level='4.0'),
    '1080p60': dict(streams='1080p60,1080p,best', width=1920, height=1080, fps=60,
                    bitrate=7500, preset='veryfast', profile='high', level='4.2'),
}
DEFAULT_QUALITY = '1080p'
CHECK_INTERVAL = 120          # seconds between live checks of the whole library, while the app is in use
# The server only works in the background (live checks, followed channels) while someone uses the app.
IN_USE_FOR = 600              # seconds after a screen last asked for something
STALE_AFTER = 1800            # live results older than this are checked again before they are shown as current
ABANDONED_AFTER = 900         # a stream that no screen plays or asks about stops by itself
TOKEN = secrets.token_urlsafe(24)
INSTANCE_ID = secrets.token_hex(12)
FFMPEG = shutil.which('ffmpeg')
if not FFMPEG:
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        FFMPEG = get_ffmpeg_exe()
    except ImportError:
        pass
STREAMLINK = str(Path(sys.executable).parent / 'streamlink')


TWITCH_NAME = re.compile(r'[A-Za-z0-9_]{1,25}')
# twitch.tv paths that look like channel names but are site pages.
TWITCH_PAGES = {'directory', 'videos', 'settings', 'downloads', 'search', 'subscriptions',
                'inventory', 'wallet', 'drops', 'prime', 'turbo', 'jobs', 'p', 'login', 'signup'}
TWITCH_HOSTS = ('twitch.tv', 'www.twitch.tv', 'm.twitch.tv')


class Settings:
    """Player settings shared by every screen."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        self.quality = data.get('quality') if data.get('quality') in QUALITIES else DEFAULT_QUALITY

    def set_quality(self, quality):
        if quality not in QUALITIES:
            raise ValueError('Choose one of: ' + ', '.join(QUALITIES))
        with self.lock:
            self.quality = quality
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'quality': quality}))
            os.replace(temporary, self.path)


def encoder_args(quality):
    q = QUALITIES[quality]
    rate = q['bitrate']
    scale = ("scale='min({width},iw)':'min({height},ih)':force_original_aspect_ratio=decrease"
             ":force_divisible_by=2,format=yuv420p").format(**q)
    return ['-vf', scale, '-r', str(q['fps']), '-c:v', 'libx264', '-preset', q['preset'],
            '-profile:v', q['profile'], '-level:v', q['level'], '-g', str(q['fps'] * 2),
            '-sc_threshold', '0', '-b:v', '%dk' % rate, '-maxrate', '%dk' % (rate * 1.25),
            '-bufsize', '%dk' % (rate * 2.5)]


def normalize_stream(value):
    """Turn a channel name or a pasted Twitch link into the one tidy form of its address."""
    value = str(value or '').strip()
    if not value:
        raise ValueError('Paste a Twitch link or a channel name.')
    if TWITCH_NAME.fullmatch(value):
        value = 'https://www.twitch.tv/' + value
    elif '://' not in value:
        value = 'https://' + value
    if len(value) > 500:
        raise ValueError('That link is too long.')
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or (parts.hostname or '').lower() not in TWITCH_HOSTS:
        raise ValueError('My Streams plays Twitch only. Paste a twitch.tv link or a channel name.')
    segments = parts.path.strip('/').split('/')
    if len(segments) > 1 and segments[0] == 'videos' and segments[1].isdigit():
        return 'https://www.twitch.tv/videos/' + segments[1]
    name = segments[0]
    if not TWITCH_NAME.fullmatch(name) or name.lower() in TWITCH_PAGES:
        raise ValueError('Use a Twitch channel link, like twitch.tv/name.')
    return 'https://www.twitch.tv/' + name.lower()


def clean_name(value):
    value = re.sub(r'[\x00-\x1f\x7f]+', ' ', str(value or ''))
    return re.sub(r'\s+', ' ', value).strip()[:60]


def is_video_url(url):
    """A past broadcast or other video (twitch.tv/videos/123) rather than a channel."""
    return urlsplit(url).path.startswith('/videos/')


def default_name(url):
    return 'Twitch video' if is_video_url(url) else urlsplit(url).path.strip('/').split('/')[0]


def describe(url, name=None):
    return {'id': hashlib.sha1(url.encode()).hexdigest()[:12], 'url': url,
            'name': clean_name(name) or default_name(url), 'platform': 'Twitch'}


class Library:
    """Saved streams shared by every screen, stored as JSON next to the app."""
    LIMIT = 200

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.changes = 0
        self.streams = self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            return [s for s in data['streams'] if isinstance(s, dict) and {'id', 'url', 'name', 'platform'} <= s.keys()]
        except FileNotFoundError:
            return []
        except (ValueError, KeyError, TypeError, OSError):
            # Keep an unreadable library for manual recovery instead of overwriting it.
            backup = self.path.with_name('streams.unreadable-%d.json' % time.time())
            self.path.replace(backup)
            print('Could not read the stream library; moved it to', backup, flush=True)
            return []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'streams': self.streams}, indent=1))
        os.replace(temporary, self.path)
        self.changes += 1

    @property
    def version(self):
        return str(self.changes)

    def all(self):
        with self.lock:
            return [dict(s) for s in self.streams]

    def get(self, stream_id):
        with self.lock:
            for stream in self.streams:
                if stream['id'] == stream_id:
                    return dict(stream)
        raise ValueError('That stream is no longer in your library.')

    def add(self, value, name=None):
        url = normalize_stream(value)
        if is_video_url(url):
            raise ValueError('A past broadcast is played, not saved. Once you watch it, it is in your history.')
        stream = describe(url, name)
        with self.lock:
            for existing in self.streams:
                if existing['id'] == stream['id']:
                    if clean_name(name) and existing['name'] != stream['name']:
                        existing['name'] = stream['name']
                        self.save()
                    return dict(existing), False
            if len(self.streams) >= self.LIMIT:
                raise ValueError('Your library is full. Remove a stream first.')
            stream['added'] = int(time.time())
            self.streams.append(stream)
            self.save()
            return dict(stream), True

    def remove(self, stream_id):
        with self.lock:
            kept = [s for s in self.streams if s['id'] != stream_id]
            if len(kept) != len(self.streams):
                self.streams = kept
                self.save()


def thumbnail_url(url):
    """Twitch's public preview image of a live channel."""
    login = urlsplit(url).path.strip('/').split('/')[0].lower()
    return 'https://static-cdn.jtvnw.net/previews-ttv/live_user_%s-440x248.jpg' % login


def probe_stream(stream):
    """Ask Twitch whether a saved channel is live: {live, checked, title, category, image}."""
    from streamlink import Streamlink
    session = Streamlink({'http-timeout': 10, 'ipv4': IPV4_ONLY})
    try:
        _, plugin_class, url = session.resolve_url(stream['url'])
        plugin = plugin_class(session, url)
        info = {'live': bool(plugin.streams()), 'checked': int(time.time())}
        if not info['live']:
            return info
        info.update(title=clean_name(plugin.get_title())[:120] if plugin.get_title() else None,
                    category=plugin.get_category(), image=thumbnail_url(url))
        return info
    finally:
        # Every check makes a new session; without this its connections stay open until the server runs out of files.
        session.http.close()


class ImageCache:
    """Preview images fetched once on demand and kept briefly, so screens never wait on other sites."""
    TTL = 300
    LIMIT = 400

    def __init__(self, http=None):
        self.http = http or requests.Session()
        self.lock = threading.Lock()
        self.sources = OrderedDict()   # key -> source URL
        self.cache = OrderedDict()     # key -> (url, fetched, content type, bytes)

    def register(self, key, url, version):
        """Remember where an image comes from; returns the versioned path screens load it from."""
        if not url:
            return None
        with self.lock:
            self.sources[key] = url
            self.sources.move_to_end(key)
            while len(self.sources) > self.LIMIT * 5:
                self.sources.popitem(last=False)
        return '/api/img/%s?v=%s' % (key, version)

    def get(self, key):
        with self.lock:
            url = self.sources.get(key)
            hit = self.cache.get(key)
        if not url:
            return None
        if hit and hit[0] == url and time.time() - hit[1] < self.TTL:
            return hit[2], hit[3]
        try:
            response = self.http.get(url, timeout=8)
            ok = response.status_code == 200 and len(response.content) < 2_000_000
        except requests.RequestException:
            ok = False
        if not ok:
            return (hit[2], hit[3]) if hit else None
        entry = (url, time.time(), response.headers.get('Content-Type', 'image/jpeg'), response.content)
        with self.lock:
            self.cache[key] = entry
            self.cache.move_to_end(key)
            while len(self.cache) > self.LIMIT:
                self.cache.popitem(last=False)
        return entry[2], entry[3]


class LiveChecker:
    """Checks the library in the background: who is live, what they stream, a preview image."""

    def __init__(self, streams, images, probe=probe_stream, workers=4, active=None):
        self.streams = streams      # returns every saved stream on the server, once each
        self.images = images
        self.probe = probe
        self.workers = workers
        self.active = active or (lambda: True)   # is anyone using the app?
        self.lock = threading.Lock()
        self.results = {}
        self.checked = 0
        self.changes = 0
        self.wake = threading.Event()

    def start(self):
        threading.Thread(target=self.loop, name='live-checker', daemon=True).start()

    def loop(self):
        while True:
            self.wake.clear()
            if self.active():
                try:
                    self.check_all()
                except Exception as error:
                    print('Live check failed:', error, flush=True)
            self.wake.wait(CHECK_INTERVAL)

    def resume(self):
        """Someone opened the app after a pause: check now. What was live long ago shows as "Checking..." meanwhile."""
        with self.lock:
            if self.results and time.time() - self.checked > STALE_AFTER:
                self.results = {}
                self.changes += 1
        self.wake.set()

    def check_one(self, stream):
        try:
            return self.probe(stream)
        except Exception as error:
            return {'live': None, 'checked': int(time.time()), 'error': str(error)[:200]}

    def check_all(self):
        streams = self.streams()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(self.check_one, streams))
        for stream, info in zip(streams, results):
            # Previews refresh every five minutes; between those the TV keeps its cached copy.
            info['thumb'] = self.images.register('s-' + stream['id'], info.pop('image', None), info['checked'] // 300)
        with self.lock:
            self.results = {stream['id']: info for stream, info in zip(streams, results)}
            self.checked = time.time()
            self.changes += 1

    def merged(self, library):
        """Library entries with their latest live information."""
        streams = library.all()
        with self.lock:
            for stream in streams:
                stream.update(self.results.get(stream['id'], {}))
        return streams

    @property
    def version(self):
        return str(self.changes)


def prepare_sample():
    folder = MEDIA / 'sample'
    folder.mkdir(parents=True, exist_ok=True)
    mp4 = folder / 'sample.mp4'
    if not mp4.exists():
        subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                        '-t', '12', '-vf', 'format=yuv420p', '-c:v', 'libx264',
                        '-preset', 'ultrafast', '-profile:v', 'main', '-level:v', '3.1',
                        '-g', '60', '-sc_threshold', '0', '-b:v', '1800k',
                        '-af', 'volume=0.08', '-c:a', 'aac', '-b:a', '128k',
                        '-ac', '2', '-movflags', '+faststart', str(mp4)], check=True)
    subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(mp4),
                    '-c', 'copy', '-hls_time', '2', '-hls_list_size', '0',
                    '-hls_playlist_type', 'vod', '-hls_segment_filename', str(folder / 'part%03d.ts'),
                    str(folder / 'index.m3u8')], check=True)


def video_format(quality):
    """Prefer H.264 at or below the chosen height: the TV plays it directly, so no re-encoding."""
    h = QUALITIES[quality]['height']
    return ('bv*[vcodec^=avc1][height<={h}]+ba[acodec^=mp4a]/bv*[vcodec^=avc1][height<={h}]+ba'
            '/b[vcodec^=avc1][height<={h}]/bv*[height<={h}]+ba/b[height<={h}]/b').format(h=h)


def extract_video(url, quality):
    """Look a video up with yt-dlp in its own process (importing it here would patch this server's HTTP library)."""
    result = subprocess.run([sys.executable, '-m', 'yt_dlp', *ipv4_args('--force-ipv4'),
                             '--dump-single-json', '--no-warnings', '--no-playlist',
                             '-f', video_format(quality), url], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError((result.stderr.strip().splitlines() or ['unknown error'])[-1])
    return json.loads(result.stdout)


class VideoLookups:
    """Recent yt-dlp lookups, kept a while: a retry or another screen does not look the same video up again."""
    TTL = 3 * 3600

    def __init__(self, extract=None):
        self.extract = extract or extract_video
        self.lock = threading.Lock()
        self.cache = {}

    def get(self, url, quality):
        now = time.time()
        with self.lock:
            self.cache = {key: hit for key, hit in self.cache.items() if now - hit[0] < self.TTL}
            hit = self.cache.get((url, quality))
        if hit:
            return hit[1]
        info = self.extract(url, quality)
        if not info.get('is_live') and info.get('live_status') != 'is_upcoming':   # broadcasts change; do not keep them
            with self.lock:
                self.cache[(url, quality)] = (now, info)
        return info


ACTIVE_RUNS = set()           # media folders that a relay is using right now
RUNS_LOCK = threading.Lock()


def remove_old_runs():
    """Delete finished streams and videos, never one that someone is watching."""
    with RUNS_LOCK:
        keep = set(ACTIVE_RUNS) | {'sample'}
    for folder in MEDIA.iterdir() if MEDIA.exists() else ():
        if folder.is_dir() and folder.name not in keep:
            shutil.rmtree(folder, ignore_errors=True)


def watching_now():
    return sum(1 for profile in list(PROFILES.values()) if profile.relay.state in ('starting', 'playing'))


class Relay:
    """One person's player source: a live channel (Streamlink) or a past broadcast (yt-dlp), served as HLS.
    All of that person's screens share it."""

    def __init__(self, profile=None):
        self.profile = profile
        self.lock = threading.RLock()
        self.processes = []
        self.logs = []
        self.folder = None
        self.started = 0
        self.state = 'idle'
        self.message = 'Ready.'
        self.stream = None
        self.run = None
        self.quality = None
        self.kind = 'live'
        self.duration = None

    def stop(self):
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
            for process in self.processes:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for log in self.logs:
                log.close()
            self.processes, self.logs = [], []
            with RUNS_LOCK:
                ACTIVE_RUNS.discard(self.folder.name if self.folder else None)
            self.state, self.message, self.folder = 'idle', 'Stopped.', None

    def start(self, stream, quality):
        with self.lock:
            busy = self.state in ('starting', 'playing')
            if not busy and watching_now() >= MAX_STREAMS:
                raise ValueError('The server is busy: %d people are watching right now. Try again later.' % MAX_STREAMS)
            self.stop()
            self.quality = quality
            # Long and random: the media path is the only key to this person's video.
            self.run = run = secrets.token_hex(12)
            self.stream = {key: stream.get(key) for key in ('id', 'url', 'name', 'platform', 'kind', 'title')}
            self.folder = MEDIA / run
            self.folder.mkdir(parents=True)
            with RUNS_LOCK:
                ACTIVE_RUNS.add(run)
            threading.Thread(target=remove_old_runs, daemon=True).start()
            self.started = time.monotonic()
            self.kind, self.duration = 'live', None
            kind = stream.get('kind') or ('auto' if is_video_url(stream['url']) else 'live')
            if kind == 'live':
                self.state, self.message = 'starting', 'Preparing ' + stream['name'] + ' (allow up to 60 seconds)...'
                self.launch_live(stream['url'], quality)
                return
            self.state, self.message = 'starting', 'Preparing ' + stream['name'] + '...'
        # Looking a video up takes a few seconds; do it without holding up other requests.
        threading.Thread(target=self.prepare_video, args=(run, stream['url'], quality), daemon=True).start()

    def prepare_video(self, run, url, quality):
        try:
            info = lookups.get(url, quality)
        except Exception as error:
            message = re.sub(r'^ERROR: (\[[^]]*\] )?', '', str(error)).strip()[:200]
            with self.lock:
                if self.run == run:
                    self.stop()
                    self.state, self.message = 'error', 'This video cannot be played: ' + message
            return
        with self.lock:
            if self.run != run or self.state != 'starting':
                return
            try:
                if info.get('live_status') == 'is_upcoming':
                    self.stop()
                    self.state, self.message = 'error', 'This broadcast has not started yet.'
                elif info.get('is_live'):
                    self.message = 'Preparing the live broadcast...'
                    self.launch_live(url, quality)
                else:
                    self.launch_video(info, quality)
                    if self.profile:
                        self.profile.history.started(url, info, self.stream)
            except Exception:
                self.stop()
                self.state, self.message = 'error', 'Could not start the video. Check the helper logs.'

    def open_logs(self):
        folder = RUNTIME / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        name = self.profile.id if self.profile else 'relay'
        self.logs = [open(folder / (name + '-download.log'), 'wb'), open(folder / (name + '-ffmpeg.log'), 'wb')]
        return self.logs

    def launch_live(self, url, quality):
        stream_log, video_log = self.open_logs()
        try:
            stream = subprocess.Popen([STREAMLINK, '--no-config', '--loglevel', 'info', *ipv4_args('--ipv4'),
                                       '--stream-segment-timeout', '15', '--stdout', url,
                                       QUALITIES[quality]['streams']], stdout=subprocess.PIPE, stderr=stream_log)
            self.processes.append(stream)
            video = subprocess.Popen([FFMPEG, '-hide_banner', '-loglevel', 'warning',
                                      '-y', '-fflags', '+genpts', '-i', 'pipe:0',
                                      '-map', '0:v:0', '-map', '0:a:0?', *encoder_args(quality),
                                      '-c:a', 'aac', '-b:a', '128k',
                                      '-ar', '48000', '-ac', '2', '-f', 'hls', '-hls_time', '2',
                                      '-hls_list_size', '8', '-hls_delete_threshold', '4',
                                      '-hls_flags', 'delete_segments+temp_file+omit_endlist',
                                      '-hls_segment_filename', str(self.folder / 'part%06d.ts'),
                                      str(self.folder / 'index.m3u8')],
                                     stdin=stream.stdout, stdout=subprocess.DEVNULL, stderr=video_log)
            self.processes.append(video)
            stream.stdout.close()
        except Exception:
            self.stop()
            raise

    def launch_video(self, info, quality):
        """Download with yt-dlp (fast, in chunks) and repackage into a growing, seekable HLS playlist."""
        self.kind, self.duration = 'video', info.get('duration')
        formats = info.get('requested_formats') or [info]
        info_file = self.folder / 'video.json'
        info_file.write_text(json.dumps(info))
        download_log, video_log = self.open_logs()
        try:
            downloads = []
            for item in formats:
                process = subprocess.Popen([sys.executable, '-m', 'yt_dlp', *ipv4_args('--force-ipv4'),
                                            '--quiet', '--no-warnings',
                                            '--load-info-json', str(info_file), '-f', item['format_id'],
                                            '--http-chunk-size', '10M', '-o', '-'],
                                           # yt-dlp keeps the piece it is fetching in a file: in this run's folder, removed with it
                                           cwd=self.folder, stdout=subprocess.PIPE, stderr=download_log)
                self.processes.append(process)
                downloads.append(process)
            # The first download arrives on stdin, any second one (audio) on its own pipe.
            # A small probe: the format is known, and reading less before starting saves time.
            probe = ['-probesize', '1M', '-analyzeduration', '1M']
            inputs = probe + ['-i', 'pipe:0']
            extra = [process.stdout.fileno() for process in downloads[1:]]
            for descriptor in extra:
                inputs += probe + ['-i', 'pipe:%d' % descriptor]
            video_index = next((i for i, f in enumerate(formats) if f.get('vcodec') not in (None, 'none')), 0)
            audio_index = next((i for i, f in enumerate(formats) if f.get('acodec') not in (None, 'none')), 0)
            picked = formats[video_index]
            copy = (str(picked.get('vcodec')).startswith(('avc1', 'h264'))
                    and (picked.get('height') or 0) <= QUALITIES[quality]['height'])
            video = subprocess.Popen([FFMPEG, '-hide_banner', '-loglevel', 'warning', '-y', *inputs,
                                      '-map', '%d:v:0' % video_index, '-map', '%d:a:0?' % audio_index,
                                      *(['-c:v', 'copy'] if copy else encoder_args(quality)),
                                      '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-ac', '2',
                                      '-f', 'hls', '-hls_time', '4', '-hls_playlist_type', 'event',
                                      '-hls_flags', 'temp_file',
                                      '-hls_segment_filename', str(self.folder / 'part%06d.ts'),
                                      str(self.folder / 'index.m3u8')],
                                     stdin=downloads[0].stdout, pass_fds=extra,
                                     stdout=subprocess.DEVNULL, stderr=video_log)
            self.processes.append(video)
            for process in downloads:
                process.stdout.close()
        except Exception:
            self.stop()
            raise

    def shown_stream(self):
        """The stream with where to continue it (None: never watched), so a screen joining later resumes too."""
        if not self.stream:
            return None
        position = self.profile.history.position(self.stream['url']) if self.profile else None
        return dict(self.stream, position=position)

    def status(self):
        with self.lock:
            url = None
            if self.state in ('starting', 'playing') and self.processes:
                codes = [p.poll() for p in self.processes]
                # A video's downloads finish normally; a live stream never should.
                failed = (any(code not in (None, 0) for code in codes) if self.kind == 'video'
                          else any(code is not None for code in codes))
                if failed:
                    self.stop()
                    self.state = 'error'
                    self.message = ('The video download stopped. Check the helper logs.' if self.kind == 'video' else
                                    'Stream stopped or could not start. The channel may be offline or the site refused playback. Check the helper logs.')
                elif self.state == 'playing':
                    # Enough had arrived already. Every screen asks every two seconds, and a long
                    # video's playlist is large: do not read it again.
                    url = '/media/' + self.folder.name + '/index.m3u8'
                elif (self.folder / 'index.m3u8').exists():
                    playlist = (self.folder / 'index.m3u8').read_text()
                    # Live streams need a few seconds of buffer; a video downloads faster than it plays.
                    needed = 1 if self.kind == 'video' else 3
                    if playlist.count('#EXTINF:') >= needed or '#EXT-X-ENDLIST' in playlist:
                        self.state, self.message = 'playing', 'Video ready.'
                        url = '/media/' + self.folder.name + '/index.m3u8'
                    elif time.monotonic() - self.started > 90:
                        self.stop()
                        self.state, self.message = 'error', 'Not enough video arrived. Try again or choose another stream.'
                    else:
                        self.message = 'Buffering the first few seconds...'
                elif time.monotonic() - self.started > 90:
                    self.stop()
                    self.state, self.message = 'error', 'No video arrived within 90 seconds. Try another stream.'
            elif self.state == 'starting' and time.monotonic() - self.started > 90:
                self.stop()
                self.state, self.message = 'error', 'Looking up the video took too long. Try again.'
            active = self.state != 'idle'
            return dict(state=self.state, message=self.message, url=url, instance_id=INSTANCE_ID, run=self.run,
                        stream=self.shown_stream() if active else None, library=self.profile.version(),
                        quality=self.profile.settings.quality, qualities=list(QUALITIES),
                        playing_quality=self.quality if active else None,
                        kind=self.kind if active else None, duration=self.duration if active else None)


images = ImageCache()
users = Users(DATA / 'users.json')
app_store = accounts.Store(DATA / 'app.json')     # the Twitch app registration, set by the owner
PROFILES = {}
PROFILES_LOCK = threading.Lock()


class Profile:
    """Everything that belongs to one person: library, settings, connected accounts and player."""

    def __init__(self, user_id):
        self.id = user_id
        folder = DATA / 'users' / user_id
        self.folder = folder
        self.library = Library(folder / 'streams.json')
        self.settings = Settings(folder / 'settings.json')
        self.feeds = accounts.Feeds(accounts.Store(folder / 'accounts.json'), images, app=app_store)
        self.history = history.WatchHistory(folder / 'history.json', images)
        self.relay = Relay(self)
        self.seen = 0                   # when one of this person's screens last asked for something
        self.feeds.active = self.here
        self.feeds.start()

    def here(self, within=IN_USE_FOR):
        """Is this person using the app (a screen open, or a stream playing somewhere)?"""
        return time.time() - self.seen < within

    def touch(self):
        """One of this person's screens asked for something."""
        away = not self.here()
        self.seen = time.time()
        if away:
            # Nothing was refreshed while nobody looked: catch up now.
            checker.resume()
            self.feeds.wake.set()

    def version(self):
        """Changes whenever anything shown in this person's library changes."""
        return '%s/%s/%s/%s/%s' % (INSTANCE_ID, checker.version, self.library.version, self.feeds.changes,
                                   self.history.changes)

    def home(self):
        """Everything the library screen shows, in one response."""
        streams = checker.merged(self.library)
        saved = {s['url']: s for s in streams}
        followed = []
        for item in self.feeds.snapshot():
            if item['url'] in saved:
                # Already in the library: borrow the viewer count, do not show it twice.
                saved[item['url']].setdefault('viewers', item.get('viewers'))
            else:
                followed.append(item)
        return {'streams': streams, 'followed': followed[:accounts.MAX_FOLLOWED_LIVE],
                'continuing': self.history.continuing(),
                'accounts': {name: {'connected': a['connected'], 'user': (a['user'] or {}).get('name')}
                             for name, a in self.feeds.accounts().items()},
                'version': self.version()}

    def close(self):
        self.relay.stop()
        self.feeds.stop()


def profile(user_id):
    with PROFILES_LOCK:
        if user_id not in PROFILES:
            PROFILES[user_id] = Profile(user_id)
        return PROFILES[user_id]


def all_saved_streams():
    """Every saved stream on the server, once: shared live checks cost the same for one or many people."""
    unique = {}
    for item in list(PROFILES.values()):
        for stream in item.library.all():
            unique.setdefault(stream['id'], stream)
    return list(unique.values())


def anyone_here():
    return any(item.here() for item in list(PROFILES.values()))


def watched(run):
    """A screen fetched part of this stream: its person is watching, even when the page itself sleeps
    (an iPhone in the background, AirPlay)."""
    for item in list(PROFILES.values()):
        if item.relay.run == run:
            item.touch()
            return


def stop_abandoned():
    """Closing the page does not stop a stream, so another screen can pick it up. One that no screen
    plays or asks about any more would otherwise keep the server encoding for nobody."""
    for item in list(PROFILES.values()):
        if item.relay.state in ('starting', 'playing') and not item.here(ABANDONED_AFTER):
            print('Nobody is watching %s any more; stopping it.' % (item.relay.stream or {}).get('name'), flush=True)
            item.relay.stop()


def watch_for_abandoned_streams():
    while True:
        time.sleep(60)
        try:
            stop_abandoned()
        except Exception as error:
            print('Stopping an unwatched stream failed:', error, flush=True)


checker = LiveChecker(all_saved_streams, images, active=anyone_here)
lookups = VideoLookups()


def create_owner():
    """A new server starts with one person: the owner. Whoever sets the server up signs in as them."""
    if not users.owner():
        users.create('Owner', admin=True)


EXTENSION_ORIGINS = ('chrome-extension://', 'moz-extension://', 'safari-web-extension://')
PUBLIC_FILES = {
    '/player.js': 'application/javascript',
    '/player.css': 'text/css',
    '/manifest.webmanifest': 'application/manifest+json',
    '/icons/apple-touch-icon.png': 'image/png',
    '/icons/icon-192.png': 'image/png',
    '/icons/icon-512.png': 'image/png',
    '/icons/icon-maskable-512.png': 'image/png',
    '/icons/favicon-32.png': 'image/png',
    '/vendor/hls.light.min.js': 'application/javascript',
    '/vendor/hls.js.LICENSE': 'text/plain',
}


PACKABLE = ('text/', 'application/json', 'application/javascript', 'application/manifest+json', 'image/svg+xml')
STATIC = {}                   # file -> (modified, size, ETag, content, compressed content or None)
STATIC_LOCK = threading.Lock()


def static_file(file, content_type):
    """A player file with its ETag and compressed copy, prepared again only when the file changes."""
    stat = file.stat()
    with STATIC_LOCK:
        hit = STATIC.get(file)
        if hit and hit[:2] == (stat.st_mtime_ns, stat.st_size):
            return hit[2:]
    body = file.read_bytes()
    packed = gzip.compress(body, 6, mtime=0) if content_type.startswith(PACKABLE) else None
    entry = (stat.st_mtime_ns, stat.st_size, '"%s"' % hashlib.sha1(body).hexdigest()[:20], body, packed)
    with STATIC_LOCK:
        STATIC[file] = entry
    return entry[2:]


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>My Streams</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0d13;color:#f2f3f8;
font:17px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}main{width:min(440px,92vw);padding:28px;border-radius:18px;background:#171a24}
h1{margin:0 0 10px;font-size:24px}p{color:#c2c8d9}.code{font-size:26px;font-weight:800;letter-spacing:.1em;color:#c9b8ff;white-space:nowrap}
input{box-sizing:border-box;width:100%;padding:12px;margin:6px 0 14px;border-radius:10px;border:2px solid #39405a;background:#0b0d13;color:#fff;
font-size:20px;letter-spacing:.08em;text-transform:uppercase}button,a.button{display:inline-block;padding:12px 20px;border:0;border-radius:10px;
background:#7c5cff;color:#fff;font-size:17px;font-weight:700;text-decoration:none;cursor:pointer}.muted{color:#8f97ad;font-size:15px}
.error{color:#ff9aa5}</style></head><body><main>__BODY__</main></body></html>"""

APPROVE_SCRIPT = """<script>
document.getElementById("approve").onclick = function () {
  var invite = document.getElementById("invite"), out = document.getElementById("out");
  fetch("/api/pair/approve", {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Playback-Test": %(token)s},
    body: JSON.stringify({code: %(code)s, invite: invite ? invite.value : ""})})
  .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
  .then(function (r) {
    if (!r[0]) { out.className = "error"; out.textContent = r[1].error; return; }
    if (r[1].token) { try { localStorage.setItem("my-streams-device", r[1].token); } catch (e) {} }
    document.querySelector("main").innerHTML = "<h1>Done!</h1><p>The %(device)s is signed in. You can close this page.</p>";
  });
};
</script>"""


def page(body):
    return PAGE.replace('__BODY__', body).encode()


FAILED_ATTEMPTS = {}   # address -> times of wrong invite codes, to stop guessing
ATTEMPTS_LOCK = threading.Lock()


def too_many_attempts(address, failed=False):
    with ATTEMPTS_LOCK:
        now = time.time()
        recent = [t for t in FAILED_ATTEMPTS.get(address, []) if now - t < 600]
        if failed:
            recent.append(now)
        FAILED_ATTEMPTS[address] = recent
        return len(recent) >= 10


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, content_type='application/json', headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        headers = dict(headers or {})
        if content_type.startswith(PACKABLE):
            headers['Vary'] = 'Accept-Encoding'
            if 'gzip' in self.headers.get('Accept-Encoding', '') and 'Content-Encoding' not in headers:
                if status == 200 and len(body) >= 1400:   # smaller ones (the status every two seconds) fit one packet anyway
                    body = gzip.compress(body, 5, mtime=0)
                    headers['Content-Encoding'] = 'gzip'
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', headers.pop('Cache-Control', 'no-store'))
        self.send_header('X-Content-Type-Options', 'nosniff')
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def static(self, file, content_type):
        """A player file. Screens keep their copy and only ask whether it changed, so an update still arrives at once."""
        etag, body, packed = static_file(file, content_type)
        headers = {'Cache-Control': 'no-cache', 'ETag': etag}
        if etag in self.headers.get('If-None-Match', ''):
            self.send_response(304)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            return
        if packed and 'gzip' in self.headers.get('Accept-Encoding', ''):
            body, headers['Content-Encoding'] = packed, 'gzip'   # compressed once, not on every request
        self.reply(200, body, content_type, headers)

    # Who is asking

    def base_url(self):
        host = (PUBLIC and self.headers.get('X-Forwarded-Host')) or self.headers.get('Host') or 'localhost'
        if not re.fullmatch(r'[A-Za-z0-9.:\[\]-]{1,200}', host):
            host = 'localhost'
        scheme = (PUBLIC and self.headers.get('X-Forwarded-Proto')) or 'http'
        return ('https' if scheme == 'https' else 'http') + '://' + host

    def device_token(self):
        token = self.headers.get('X-Device-Token')
        if token:
            return token
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', '')).get(COOKIE)
        except CookieError:
            return None
        return cookie.value if cookie else None

    def user(self):
        if not hasattr(self, '_user'):
            self._user = users.device_user(self.device_token())
        return self._user

    def sign_in_cookie(self, token):
        """Remember this device. Lax: sent when following a link here, never on other sites' requests."""
        secure = '; Secure' if PUBLIC else ''
        return {'Set-Cookie': '%s=%s; Path=/; Max-Age=63072000; HttpOnly; SameSite=Lax%s' % (COOKIE, token, secure)}

    def client_ip(self):
        forwarded = PUBLIC and self.headers.get('X-Forwarded-For')
        return forwarded.split(',')[0].strip() if forwarded else self.client_address[0]

    # GET

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path == '/app':
            return self.reply(302, b'', 'text/plain', {'Location': '/app/'})
        app_assets = {
            '/app/': ('index.html', 'text/html; charset=utf-8'),
            '/app/launcher.js': ('launcher.js', 'application/javascript'),
            '/app/icon.png': ('icon.png', 'image/png'),
            '/app/largeIcon.png': ('largeIcon.png', 'image/png'),
        }
        if path in app_assets:
            name, content_type = app_assets[path]
            return self.reply(200, (ROOT / 'webos' / name).read_bytes(), content_type)
        if path.startswith('/downloads/'):
            app_info = json.loads((ROOT / 'webos/appinfo.json').read_text())
            package_name = app_info['id'] + '_' + app_info['version'] + '_all.ipk'
            package = ROOT / 'dist' / package_name
            if path not in ('/downloads/my-streams.ipk', '/downloads/' + package_name):
                return self.reply(404, {'error': 'Not found'})
            if not package.is_file():
                return self.reply(404, {'error': 'Build the TV app package first.'})
            return self.reply(200, package.read_bytes(), 'application/octet-stream',
                              {'Content-Disposition': 'attachment; filename="' + package_name + '"'})
        if path == '/api/health':
            # Only this non-sensitive check is accessible to the file-based TV launcher.
            return self.reply(200, b'{"app":"my-streams-helper","ok":true}', 'application/json',
                              {'Access-Control-Allow-Origin': '*'})
        if path == '/api/session':
            # Same-origin only (no CORS), like the player page that already includes this token.
            return self.reply(200, {'token': TOKEN, 'instance_id': INSTANCE_ID})
        if path == '/':
            page_html = (ROOT / 'public/index.html').read_text().replace('__TEST_TOKEN__', TOKEN)
            return self.reply(200, page_html.encode(), 'text/html; charset=utf-8')
        if path in PUBLIC_FILES:
            return self.static(ROOT / 'public' / path.lstrip('/'), PUBLIC_FILES[path])
        if path == '/api/qr':
            return self.qr((query.get('data') or [''])[0])
        if path.startswith('/join/'):
            return self.join(path[len('/join/'):])
        if path == '/link':
            return self.link_page((query.get('code') or [''])[0])
        if path == '/api/owner-invite':
            # From the server itself only (tools/invite.py over SSH): how the owner signs in the first time.
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                return self.reply(403, {'error': 'Only from the server itself'})
            return self.reply(200, {'code': users.invite(users.owner())})
        if path.startswith('/media/'):
            return self.media(path)
        user = self.user()
        if not user:
            return self.reply(401, {'error': 'Sign in to continue.', 'signin': True})
        me = profile(user['id'])
        me.touch()
        if path == '/api/me':
            # Also renews the cookie when a device signed in with its saved token.
            return self.reply(200, {'user': {'name': user['name'], 'admin': user['admin']}},
                              'application/json', self.sign_in_cookie(self.device_token()))
        if path == '/api/status':
            return self.reply(200, me.relay.status())
        if path == '/api/streams':
            return self.reply(200, me.home())
        if path == '/api/accounts':
            return self.reply(200, me.feeds.accounts())
        if path == '/api/history':
            return self.reply(200, {'items': me.history.items()})
        if path == '/api/people':
            if not user['admin']:
                return self.reply(403, {'error': 'Only the owner can manage people.'})
            return self.reply(200, self.people())
        match = re.fullmatch(r'/api/img/([A-Za-z0-9_-]{1,80})', path)
        if match:
            image = images.get(match.group(1))
            if not image:
                return self.reply(404, {'error': 'No preview'})
            # The URL carries a version, so screens can keep the image instead of downloading it again.
            return self.reply(200, image[1], image[0], {'Cache-Control': 'private, max-age=3600'})
        return self.reply(404, {'error': 'Not found'})

    def qr(self, data):
        if not re.fullmatch(r'https?://[\x21-\x7e]{1,400}', data):
            return self.reply(400, {'error': 'Invalid QR content'})
        import segno
        buffer = io.BytesIO()
        segno.make(data, error='m').save(buffer, kind='svg', scale=8, border=2, dark='#000', light='#fff')
        return self.reply(200, buffer.getvalue(), 'image/svg+xml', {'Cache-Control': 'max-age=600'})

    def join(self, code):
        if too_many_attempts(self.client_ip()):
            return self.reply(429, page('<h1>Too many attempts</h1><p>Wait ten minutes and open the link again.</p>'),
                              'text/html; charset=utf-8')
        token = users.redeem(code, 'Browser')
        if not token:
            too_many_attempts(self.client_ip(), failed=True)
            return self.reply(404, page('<h1>This invite has expired</h1><p>Ask for a new invite link.</p>'),
                              'text/html; charset=utf-8')
        name = html.escape(users.device_user(token)['name'])
        body = ('<h1>Welcome, %s!</h1><p>This browser is now signed in to My Streams.</p>'
                '<p><a class="button" href="/">Open My Streams</a></p>'
                '<p class="muted">To sign in your TV: open My Streams on the TV and scan the code it shows '
                'with this phone.</p>' % name)
        if re.search(r'iPhone|iPad|iPod', self.headers.get('User-Agent', '')):
            # Home Screen apps on iPhone do not share Safari's sign-in, so give the code to enter there.
            body += ('<p class="muted">Want it as an app on your Home Screen? Tap <b>Share</b>, then '
                     '<b>Add to Home Screen</b>. Open it and enter this code:</p><p class="code">%s</p>'
                     % html.escape(show_code(clean_code(code, 10))))
        body += '<script>try{localStorage.setItem("my-streams-device",%s)}catch(e){}</script>' % json.dumps(token)
        return self.reply(200, page(body), 'text/html; charset=utf-8', self.sign_in_cookie(token))

    def link_page(self, code):
        """Opened by scanning the TV's sign-in QR code (or from the extension): approve that device."""
        pairing = users.pairing(code)
        if not pairing:
            return self.reply(404, page('<h1>This code has expired</h1><p>A new code is shown on the TV. '
                                        'Scan it again.</p>'), 'text/html; charset=utf-8')
        user = self.user()
        device = html.escape(pairing['kind'])
        shown = html.escape(show_code(pairing['code']))
        if user:
            ask = ('<p>Sign in the <b>%s</b> showing <span class="code">%s</span> as <b>%s</b>?</p>'
                   % (device, shown, html.escape(user['name'])))
            field = ''
        else:
            ask = ('<p>Sign in the <b>%s</b> showing <span class="code">%s</span>.</p>'
                   '<p>Enter the invite code you received:</p>' % (device, shown))
            field = '<input id="invite" autocomplete="off" autocapitalize="characters" placeholder="ABCDE-FGHJK">'
        script = APPROVE_SCRIPT % {'token': json.dumps(TOKEN), 'code': json.dumps(pairing['code']), 'device': device}
        body = '<h1>Sign in</h1>%s%s<button id="approve">Approve</button><p id="out"></p>%s' % (ask, field, script)
        return self.reply(200, page(body), 'text/html; charset=utf-8')

    def people(self):
        twitch = {'configured': accounts.Twitch(app_store, None).configured(),
                  'client_id': app_store.get('twitch').get('client_id', '')}
        return {'people': users.all(), 'apps': {'twitch': twitch}, 'watching': watching_now(), 'max_streams': MAX_STREAMS}

    def media(self, path):
        file = (RUNTIME / path.lstrip('/')).resolve()
        if not file.is_relative_to(MEDIA.resolve()) or not file.is_file():
            return self.reply(404, {'error': 'Not found'})
        watched(file.parent.name)
        if file.suffix == '.m3u8':
            # Return one complete snapshot: FFmpeg replaces live playlists atomically.
            # Ignore byte ranges on playlists so clients cannot reuse an old length.
            playlist = file.read_bytes()
            if b'#EXT-X-PLAYLIST-TYPE:EVENT' in playlist:
                # A video that is still downloading: start at the beginning, not the newest part.
                playlist = playlist.replace(b'#EXTM3U\n', b'#EXTM3U\n#EXT-X-START:TIME-OFFSET=0\n', 1)
            return self.reply(200, playlist, 'application/vnd.apple.mpegurl')
        size = file.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get('Range')
        if range_header:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
            if not match or not any(match.groups()):
                return self.reply(416, {'error': 'Invalid range'})
            left, right = match.groups()
            if left:
                start, end = int(left), min(int(right), end) if right else end
            else:
                start = max(0, size - int(right))
            if start > end or start >= size:
                return self.reply(416, {'error': 'Range outside file'})
        self.send_response(206 if range_header else 200)
        mime = {'.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t', '.mp4': 'video/mp4'}
        self.send_header('Content-Type', mime.get(file.suffix, mimetypes.guess_type(str(file))[0] or 'application/octet-stream'))
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        if range_header:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command != 'HEAD':
            try:
                with file.open('rb') as source:
                    source.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = source.read(min(remaining, 65536))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # POST

    def extension_origin(self):
        origin = self.headers.get('Origin') or ''
        return origin if origin.startswith(EXTENSION_ORIGINS) else None

    def do_OPTIONS(self):
        # Browser extensions with host permission normally skip CORS; answer a preflight anyway.
        origin = self.extension_origin()
        if not origin:
            return self.reply(403, {'error': 'Not allowed'})
        self.reply(204, b'', 'text/plain', {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Methods': 'GET, POST',
            'Access-Control-Allow-Headers': 'Content-Type, X-Playback-Test, X-Device-Token',
        })

    def started(self, me, stream):
        me.relay.start(stream, me.settings.quality)
        return {'ok': True, 'instance_id': INSTANCE_ID, 'run': me.relay.run, 'stream': me.relay.shown_stream()}

    def do_POST(self):
        # The page token plus a same-origin (or extension) check keep other websites from sending commands.
        if self.headers.get('X-Playback-Test') != TOKEN:
            return self.reply(403, {'error': 'Reload the player page.'})
        origin = self.headers.get('Origin')
        if origin and origin != self.base_url() and not self.extension_origin():
            return self.reply(403, {'error': 'Use the player from its own page.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Invalid request')
            path = urlsplit(self.path).path
            if path == '/api/pair/start':
                code, secret = users.start_pairing(str(body.get('kind') or 'TV'))
                link = self.base_url() + '/link?code=' + code
                return self.reply(200, {'code': show_code(code), 'secret': secret, 'link': link,
                                        'qr': '/api/qr?' + urlencode({'data': link}), 'setup': self.can_claim()})
            if path == '/api/setup/claim':
                if not self.can_claim():
                    return self.reply(403, {'error': 'The owner is already set up. Ask them for an invite.'})
                token = users.add_device(users.owner(), 'Browser')
                return self.reply(200, {'token': token}, 'application/json', self.sign_in_cookie(token))
            if path == '/api/pair/collect':
                token = users.collect(body.get('secret'))
                if not token:
                    return self.reply(200, {'done': False})
                return self.reply(200, {'done': True, 'token': token}, 'application/json', self.sign_in_cookie(token))
            if path == '/api/pair/approve':
                return self.approve(body)
            if path == '/api/devices/redeem':
                # A code typed on this device's own sign-in screen signs in this device.
                if too_many_attempts(self.client_ip()):
                    return self.reply(429, {'error': 'Too many wrong codes. Wait ten minutes.'})
                token = users.redeem(body.get('code'), str(body.get('kind') or 'Device')[:30])
                if not token:
                    too_many_attempts(self.client_ip(), failed=True)
                    return self.reply(400, {'error': 'That code is not valid or has expired. Check it and try again.'})
                return self.reply(200, {'token': token}, 'application/json', self.sign_in_cookie(token))
            user = self.user()
            if not user:
                return self.reply(401, {'error': 'Sign in to continue.', 'signin': True})
            if path.startswith(('/api/people/', '/api/app/')):
                if not user['admin']:
                    return self.reply(403, {'error': 'Only the owner can do this.'})
                return self.owner_action(path, body)
            me = profile(user['id'])
            me.touch()
            return self.person_action(path, body, user, me)
        except (ValueError, TypeError, AttributeError) as error:
            return self.reply(400, {'error': str(error)})
        except OSError:
            return self.reply(500, {'error': 'Could not launch the relay. Check FFmpeg and Streamlink installation.'})

    def can_claim(self):
        """First start: until the owner has signed in anywhere, the home network may set up as the owner.
        Never on a public server: there the owner signs in with tools/invite.py."""
        return not PUBLIC and not users.owner_signed_in()

    def approve(self, body):
        user, headers, token = self.user(), {}, None
        if not user:
            # Not signed in on this phone yet: the invite code signs it in and approves in one step.
            if too_many_attempts(self.client_ip()):
                return self.reply(429, {'error': 'Too many wrong codes. Wait ten minutes.'})
            token = users.redeem(body.get('invite'), 'Phone')
            if not token:
                too_many_attempts(self.client_ip(), failed=True)
                return self.reply(400, {'error': 'That invite code is not valid. Check it and try again.'})
            user, headers = users.device_user(token), self.sign_in_cookie(token)
        users.approve(body.get('code'), user['id'])
        return self.reply(200, {'ok': True, 'name': user['name'], 'token': token}, 'application/json', headers)

    def owner_action(self, path, body):
        if path == '/api/people/add':
            user_id = users.create(body.get('name'))
            profile(user_id)
            return self.reply(200, dict(self.people(), invite=self.invite_for(user_id)))
        if path == '/api/people/invite':
            return self.reply(200, dict(self.people(), invite=self.invite_for(str(body.get('id')))))
        if path == '/api/people/remove':
            user_id = str(body.get('id'))
            users.remove(user_id)
            with PROFILES_LOCK:
                removed = PROFILES.pop(user_id, None)
            if removed:
                removed.close()
            shutil.rmtree(DATA / 'users' / user_id, ignore_errors=True)
            return self.reply(200, self.people())
        if path == '/api/app/setup':
            accounts.Twitch(app_store, None).configure(body.get('client_id'))
            return self.reply(200, self.people())
        return self.reply(404, {'error': 'Not found'})

    def invite_for(self, user_id):
        code = users.invite(user_id)
        link = self.base_url() + '/join/' + code
        return {'name': users.get(user_id)['name'], 'code': show_code(code), 'link': link,
                'qr': '/api/qr?' + urlencode({'data': link})}

    def person_action(self, path, body, user, me):
        if path == '/api/start':
            if body.get('id') and not body.get('url'):
                stream = me.library.get(str(body['id']))
            else:
                stream = describe(normalize_stream(body.get('url')), body.get('name'))
            if body.get('kind') in ('live', 'video'):
                stream['kind'] = body['kind']
            stream['title'] = clean_name(body.get('title'))[:120] or None
            return self.reply(200, self.started(me, stream))
        if path.startswith('/api/accounts/'):
            provider = me.feeds.twitch
            action = path.rsplit('/', 1)[1]
            try:
                if action == 'connect':
                    provider.connect()
                elif action == 'disconnect':
                    provider.disconnect()
                    me.feeds.refresh_now()
                elif action == 'cancel':
                    provider.cancel()
                else:
                    return self.reply(404, {'error': 'Not found'})
            except requests.RequestException:
                return self.reply(502, {'error': 'Could not reach the sign-in service. Check the internet connection.'})
            return self.reply(200, me.feeds.accounts())
        if path == '/api/stop':
            me.relay.stop()
            return self.reply(200, {'ok': True})
        if path == '/api/streams/add':
            stream, created = me.library.add(body.get('url'), body.get('name'))
            if created:
                checker.wake.set()
            result = {'ok': True, 'stream': stream, 'created': created}
            if body.get('play'):
                result.update(self.started(me, stream))
            return self.reply(200, result)
        if path == '/api/settings':
            me.settings.set_quality(str(body.get('quality', '')))
            return self.reply(200, {'ok': True, 'quality': me.settings.quality})
        if path == '/api/history/progress':
            known = me.history.progress(str(body.get('url', '')), body.get('position'), body.get('duration'),
                                        'ended' if body.get('event') == 'ended' else
                                        'pause' if body.get('event') == 'pause' else 'tick')
            return self.reply(200, {'ok': known})
        if path == '/api/history/remove':
            me.history.remove(str(body.get('url', '')))
            return self.reply(200, {'ok': True})
        if path == '/api/history/clear':
            me.history.clear()
            return self.reply(200, {'ok': True})
        if path == '/api/streams/remove':
            me.library.remove(str(body.get('id', '')))
            return self.reply(200, {'ok': True})
        if path == '/api/devices/code':
            # Sign in another device of this person, e.g. the iPhone Home Screen app (its storage is separate).
            code = users.invite(user['id'], seconds=3600)
            link = self.base_url() + '/join/' + code
            return self.reply(200, {'code': show_code(code), 'link': link, 'qr': '/api/qr?' + urlencode({'data': link})})
        if path == '/api/logout':
            users.sign_out(self.device_token())
            return self.reply(200, {'ok': True}, 'application/json',
                              {'Set-Cookie': '%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax' % COOKIE})
        if path == '/api/event':
            event = str(body.get('event', ''))[:50]
            detail = str(body.get('detail', ''))[:500]
            print(f'Event {user["name"]} {self.client_ip()} {event!r}: {detail!r}', flush=True)
            return self.reply(200, {'ok': True})
        return self.reply(404, {'error': 'Not found'})

    def log_message(self, fmt, *args):
        if '/api/status' not in self.path and '/api/pair/collect' not in self.path:
            super().log_message(fmt, *args)

    def address_string(self):
        return self.client_ip()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def verify_request(self, request, address):
        if PUBLIC:
            return True
        ip = ipaddress.ip_address(address[0])
        return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10')


def start_background_work():
    create_owner()
    for person in users.all():
        profile(person['id'])
    checker.start()
    threading.Thread(target=watch_for_abandoned_streams, name='unwatched-streams', daemon=True).start()


def main():
    global MAX_STREAMS, PUBLIC, IPV4_ONLY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--max-streams', type=int, default=MAX_STREAMS,
                        help='How many people can watch at the same time (each stream uses CPU and upload)')
    parser.add_argument('--allow-ipv6', action='store_true',
                        help='Also use IPv6 for streams (needed on an IPv6-only server)')
    parser.add_argument('--public', action='store_true',
                        help='Accept connections from the internet. Only behind an HTTPS reverse proxy.')
    args = parser.parse_args()
    MAX_STREAMS, PUBLIC, IPV4_ONLY = args.max_streams, args.public, not args.allow_ipv6
    if IPV4_ONLY:
        use_ipv4_only()
    if not FFMPEG or not Path(STREAMLINK).is_file():
        sys.exit('Install ffmpeg and run this with .venv/bin/python after installing requirements.txt.')
    MEDIA.mkdir(parents=True, exist_ok=True)
    prepare_sample()
    remove_old_runs()
    server = LocalServer(('0.0.0.0', args.port), Handler)
    start_background_work()
    atexit.register(lambda: [item.relay.stop() for item in list(PROFILES.values())])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(('192.0.2.1', 80))
            address = sock.getsockname()[0]
        except OSError:
            address = 'localhost'
    print(f'My Streams is running: http://{address}:{args.port}', flush=True)
    owner = users.owner()
    if not any(person['devices'] for person in users.all() if person['id'] == owner):
        print(f'Sign in as the owner: http://{address}:{args.port}/join/{users.invite(owner)}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for item in list(PROFILES.values()):
            item.relay.stop()
        server.server_close()


if __name__ == '__main__':
    main()
