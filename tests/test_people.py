"""End to end over HTTP: signing in, inviting people, pairing a TV, and keeping people's data apart."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MY_STREAMS_DATA', tempfile.mkdtemp())   # never touch the real data/
import server  # noqa: E402
from users import Users  # noqa: E402


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, None

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = dict(headers or {})
        if self.cookie:
            headers['Cookie'] = self.cookie
        if method == 'POST':
            headers.setdefault('X-Playback-Test', server.TOKEN)
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, json.dumps(body or {}) if method == 'POST' else None, headers)
        response = connection.getresponse()
        data = response.read()
        cookie = response.getheader('Set-Cookie')
        if cookie:
            self.cookie = cookie.split(';')[0]
        try:
            data = json.loads(data)
        except ValueError:
            try:
                data = data.decode()
            except UnicodeDecodeError:
                pass   # an image
        return response.status, data


class PeopleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.create_owner()
        cls.httpd = server.LocalServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        for person in list(server.PROFILES.values()):
            person.close()

    def test_sign_in_invite_pair_and_separate_libraries(self):
        stranger = Client(self.port)
        self.assertEqual(stranger.call('GET', '/api/status')[0], 401)
        self.assertEqual(stranger.call('POST', '/api/stop')[0], 401)

        # First start: the home network may set up as the owner, once.
        setup = Client(self.port)
        self.assertTrue(setup.call('POST', '/api/pair/start', {'kind': 'browser'})[1]['setup'])
        server.PUBLIC = True
        try:
            self.assertEqual(setup.call('POST', '/api/setup/claim')[0], 403)   # never on a public server
        finally:
            server.PUBLIC = False
        status, claimed = setup.call('POST', '/api/setup/claim')
        self.assertEqual(status, 200)
        self.assertTrue(setup.call('GET', '/api/me')[1]['user']['admin'])
        self.assertFalse(Client(self.port).call('POST', '/api/pair/start', {'kind': 'TV'})[1]['setup'])
        self.assertEqual(Client(self.port).call('POST', '/api/setup/claim')[0], 403)

        # The owner can also sign in with the invite printed by the server (or tools/invite.py).
        owner = Client(self.port)
        code = owner.call('GET', '/api/owner-invite')[1]['code']
        status, page = owner.call('GET', '/join/' + code)
        self.assertEqual(status, 200)
        self.assertIn('Welcome', page)
        self.assertTrue(owner.call('GET', '/api/me')[1]['user']['admin'])

        # The owner invites a friend.
        status, people = owner.call('POST', '/api/people/add', {'name': 'Friend'})
        self.assertEqual(status, 200)
        invite = people['invite']
        self.assertRegex(invite['code'], r'^[A-Z2-9]{5}-[A-Z2-9]{5}$')
        self.assertTrue(invite['link'].endswith('/join/' + invite['code'].replace('-', '')))
        friend_id = [p['id'] for p in people['people'] if p['name'] == 'Friend'][0]

        # The friend's TV shows a code; their phone (not signed in yet) approves it with the invite code.
        tv = Client(self.port)
        status, pairing = tv.call('POST', '/api/pair/start', {'kind': 'TV'})
        self.assertEqual(status, 200)
        self.assertIn('/link?code=', pairing['link'])
        self.assertEqual(tv.call('GET', pairing['qr'])[0], 200)
        self.assertEqual(tv.call('POST', '/api/pair/collect', {'secret': pairing['secret']})[1], {'done': False})
        phone = Client(self.port)
        self.assertIn('invite code', phone.call('GET', '/link?code=' + pairing['code'].replace('-', ''))[1])
        status, result = phone.call('POST', '/api/pair/approve', {'code': pairing['code'], 'invite': invite['code']})
        self.assertEqual((status, result['name']), (200, 'Friend'))
        status, result = tv.call('POST', '/api/pair/collect', {'secret': pairing['secret']})
        self.assertTrue(result['done'])
        self.assertEqual(tv.call('GET', '/api/me')[1]['user'], {'name': 'Friend', 'admin': False})
        self.assertEqual(phone.call('GET', '/api/me')[1]['user']['name'], 'Friend')
        # A TV that lost its cookie still signs in with its saved token.
        self.assertEqual(Client(self.port).call('GET', '/api/me', headers={'X-Device-Token': result['token']})[0], 200)

        # Each person has their own library.
        self.assertEqual(tv.call('POST', '/api/streams/add', {'url': 'https://www.twitch.tv/friendpick'})[0], 200)
        friend_urls = [s['url'] for s in tv.call('GET', '/api/streams')[1]['streams']]
        owner_urls = [s['url'] for s in owner.call('GET', '/api/streams')[1]['streams']]
        self.assertEqual(friend_urls, ['https://www.twitch.tv/friendpick'])
        self.assertNotIn('https://www.twitch.tv/friendpick', owner_urls)

        # Only the owner manages people and app registrations.
        self.assertEqual(tv.call('POST', '/api/people/add', {'name': 'Sneaky'})[0], 403)
        self.assertEqual(tv.call('GET', '/api/people')[0], 403)
        self.assertEqual(tv.call('POST', '/api/app/setup', {'client_id': 'abcdefgh123'})[0], 403)
        status, people = owner.call('POST', '/api/app/setup', {'client_id': 'abcdefgh123'})
        self.assertTrue(people['apps']['twitch']['configured'])

        owner_id = server.users.owner()
        # Only Twitch is accepted, from the player and from the extension alike.
        status, result = tv.call('POST', '/api/streams/add', {'url': 'https://www.youtube.com/watch?v=abcdefghijk'})
        self.assertEqual(status, 400)
        self.assertIn('Twitch only', result['error'])

        # Watch history: kept on the server per person, so every screen continues where another stopped.
        watching = 'https://www.twitch.tv/videos/123456789'
        friend = next(p for user_id, p in server.PROFILES.items() if user_id != owner_id)
        self.assertEqual(tv.call('POST', '/api/history/progress', {'url': watching, 'position': 50})[1], {'ok': False})
        friend.history.started(watching, {'title': 'Long talk', 'channel': 'Talks', 'duration': 3600},
                               {'platform': 'Twitch'})
        status, result = phone.call('POST', '/api/history/progress', {'url': watching, 'position': 754,
                                                                      'duration': 3600, 'event': 'pause'})
        self.assertEqual((status, result), (200, {'ok': True}))
        home = tv.call('GET', '/api/streams')[1]
        self.assertEqual([(c['title'], c['position']) for c in home['continuing']], [('Long talk', 754)])
        self.assertEqual(friend.history.position(watching), 754)
        friend.relay.stream = {'url': watching, 'name': 'Talks'}
        self.assertEqual(friend.relay.shown_stream()['position'], 754)   # a joining screen resumes there too
        friend.relay.stream = None
        self.assertEqual(owner.call('GET', '/api/history')[1], {'items': []})   # nobody sees another's history
        self.assertEqual([i['url'] for i in tv.call('GET', '/api/history')[1]['items']], [watching])
        self.assertEqual(tv.call('POST', '/api/history/progress', {'url': watching, 'position': 'soon'})[0], 400)
        tv.call('POST', '/api/history/progress', {'url': watching, 'position': 3590, 'event': 'ended'})
        self.assertEqual(tv.call('GET', '/api/streams')[1]['continuing'], [])
        self.assertTrue(tv.call('GET', '/api/history')[1]['items'][0]['finished'])
        self.assertEqual(tv.call('POST', '/api/history/remove', {'url': watching})[0], 200)
        self.assertEqual(tv.call('POST', '/api/history/remove', {'url': watching})[0], 400)
        friend.history.started(watching, {'title': 'Long talk'})
        self.assertEqual(tv.call('POST', '/api/history/clear')[0], 200)
        self.assertEqual(tv.call('GET', '/api/history')[1], {'items': []})

        # The iPhone Home Screen app has its own storage: a code from a signed-in device signs it in.
        status, code = owner.call('POST', '/api/devices/code')
        self.assertEqual(status, 200)
        self.assertIn('/join/', code['link'])
        app = Client(self.port)
        status, result = app.call('POST', '/api/devices/redeem', {'code': code['code'].lower(), 'kind': 'iPhone app'})
        self.assertEqual(status, 200)
        self.assertTrue(app.call('GET', '/api/me')[1]['user']['admin'])
        self.assertEqual(Client(self.port).call('POST', '/api/devices/redeem', {'code': 'WRONGCODE1'})[0], 400)
        server.FAILED_ATTEMPTS.clear()
        # On an iPhone the welcome page explains Add to Home Screen and shows the code to enter there.
        iphone = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'}
        page = Client(self.port).call('GET', '/join/' + code['code'].replace('-', ''), headers=iphone)[1]
        self.assertIn('Add to Home Screen', page)
        self.assertIn(code['code'], page)
        # The web app files are served without signing in.
        for path in ('/manifest.webmanifest', '/icons/apple-touch-icon.png', '/icons/icon-512.png'):
            self.assertEqual(Client(self.port).call('GET', path)[0], 200, path)

        # Other websites cannot send commands, even with a signed-in browser.
        self.assertEqual(owner.call('POST', '/api/stop', headers={'Origin': 'http://evil.example'})[0], 403)

        # Removing a person signs out all their devices.
        owner.call('POST', '/api/people/remove', {'id': friend_id})
        self.assertEqual(tv.call('GET', '/api/me')[0], 401)
        self.assertEqual(phone.call('GET', '/api/me')[0], 401)
        self.assertEqual(owner.call('POST', '/api/people/remove', {'id': server.users.owner()})[0], 400)

    def test_guessing_invite_codes_is_blocked(self):
        guesser = Client(self.port)
        for _ in range(10):
            guesser.call('GET', '/join/AAAAAAAAAA')
        self.assertEqual(guesser.call('GET', '/join/AAAAAAAAAA')[0], 429)
        server.FAILED_ATTEMPTS.clear()

    def fetch(self, path, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        return response, response.read()

    def test_player_files_are_compressed_and_only_sent_again_when_they_change(self):
        source = (Path(server.ROOT) / 'public/player.js').read_bytes()
        plain, body = self.fetch('/player.js')
        self.assertEqual((plain.status, body), (200, source))
        self.assertIsNone(plain.getheader('Content-Encoding'))
        self.assertEqual(plain.getheader('Cache-Control'), 'no-cache')    # screens ask every time: updates arrive at once
        packed, body = self.fetch('/player.js', **{'Accept-Encoding': 'gzip, deflate'})
        self.assertEqual(packed.getheader('Content-Encoding'), 'gzip')
        self.assertEqual(server.gzip.decompress(body), source)
        self.assertLess(len(body), len(source) / 2)
        self.assertEqual(int(packed.getheader('Content-Length')), len(body))
        same, body = self.fetch('/player.js', **{'If-None-Match': plain.getheader('ETag'), 'Accept-Encoding': 'gzip'})
        self.assertEqual((same.status, body), (304, b''))
        changed, body = self.fetch('/player.js', **{'If-None-Match': '"an-older-copy"'})
        self.assertEqual((changed.status, body), (200, source))
        # Images are compressed already; the page itself is never kept (it carries this server's token).
        icon, body = self.fetch('/icons/favicon-32.png', **{'Accept-Encoding': 'gzip'})
        self.assertIsNone(icon.getheader('Content-Encoding'))
        page, body = self.fetch('/', **{'Accept-Encoding': 'gzip'})
        self.assertEqual((page.getheader('Content-Encoding'), page.getheader('Cache-Control')), ('gzip', 'no-store'))
        self.assertIn(server.TOKEN.encode(), server.gzip.decompress(body))
        # Small answers such as the status that is asked every two seconds are sent as they are.
        health, body = self.fetch('/api/health', **{'Accept-Encoding': 'gzip'})
        self.assertIsNone(health.getheader('Content-Encoding'))
        self.assertEqual(json.loads(body)['ok'], True)
        self.assertEqual(self.fetch('/downloads/something-else')[0].status, 404)

    def test_owner_invite_only_from_the_server_itself(self):
        class Fake(server.Handler):
            def __init__(self):
                self.client_address = ('192.168.1.20', 1)
        self.assertFalse(server.ipaddress.ip_address(Fake().client_address[0]).is_loopback)


    def test_work_only_while_the_app_is_in_use(self):
        owner = Client(self.port)
        owner.call('GET', '/join/' + owner.call('GET', '/api/owner-invite')[1]['code'])
        me = server.profile(server.users.owner())
        saved = me.relay.state, me.relay.run, me.relay.stream
        try:
            # Nobody has a screen open: followed channels are not refreshed.
            me.seen = 0
            self.assertFalse(me.feeds.active())
            # Opening the app catches up at once; live results from hours ago are checked again first.
            server.checker.results, server.checker.checked = {'old': {'live': True}}, time.time() - server.STALE_AFTER - 1
            # (This person's followed channels run in a thread that takes its wake-up at once; its test covers it.)
            server.checker.wake.clear()
            self.assertEqual(owner.call('GET', '/api/streams')[0], 200)
            self.assertTrue(me.feeds.active())
            self.assertEqual(server.checker.results, {})
            self.assertTrue(server.checker.wake.is_set())
            # While it stays open nothing is restarted on every request.
            server.checker.wake.clear()
            owner.call('GET', '/api/status')
            self.assertFalse(server.checker.wake.is_set())

            # A stream that no screen plays or asks about stops by itself; one that is being played does not.
            me.relay.state, me.relay.run, me.relay.stream = 'playing', 'run-abc', {'name': 'Left on'}
            me.seen = time.time() - server.ABANDONED_AFTER - 1
            server.watched('run-abc')        # a screen fetched a piece of it (an iPhone playing in the background)
            server.stop_abandoned()
            self.assertEqual(me.relay.state, 'playing')
            me.seen = time.time() - server.ABANDONED_AFTER - 1
            server.watched('another-run')
            server.stop_abandoned()
            self.assertEqual(me.relay.state, 'idle')
        finally:
            me.relay.stop()
            me.relay.state, me.relay.run, me.relay.stream = saved
            server.checker.results = {}


class OwnerTest(unittest.TestCase):
    def test_a_new_server_gets_one_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = server.users
            server.users = Users(Path(folder) / 'users.json')
            try:
                server.create_owner()
                server.create_owner()   # running again changes nothing
                self.assertEqual([person['admin'] for person in server.users.all()], [True])
            finally:
                server.users = saved


if __name__ == '__main__':
    unittest.main()
