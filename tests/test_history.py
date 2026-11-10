"""Watch history: what each person watched and where they stopped."""
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import history  # noqa: E402


class Images:
    def register(self, key, url, version):
        return '/api/img/%s?v=%s' % (key, version) if url else None


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / 'history.json'
        self.history = history.WatchHistory(self.path, Images())

    def start(self, vid='1000000001', title='A video', duration=1200):
        url = 'https://www.twitch.tv/videos/' + vid
        self.history.started(url, {'title': title, 'channel': 'Chan', 'duration': duration, 'thumbnail': 'https://img/x.jpg'},
                             {'platform': 'Twitch'})
        return url

    def test_keeps_position_across_restarts(self):
        url = self.start()
        self.assertIsNone(self.history.position('https://www.twitch.tv/videos/999'))
        self.assertEqual(self.history.position(url), 0)
        self.history.progress(url, 300.7, 1200, 'pause')
        # Another screen, a restarted server.
        again = history.WatchHistory(self.path, Images())
        self.assertEqual(again.position(url), 300)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        card = again.items()[0]
        self.assertEqual((card['title'], card['name'], card['platform'], card['progress'], card['source']),
                         ('A video', 'Chan', 'Twitch', 0.25, 'history'))
        self.assertTrue(card['thumb'].startswith('/api/img/h-'))

    def test_continue_watching_and_finishing(self):
        first = self.start('aaaaaaaaaaa', 'First')
        second = self.start('bbbbbbbbbbb', 'Second')
        self.start('ccccccccccc', 'Barely started')
        self.history.progress(first, 600, None, 'pause')
        self.history.progress(second, 100, None, 'pause')
        self.history.progress('https://www.twitch.tv/videos/ccccccccccc', 10, None, 'pause')
        titles = {c['title'] for c in self.history.continuing()}
        self.assertEqual(titles, {'First', 'Second'})            # under 30 seconds is not "started"
        # The last minute counts as finished; finished videos leave the row and start over next time.
        self.history.progress(first, 1170)
        self.assertEqual([c['title'] for c in self.history.continuing()], ['Second'])
        self.assertEqual(self.history.position(first), 0)
        self.assertEqual(self.history.items()[-1]['progress'], 1)
        self.history.progress(second, 50, None, 'ended')
        self.assertEqual(self.history.continuing(), [])
        # Watching a finished video again puts it back in progress.
        self.history.progress(first, 200)
        self.assertEqual([c['title'] for c in self.history.continuing()], ['First'])

    def test_unknown_videos_and_bad_numbers(self):
        self.assertFalse(self.history.progress('https://www.twitch.tv/videos/404', 100))
        url = self.start()
        with self.assertRaises(ValueError):
            self.history.progress(url, 'later')
        self.assertTrue(self.history.progress(url, 99999, None, 'pause'))
        self.assertEqual(self.history.items()[0]['finished'], True)   # clamped to the length

    def test_newest_first_with_a_limit(self):
        with mock.patch.object(history, 'LIMIT', 3):
            for vid in ('aaaaaaaaaaa', 'bbbbbbbbbbb', 'ccccccccccc', 'ddddddddddd', 'bbbbbbbbbbb'):
                self.start(vid, vid[0])
        self.assertEqual([i['title'] for i in self.history.items()], ['b', 'd', 'c'])

    def test_ticks_are_written_now_and_then(self):
        url = self.start()
        self.history.progress(url, 100, None, 'pause')
        changes = self.history.changes
        self.history.progress(url, 110)                               # playing: no library refresh, no write yet
        self.assertEqual(self.history.changes, changes)
        self.assertEqual(json.loads(self.path.read_text())['videos'][0]['position'], 100)
        self.history.saved = 0
        self.history.progress(url, 120)
        self.assertEqual(json.loads(self.path.read_text())['videos'][0]['position'], 120)

    def test_remove_and_clear(self):
        url = self.start()
        self.start('bbbbbbbbbbb')
        self.history.remove(url)
        self.assertIsNone(self.history.position(url))
        with self.assertRaises(ValueError):
            self.history.remove(url)
        self.history.clear()
        self.assertEqual(history.WatchHistory(self.path, Images()).items(), [])

    def test_unreadable_file_starts_empty(self):
        self.path.write_text('not json')
        self.assertEqual(history.WatchHistory(self.path, Images()).items(), [])


if __name__ == '__main__':
    unittest.main()
