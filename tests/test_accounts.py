"""Twitch sign-in, token refresh and followed live channels, with the network replaced by fakes."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MY_STREAMS_DATA', tempfile.mkdtemp())   # never touch the real data/
import accounts  # noqa: E402
import server  # noqa: E402


class Response:
    def __init__(self, data, status=200, content=b''):
        self.data, self.status_code, self.content = data, status, content

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    """Answers by URL; each value is a Response or a list of Responses used in turn."""

    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def handle(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.routes[url]
        return answer.pop(0) if isinstance(answer, list) else answer

    def get(self, url, **kwargs):
        return self.handle('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.handle('POST', url, **kwargs)


class Images:
    def register(self, key, url, version):
        return '/api/img/%s?v=%s' % (key, version)


class TwitchSignInTest(unittest.TestCase):
    def test_device_flow_refresh_and_followed_live(self):
        with tempfile.TemporaryDirectory() as folder:
            store = accounts.Store(Path(folder) / 'accounts.json')
            http = FakeHttp({
                'https://id.twitch.tv/oauth2/device': Response({'device_code': 'dev', 'user_code': 'ABCD',
                                                                'verification_uri': 'https://www.twitch.tv/activate?x=1',
                                                                'interval': 0, 'expires_in': 60}),
                'https://id.twitch.tv/oauth2/token': [
                    Response({'status': 400, 'message': 'authorization_pending'}, 400),
                    Response({'access_token': 'a1', 'refresh_token': 'r1', 'expires_in': 3600}),
                    Response({'access_token': 'a2', 'refresh_token': 'r2', 'expires_in': 14000}),
                ],
                'https://id.twitch.tv/oauth2/validate': Response({'user_id': '42', 'login': 'me'}),
                'https://api.twitch.tv/helix/streams/followed': Response({'data': [
                    {'user_login': 'small', 'user_name': 'Small', 'title': 't', 'game_name': 'Chess', 'viewer_count': 5,
                     'thumbnail_url': 'https://x/{width}x{height}.jpg'},
                    {'user_login': 'big', 'user_name': 'Big', 'title': 't', 'game_name': None, 'viewer_count': 900,
                     'thumbnail_url': ''}]}),
            })
            twitch = accounts.Twitch(store, http)
            self.assertFalse(twitch.configured())
            with self.assertRaises(ValueError):
                twitch.connect()
            twitch.configure('abcdefgh12345')
            connected = []
            twitch.on_connected = lambda: connected.append(True)
            self.assertEqual(twitch.connect()['pending']['code'], 'ABCD')
            for _ in range(200):
                if not twitch.summary()['pending']:
                    break
                time.sleep(0.01)
            summary = twitch.summary()
            self.assertTrue(summary['connected'])
            self.assertEqual(summary['user'], {'id': '42', 'name': 'me'})
            self.assertEqual(connected, [True])
            self.assertNotIn('refresh_token', summary)
            mode = stat.S_IMODE((Path(folder) / 'accounts.json').stat().st_mode)
            self.assertEqual(mode, 0o600)
            # Expire the token: the next call refreshes it and stores the new one-time refresh token.
            store.update('twitch', expires=0)
            live = twitch.followed_live()
            self.assertEqual(store.get('twitch')['refresh_token'], 'r2')
            self.assertEqual([s['name'] for s in live], ['Big', 'Small'])
            self.assertEqual(live[1]['image'], 'https://x/440x248.jpg')
            helix = [call for call in http.calls if 'helix' in call[1]][0][2]
            self.assertEqual(helix['headers']['Authorization'], 'Bearer a2')
            self.assertEqual(helix['params']['user_id'], '42')
            twitch.disconnect()
            self.assertFalse(twitch.summary()['connected'])
            self.assertTrue(twitch.summary()['configured'])

    def test_rejected_refresh_signs_out(self):
        with tempfile.TemporaryDirectory() as folder:
            store = accounts.Store(Path(folder) / 'accounts.json')
            store.update('twitch', client_id='abcdefgh1', refresh_token='old', access_token='x', expires=0)
            http = FakeHttp({'https://id.twitch.tv/oauth2/token': Response({'message': 'Invalid refresh token'}, 400)})
            twitch = accounts.Twitch(store, http)
            with self.assertRaises(accounts.AccountError):
                twitch.token()
            self.assertFalse(twitch.summary()['connected'])
            self.assertIn('expired', twitch.summary()['error'])


class FollowedTest(unittest.TestCase):
    def test_followed_live_channels_are_published_with_previews(self):
        with tempfile.TemporaryDirectory() as folder:
            store = accounts.Store(Path(folder) / 'accounts.json')
            store.update('twitch', client_id='abcdefgh1', refresh_token='r', access_token='a',
                         expires=time.time() + 3600, user={'id': '42', 'name': 'me'})
            http = FakeHttp({'https://api.twitch.tv/helix/streams/followed': Response({'data': [
                {'user_login': 'big', 'user_name': 'Big', 'title': 't', 'game_name': 'Chess', 'viewer_count': 900,
                 'thumbnail_url': 'https://x/{width}x{height}.jpg'}]})})
            feeds = accounts.Feeds(store, Images(), http)
            before = feeds.changes
            feeds.refresh()
            self.assertEqual([(s['id'], s['viewers']) for s in feeds.snapshot()], [('tw-big', 900)])
            self.assertTrue(feeds.snapshot()[0]['thumb'].startswith('/api/img/tw-big?v='))
            self.assertNotEqual(feeds.changes, before)
            # Disconnecting empties the row.
            feeds.twitch.disconnect()
            feeds.refresh()
            self.assertEqual(feeds.snapshot(), [])


class HomeTest(unittest.TestCase):
    def test_followed_streams_already_saved_are_not_shown_twice(self):
        person = server.profile(server.users.create('Home test', admin=True))
        try:
            saved, _ = person.library.add('somechannel')
            person.feeds.followed = [{'id': 'tw-x', 'url': saved['url'], 'viewers': 50},
                                     {'id': 'tw-y', 'url': 'https://www.twitch.tv/other', 'viewers': 10}]
            result = person.home()
        finally:
            person.close()
        self.assertEqual([f['id'] for f in result['followed']], ['tw-y'])
        self.assertEqual(result['streams'][0]['viewers'], 50)
        self.assertEqual(set(result['accounts']), {'twitch'})


class AppRegistrationTest(unittest.TestCase):
    def test_app_details_are_shared_tokens_are_personal(self):
        with tempfile.TemporaryDirectory() as folder:
            app = accounts.Store(Path(folder) / 'app.json')
            alice = accounts.Twitch(accounts.Store(Path(folder) / 'a.json'), None, app)
            bob = accounts.Twitch(accounts.Store(Path(folder) / 'b.json'), None, app)
            self.assertFalse(bob.configured())
            alice.configure('abcdefgh12345')
            self.assertTrue(bob.configured())
            alice.store.update('twitch', refresh_token='secret')
            self.assertTrue(alice.summary()['connected'])
            self.assertFalse(bob.summary()['connected'])
            self.assertNotIn('client_id', bob.summary())


class InUseTest(unittest.TestCase):
    def test_nothing_is_refreshed_while_nobody_uses_the_app(self):
        with tempfile.TemporaryDirectory() as folder:
            feeds = accounts.Feeds(accounts.Store(Path(folder) / 'accounts.json'), None, FakeHttp({}))
            ran, in_use = [], [False]
            feeds.refresh = lambda: ran.append('refresh')
            feeds.active = lambda: in_use[0]
            worker = threading.Thread(target=feeds.loop, daemon=True)
            worker.start()
            try:
                time.sleep(0.1)
                self.assertEqual(ran, [])
                in_use[0] = True
                feeds.wake.set()             # what opening the app does
                for _ in range(200):
                    if ran:
                        break
                    time.sleep(0.01)
                self.assertEqual(ran, ['refresh'])
            finally:
                feeds.stop()
                worker.join(2)


if __name__ == '__main__':
    unittest.main()
