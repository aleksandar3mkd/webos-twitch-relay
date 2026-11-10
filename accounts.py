"""Twitch sign-in (device code flow) and the live channels a person follows.

Everything here runs on the server in background threads, so the TV only ever reads
small, ready-made lists. The owner registers a Twitch app once (app.json); each person
only signs in. Tokens never leave this server.
"""
import json
import os
import re
import threading
import time

import requests

TWITCH_SCOPES = 'user:read:follows'
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
DEVICE_URL = 'https://id.twitch.tv/oauth2/device'
TOKEN_URL = 'https://id.twitch.tv/oauth2/token'

TWITCH_LIVE_EVERY = 60          # followed live streams: one API call
MAX_FOLLOWED_LIVE = 24          # keep the TV light even when many channels are live


class AccountError(Exception):
    pass


class Store:
    """Client ID and tokens, saved privately in one JSON file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, provider):
        with self.lock:
            return dict(self.data.get(provider) or {})

    def update(self, provider, **values):
        with self.lock:
            entry = self.data.setdefault(provider, {})
            for key, value in values.items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            # Owner-only: this file holds sign-in tokens.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as handle:
                json.dump(self.data, handle, indent=1)
            os.replace(temporary, self.path)


class Twitch:
    """One person's Twitch sign-in, with the device code flow: the TV shows a code, the phone approves it."""
    name = 'twitch'

    def __init__(self, store, http, app=None):
        self.store = store          # this person's tokens
        self.app = app or store     # the app registration, shared by everyone
        self.http = http
        self.lock = threading.Lock()
        self.pending = None      # {code, url, expires} while waiting for approval
        self.error = None
        self.attempt = 0
        self.on_connected = None

    def config(self):
        """App registration plus this person's tokens."""
        config = self.app.get(self.name)
        config.update(self.store.get(self.name))
        return config

    def configured(self):
        return bool(self.app.get(self.name).get('client_id'))

    def summary(self):
        config = self.config()
        with self.lock:
            pending = dict(self.pending) if self.pending else None
        return {'configured': self.configured(), 'connected': bool(config.get('refresh_token')),
                'user': config.get('user'), 'pending': pending, 'error': self.error}

    def configure(self, client_id):
        client_id = str(client_id or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{8,200}', client_id):
            raise ValueError('That does not look like a client ID.')
        self.app.update(self.name, client_id=client_id)
        self.error = None

    def connect(self):
        if not self.configured():
            raise ValueError('The server owner has not set up Twitch sign-in yet.')
        config = self.config()
        response = self.http.post(DEVICE_URL, timeout=10,
                                  data={'client_id': config['client_id'], 'scopes': TWITCH_SCOPES})
        data = response.json()
        if response.status_code != 200 or 'device_code' not in data:
            raise ValueError('Sign-in could not start: ' + str(data.get('message') or data.get('error_description')
                                                               or data.get('error') or response.status_code))
        with self.lock:
            self.attempt += 1
            attempt = self.attempt
            self.pending = {'code': data['user_code'], 'url': data.get('verification_uri'),
                            'expires': int(time.time()) + int(data.get('expires_in', 1800))}
            self.error = None
        result = self.summary()
        threading.Thread(target=self.wait_for_approval, name='twitch-sign-in', daemon=True,
                         args=(attempt, data['device_code'], int(data.get('interval', 5)))).start()
        return result

    def wait_for_approval(self, attempt, device_code, interval):
        while True:
            time.sleep(interval)
            with self.lock:
                if attempt != self.attempt or not self.pending:
                    return
                expired = time.time() > self.pending['expires']
            if expired:
                self.finish(attempt, 'The code expired. Choose Connect to get a new one.')
                return
            config = self.config()
            try:
                response = self.http.post(TOKEN_URL, timeout=10,
                                          data={'client_id': config['client_id'], 'scopes': TWITCH_SCOPES,
                                                'device_code': device_code, 'grant_type': DEVICE_GRANT})
                data = response.json()
            except (requests.RequestException, ValueError):
                continue
            if 'access_token' in data:
                try:
                    user = self.profile(data['access_token'])
                except Exception as error:
                    self.finish(attempt, 'Signed in, but reading your profile failed: %s' % error)
                    return
                self.store.update(self.name, access_token=data['access_token'],
                                  refresh_token=data.get('refresh_token'),
                                  expires=int(time.time()) + int(data.get('expires_in', 3600)), user=user)
                self.finish(attempt, None)
                if self.on_connected:
                    self.on_connected()
                return
            problem = str(data.get('error') or data.get('message') or '')
            if problem == 'slow_down':
                interval += 5
            elif problem not in ('authorization_pending', ''):
                self.finish(attempt, 'Sign-in was not completed (%s).' % problem.replace('_', ' '))
                return

    def finish(self, attempt, error):
        with self.lock:
            if attempt == self.attempt:
                self.pending = None
                self.error = error

    def cancel(self):
        with self.lock:
            self.attempt += 1
            self.pending = None

    def disconnect(self):
        self.cancel()
        self.store.update(self.name, access_token=None, refresh_token=None, expires=None, user=None)
        self.error = None

    def token(self):
        """A valid access token, refreshed when it is about to expire."""
        config = self.config()
        if not config.get('refresh_token'):
            raise AccountError('not connected')
        if config.get('access_token') and config.get('expires', 0) > time.time() + 120:
            return config['access_token']
        response = self.http.post(TOKEN_URL, timeout=10,
                                  data={'client_id': config['client_id'], 'grant_type': 'refresh_token',
                                        'refresh_token': config['refresh_token']})
        data = response.json()
        if 'access_token' not in data:
            if response.status_code in (400, 401):
                self.store.update(self.name, access_token=None, refresh_token=None, expires=None)
                self.error = 'Your sign-in expired. Choose Connect to sign in again.'
            raise AccountError('token refresh failed: %s' % (data.get('message') or data.get('error')))
        # Twitch issues a new one-time refresh token with every refresh.
        self.store.update(self.name, access_token=data['access_token'],
                          refresh_token=data.get('refresh_token') or config['refresh_token'],
                          expires=int(time.time()) + int(data.get('expires_in', 3600)))
        return data['access_token']

    def profile(self, access_token):
        data = self.http.get('https://id.twitch.tv/oauth2/validate', timeout=10,
                             headers={'Authorization': 'OAuth ' + access_token}).json()
        return {'id': data['user_id'], 'name': data['login']}

    def followed_live(self):
        config = self.config()
        user = config.get('user') or {}
        response = self.http.get('https://api.twitch.tv/helix/streams/followed', timeout=10,
                                 params={'user_id': user.get('id'), 'first': 100},
                                 headers={'Client-Id': config['client_id'], 'Authorization': 'Bearer ' + self.token()})
        if response.status_code == 401:
            self.store.update(self.name, expires=0)   # refresh on the next round
        response.raise_for_status()
        streams = []
        for item in response.json().get('data', []):
            login = item['user_login']
            streams.append({
                'id': 'tw-' + login, 'name': item.get('user_name') or login,
                'url': 'https://www.twitch.tv/' + login, 'platform': 'Twitch', 'live': True,
                'title': (item.get('title') or '')[:120], 'category': item.get('game_name') or None,
                'viewers': item.get('viewer_count', 0), 'source': 'followed',
                'image': (item.get('thumbnail_url') or '').replace('{width}', '440').replace('{height}', '248'),
            })
        return sorted(streams, key=lambda s: -s['viewers'])


class Feeds:
    """Background refresh of the live channels one person follows."""

    def __init__(self, store, images, http=None, app=None):
        self.http = http or requests.Session()
        self.http.headers['User-Agent'] = 'my-streams-helper'
        self.twitch = Twitch(store, self.http, app)
        self.images = images
        self.twitch.on_connected = self.refresh_now
        self.lock = threading.Lock()
        self.changes = 0
        self.followed = []
        self.due = 0                 # when the next refresh is due
        self.stopped = False
        self.wake = threading.Event()
        self.active = lambda: True   # is this person using the app? Nothing is refreshed for an empty room.

    def start(self):
        threading.Thread(target=self.loop, name='feeds', daemon=True).start()

    def refresh_now(self):
        self.due = 0
        self.wake.set()

    def stop(self):
        self.stopped = True
        self.wake.set()

    def loop(self):
        while not self.stopped:
            self.wake.clear()
            # While nobody uses the app nothing runs; an overdue refresh runs the moment someone opens it.
            if self.active() and time.time() >= self.due:
                self.due = time.time() + TWITCH_LIVE_EVERY
                try:
                    self.refresh()
                except AccountError:
                    pass
                except Exception as error:
                    print('Refreshing followed channels failed: %s' % error, flush=True)
            self.wake.wait(15)

    def publish(self, followed):
        with self.lock:
            self.followed = followed
            self.changes += 1

    def refresh(self):
        if not self.twitch.summary()['connected']:
            if self.followed:
                self.publish([])
            return
        streams = self.twitch.followed_live()
        bucket = int(time.time() // 300)   # Twitch updates previews every few minutes
        for stream in streams:
            stream['thumb'] = self.images.register(stream['id'], stream.pop('image'), bucket)
        self.publish(streams)

    def snapshot(self):
        with self.lock:
            return list(self.followed)

    def accounts(self):
        return {'twitch': self.twitch.summary()}
