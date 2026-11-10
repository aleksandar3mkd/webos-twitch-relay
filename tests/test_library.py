"""Stream link validation and the shared library. Run: .venv/bin/python -m unittest discover tests"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MY_STREAMS_DATA', tempfile.mkdtemp())   # never touch the real data/
import server  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_twitch_names_and_links(self):
        for value in ('Somechannel', 'twitch.tv/somechannel', 'https://m.twitch.tv/somechannel/videos?x=1'):
            self.assertEqual(server.normalize_stream(value), 'https://www.twitch.tv/somechannel')

    def test_only_twitch(self):
        for value in ('https://www.youtube.com/@somebody/live', 'https://kick.com/somebody',
                      'https://example.com/live/index.m3u8', 'https://twitch.tv.example.com/somechannel'):
            with self.assertRaisesRegex(ValueError, 'Twitch only', msg=value):
                server.normalize_stream(value)

    def test_rejections(self):
        for value in ('', 'https://www.twitch.tv/directory', 'https://example.com/page',
                      'ftp://example.com/a.m3u8', '-o /tmp/x', 'https://' + 'a' * 600 + '.com/'):
            with self.assertRaises(ValueError, msg=value):
                server.normalize_stream(value)

    def test_names(self):
        self.assertEqual(server.describe('https://www.twitch.tv/abc')['name'], 'abc')
        self.assertEqual(server.describe('https://www.twitch.tv/abc', ' My\n  pick ')['name'], 'My pick')
        self.assertEqual(server.describe('https://www.twitch.tv/videos/123')['name'], 'Twitch video')
        self.assertEqual(server.describe('https://www.twitch.tv/abc')['platform'], 'Twitch')


class LibraryTest(unittest.TestCase):
    def test_add_dedupe_rename_remove_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data' / 'streams.json'
            self.assertEqual(server.Library(path).all(), [])   # a new person starts empty
            library = server.Library(path)
            library.add('somechannel')
            self.assertEqual([s['name'] for s in library.all()], ['somechannel'])
            stream, created = library.add('https://www.twitch.tv/abc')
            self.assertTrue(created)
            again, created = library.add('twitch.tv/ABC', 'ABC the great')
            self.assertFalse(created)
            self.assertEqual(again['id'], stream['id'])
            self.assertEqual(server.Library(path).get(stream['id'])['name'], 'ABC the great')
            with self.assertRaisesRegex(ValueError, 'played, not saved'):
                library.add('https://www.twitch.tv/videos/123')
            version = library.version
            library.remove(stream['id'])
            self.assertNotEqual(library.version, version)
            self.assertEqual(len(server.Library(path).all()), 1)

    def test_unreadable_file_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'streams.json'
            path.write_text('{broken')
            self.assertEqual(server.Library(path).all(), [])
            self.assertEqual(len(list(Path(folder).glob('streams.unreadable-*.json'))), 1)


class QualityTest(unittest.TestCase):
    def test_settings_persist_and_validate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            self.assertEqual(server.Settings(path).quality, server.DEFAULT_QUALITY)
            server.Settings(path).set_quality('1080p60')
            self.assertEqual(server.Settings(path).quality, '1080p60')
            with self.assertRaises(ValueError):
                server.Settings(path).set_quality('4k')

    def test_encoder_presets(self):
        args = server.encoder_args('1080p60')
        self.assertIn("scale='min(1920,iw)':'min(1080,ih)'", args[1])
        self.assertEqual(args[args.index('-r') + 1], '60')
        self.assertEqual(args[args.index('-g') + 1], '120')
        self.assertEqual(server.encoder_args('720p')[server.encoder_args('720p').index('-b:v') + 1], '2200k')


class LiveCheckerTest(unittest.TestCase):
    def test_results_images_and_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            library = server.Library(Path(folder) / 'streams.json')
            library.add('somechannel')
            other, _ = library.add('otherchannel')

            def probe(stream):
                if stream['id'] == other['id']:
                    return {'live': True, 'checked': 900, 'title': 'Hi', 'category': 'Chess', 'image': 'https://img/x.webp'}
                raise RuntimeError('site unreachable')

            images = server.ImageCache()
            checker = server.LiveChecker(library.all, images, probe=probe)
            before = checker.version
            checker.check_all()
            self.assertNotEqual(checker.version, before)
            merged = {s['id']: s for s in checker.merged(library)}
            self.assertEqual(merged[other['id']]['thumb'], '/api/img/s-%s?v=3' % other['id'])
            self.assertEqual(images.sources['s-' + other['id']], 'https://img/x.webp')
            failed = next(s for s in merged.values() if s['id'] != other['id'])
            self.assertIsNone(failed['live'])
            self.assertIsNone(failed['thumb'])
            self.assertIn('unreachable', failed['error'])

    def test_checks_only_while_the_app_is_in_use(self):
        with tempfile.TemporaryDirectory() as folder:
            library = server.Library(Path(folder) / 'streams.json')
            library.add('somechannel')
            in_use, probed, done = [False], [], server.threading.Event()

            def probe(stream):
                probed.append(stream['id'])
                done.set()
                return {'live': True, 'checked': 1}

            checker = server.LiveChecker(library.all, server.ImageCache(), probe=probe, active=lambda: in_use[0])
            with mock.patch.object(server, 'CHECK_INTERVAL', 0.01):
                checker.start()
                server.time.sleep(0.1)
                self.assertEqual(probed, [])                     # nobody has the app open: no site is asked
                in_use[0] = True
                self.assertTrue(done.wait(2))
                in_use[0] = False
            # Back soon: the last results still show while the new check runs.
            for _ in range(100):
                if checker.merged(library)[0].get('live'):
                    break
                server.time.sleep(0.01)
            before = checker.version
            checker.resume()
            self.assertTrue(checker.wake.is_set())
            self.assertTrue(checker.merged(library)[0]['live'])
            # Back after hours: what was live then is not presented as live now.
            checker.checked -= server.STALE_AFTER + 1
            checker.resume()
            self.assertNotIn('live', checker.merged(library)[0])
            self.assertNotEqual(checker.version, before)

    def test_thumbnail_urls(self):
        self.assertEqual(server.thumbnail_url('https://www.twitch.tv/somechannel'),
                         'https://static-cdn.jtvnw.net/previews-ttv/live_user_somechannel-440x248.jpg')


class ImageCacheTest(unittest.TestCase):
    def test_fetches_once_and_keeps_last_good_copy(self):
        calls = []

        class Response:
            def __init__(self, code):
                self.status_code, self.content, self.headers = code, b'jpeg', {'Content-Type': 'image/jpeg'}

        class Http:
            def get(self, url, timeout):
                calls.append(url)
                return Response(200 if len(calls) == 1 else 500)

        images = server.ImageCache(Http())
        self.assertEqual(images.register('k', 'https://a/1.jpg', 7), '/api/img/k?v=7')
        self.assertEqual(images.get('k'), ('image/jpeg', b'jpeg'))
        self.assertEqual(images.get('k'), ('image/jpeg', b'jpeg'))
        self.assertEqual(len(calls), 1)
        images.cache['k'] = ('https://a/1.jpg', 0, 'image/jpeg', b'jpeg')   # expired
        self.assertEqual(images.get('k'), ('image/jpeg', b'jpeg'))        # failed refresh keeps old copy
        self.assertIsNone(images.get('unknown'))


class VideoLinkTest(unittest.TestCase):
    def test_video_links(self):
        self.assertTrue(server.is_video_url('https://www.twitch.tv/videos/123'))
        self.assertFalse(server.is_video_url('https://www.twitch.tv/name'))
        self.assertEqual(server.normalize_stream('https://www.twitch.tv/videos/123?t=5'), 'https://www.twitch.tv/videos/123')
        self.assertIn('[vcodec^=avc1][height<=720]', server.video_format('720p'))


class IPv4Test(unittest.TestCase):
    def test_streams_use_ipv4_unless_allowed(self):
        self.assertEqual(server.ipv4_args('--force-ipv4'), ['--force-ipv4'])
        server.IPV4_ONLY = False
        try:
            self.assertEqual(server.ipv4_args('--force-ipv4'), [])
        finally:
            server.IPV4_ONLY = True


class VideoLookupTest(unittest.TestCase):
    def test_videos_are_cached_but_live_broadcasts_are_not(self):
        calls = []

        def extract(url, quality):
            calls.append(url)
            return {'is_live': 'live' in url}

        lookups = server.VideoLookups(extract)
        for _ in range(2):
            lookups.get('https://t/v1', '1080p')
            lookups.get('https://t/live', '1080p')
        self.assertEqual(calls, ['https://t/v1', 'https://t/live', 'https://t/live'])
        lookups.get('https://t/v1', '720p')   # another quality picks other formats
        self.assertEqual(calls[-1], 'https://t/v1')


class ProbeTest(unittest.TestCase):
    """Each live check opens connections; they must be closed (they once used up every open file on the server)."""

    def session(self, live):
        plugin = mock.Mock()
        plugin.streams.return_value = {'best': object()} if live else {}
        plugin.get_title.return_value = 'Title'
        session = mock.Mock()
        session.resolve_url.return_value = ('other', lambda s, url: plugin, 'https://www.twitch.tv/x')
        return session

    def test_closes_its_connections(self):
        for live in (False, True):
            session = self.session(live)
            with mock.patch('streamlink.Streamlink', return_value=session):
                server.probe_stream({'url': 'https://www.twitch.tv/x'})
            session.http.close.assert_called_once()

    def test_closes_them_when_the_check_fails(self):
        session = self.session(True)
        session.resolve_url.side_effect = RuntimeError('no plugin')
        with mock.patch('streamlink.Streamlink', return_value=session):
            with self.assertRaises(RuntimeError):
                server.probe_stream({'url': 'https://www.twitch.tv/x'})
        session.http.close.assert_called_once()


class RelayStatusTest(unittest.TestCase):
    """Every screen asks for the status every two seconds: it has to stay cheap."""

    def test_playlist_is_read_until_playback_is_ready_and_not_after(self):
        person = mock.Mock()
        person.version.return_value = 'v'
        person.settings.quality = '1080p'
        person.history.position.return_value = None
        relay = server.Relay(person)
        with tempfile.TemporaryDirectory() as folder:
            relay.folder, relay.state, relay.kind, relay.run = Path(folder), 'starting', 'video', 'r1'
            relay.stream = {'url': 'https://y/v1', 'name': 'One'}
            relay.started = server.time.monotonic()
            relay.processes = [mock.Mock(**{'poll.return_value': None})]
            self.assertIsNone(relay.status()['url'])               # nothing has arrived yet
            playlist = relay.folder / 'index.m3u8'
            playlist.write_text('#EXTM3U\n#EXTINF:4.0,\npart000000.ts\n')
            status = relay.status()
            self.assertEqual((status['state'], status['url']), ('playing', '/media/%s/index.m3u8' % relay.folder.name))
            with mock.patch.object(Path, 'read_text', side_effect=AssertionError('read again')):
                self.assertEqual(relay.status()['url'], status['url'])
            # A download that stops is still noticed.
            relay.processes = [mock.Mock(**{'poll.return_value': 1})]
            self.assertEqual(relay.status()['state'], 'error')


if __name__ == '__main__':
    unittest.main()
