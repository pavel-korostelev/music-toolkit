import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.spotify_export import Exporter, ExportError, normalize_track, paginate, account_key
from engine.spotify_session import check_request, QUERY_URL


def song(i='A', **updates):
    data = {'uri': 'spotify:track:' + str(i), 'name': 'Song (Live)',
            'artists': {'items': [{'profile': {'name': 'Artist'}}]},
            'albumOfTrack': {'name': 'Album'}, 'duration': {'totalMilliseconds': 201123},
            'playability': {'playable': True}}
    data.update(updates)
    return {'itemV2': {'data': data}}


def page(items, offset, limit):
    return {'items': items[offset:offset+limit], 'totalCount': len(items)}


class FakeBackend:
    def __init__(self):
        self.liked = [song(i) for i in range(121)]
        self.calls = []
        self.roots = [{'item': {'data': {'uri': 'spotify:playlist:P'+str(i), 'name': 'List'}}} for i in range(51)]
        self.roots.append({'item': {'data': {'uri': 'spotify:user:user:folder:F', '__typename': 'Folder', 'name': 'Folder'}}})
    def account_id(self): return 'test-account'
    def likes(self, offset, limit):
        self.calls.append(('likes', offset))
        return page(self.liked, offset, limit)
    def library(self, folder, offset, limit):
        self.calls.append((folder, offset))
        items = self.roots if folder is None else [{'item': {'data': {'uri': 'spotify:playlist:NESTED', 'name': 'Nested'}}}]
        return page(items, offset, limit)
    def playlist(self, uri, offset, limit):
        return page([song('one'), {'itemV2': {'data': {'uri': 'spotify:local:artist:album:title:123'}}},
                     {'itemV2': {'data': {'uri': 'spotify:episode:podcast'}}}, song('missing', duration={})], offset, limit)


class ExportTests(unittest.TestCase):
    def run_export(self, backend=None, sample=0):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        backend = backend or FakeBackend()
        with patch('builtins.print'):
            path, data = Exporter(backend, directory.name, sample).run()
        return backend, path, data

    def test_all_pages_and_nested_folders(self):
        backend, path, data = self.run_export()
        self.assertTrue(data['complete'])
        self.assertEqual(len(data['playlists']), 53)
        self.assertEqual(len(data['playlists'][0]['tracks']), 121)
        self.assertIn(('likes', 100), backend.calls)
        self.assertIn((None, 50), backend.calls)
        self.assertTrue(data['playlists'][-1]['folder_path'])
        self.assertEqual(data, json.loads(path.read_text()))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_skips_count_as_positions_missing_duration_is_review(self):
        _, _, data = self.run_export()
        p = data['playlists'][1]
        self.assertEqual(p['counts'], {'expected': 4, 'received': 4, 'tracks': 2, 'skipped': 2})
        self.assertEqual({s['reason'] for s in p['skipped']}, {'local_file', 'podcast_or_audiobook'})
        self.assertIsNone(p['tracks'][-1]['duration_ms'])
        self.assertIn('missing_metadata', p['tracks'][-1]['review_reasons'])

    def test_sample_is_never_full(self):
        _, _, data = self.run_export(sample=5)
        self.assertFalse(data['complete'])
        self.assertEqual(len(data['playlists'][0]['tracks']), 5)
        self.assertEqual(data['mode'], 'sample')

    def test_sample_nested_playlist_budget_is_truncation_not_error(self):
        backend = FakeBackend()
        roots = backend.roots[:4] + [backend.roots[-1]]
        nested = [{'item': {'data': {'uri': 'spotify:playlist:N'+str(i), 'name': 'Nested'}}} for i in range(5)]
        backend.library = lambda f,o,n: page(roots if f is None else nested,o,n)
        _, _, data = self.run_export(backend, sample=5)
        self.assertEqual(data['errors'], [])
        self.assertIn('sample_playlist_limit', {t['code'] for t in data['truncations']})
        self.assertEqual(len(data['playlists']), 6)  # likes plus five playlists
        self.assertFalse(data['complete'])
        from engine.sources import require_complete_spotify
        with self.assertRaises(ValueError): require_complete_spotify(data)
        # A full run of exactly the same library must not inherit sample limits.
        _, _, full = self.run_export(backend, sample=0)
        self.assertEqual(full['errors'], [])
        self.assertEqual(full['truncations'], [])
        self.assertEqual(len(full['playlists']), 10)
        require_complete_spotify(full)

    def test_sample_folder_and_page_limits_are_separate_from_errors(self):
        backend = FakeBackend()
        folders = [{'item': {'data': {'uri': 'spotify:user:test:folder:'+str(i), 'name': 'Folder'}}} for i in range(5)]
        backend.library = lambda f,o,n: page(folders if f is None else [],o,n)
        _, _, data = self.run_export(backend, sample=5)
        self.assertEqual(data['errors'], [])
        self.assertIn('sample_folder_limit', {t['code'] for t in data['truncations']})
        self.assertLessEqual(len(data['library_pages']), 5)
        _, _, data = self.run_export(sample=5)
        self.assertEqual(data['errors'], [])
        codes = {t['code'] for t in data['truncations']}
        self.assertIn('sample_library_page_limit', codes)
        self.assertIn('sample_track_limit', codes)

    def test_sample_still_reports_schema_errors_and_folder_cycles(self):
        for item in ({'unknownShape': True}, {'item': {'data': {'uri': 'spotify:user:test:folder:cycle', 'name': 'Folder'}}}):
            backend = FakeBackend()
            backend.library = lambda f,o,n: page([item],o,n)
            _, _, data = self.run_export(backend, sample=5)
            self.assertTrue(data['errors'])
            self.assertFalse(data['complete'])

    def test_partial_saved_without_exception_text_or_extra_fields(self):
        backend = FakeBackend()
        original = backend.likes
        def likes(offset, limit):
            if offset: raise RuntimeError('SECRET_SESSION_SENTINEL')
            result = original(offset, limit)
            result['cookies'] = 'SECRET_SESSION_SENTINEL'
            result['items'][0]['itemV2']['data']['accessToken'] = 'SECRET_SESSION_SENTINEL'
            return result
        backend.likes = likes
        _, path, data = self.run_export(backend)
        self.assertFalse(data['complete'])
        self.assertEqual(len(data['playlists'][0]['tracks']), 50)
        self.assertNotIn('SECRET_SESSION_SENTINEL', path.read_text())
        self.assertTrue(data['errors'])

    def test_unknown_library_shape_is_partial(self):
        backend = FakeBackend()
        backend.roots = [{'unknownShape': True}]
        _, _, data = self.run_export(backend)
        self.assertFalse(data['complete'])

    def test_counters_repeats_empty_and_changed_totals(self):
        scenarios = [lambda o,n: {'items': [], 'totalCount': 1},
                     lambda o,n: {'items': [song()], 'totalCount': 3},
                     lambda o,n: {'items': [song(o)], 'totalCount': 3+o},
                     lambda o,n: {'items': [song()]},
                     lambda o,n: {'items': [song()], 'totalCount': 0}]
        for fetch in scenarios:
            with self.subTest(fetch=fetch), self.assertRaises(ExportError):
                paginate(fetch, lambda *a: None)

    def test_short_pages_continue(self):
        seen = []
        paginate(lambda o,n: page([song(i) for i in range(3)], o, 1),
                 lambda items,o,t: seen.extend(items))
        self.assertEqual(len(seen), 3)

    def test_folder_cycle_is_bounded_and_partial(self):
        backend = FakeBackend()
        folder = {'item': {'data': {'uri': 'spotify:user:user:folder:F', 'name': 'F'}}}
        backend.library = lambda f,o,n: page([folder], o, n)
        _, _, data = self.run_export(backend)
        self.assertFalse(data['complete'])
        self.assertTrue(any(e['code'] == 'folder_cycle_or_duplicate' for e in data['errors']))

    def test_account_paths_are_distinct_and_safe(self):
        self.assertNotEqual(account_key('A'), account_key('B'))
        self.assertNotIn('/', account_key('../../escape'))

    def test_invalid_durations_and_unavailable(self):
        for duration in (None, 0, -1, True, float('nan'), float('inf'), '200'):
            track, _ = normalize_track(song(duration={'totalMilliseconds': duration}), 1)
            self.assertIsNone(track['duration_ms'])
        track, _ = normalize_track(song(playability={'playable': False}), 1)
        self.assertIn('unavailable_in_spotify', track['review_reasons'])
        self.assertEqual(track['title'], 'Song (Live)')


class NetworkTests(unittest.TestCase):
    def test_allow_only_known_read_queries(self):
        for op in ('libraryV3', 'fetchLibraryTracks', 'fetchPlaylist'):
            check_request('POST', QUERY_URL, {'params': {'operationName': op}})
        for op in ('addToLibrary', 'removeFromPlaylist', 'createPlaylist', None):
            with self.assertRaises(ExportError):
                check_request('POST', QUERY_URL, {'params': {'operationName': op}})

    def test_block_other_hosts_methods_and_redirect_targets(self):
        for method, url in [('PUT', 'https://www.spotify.com/api/account-settings/v1/profile'),
                            ('POST', 'https://spclient.wg.spotify.com/player'),
                            ('GET', 'https://evil.example/'),
                            ('GET', 'http://open.spotify.com'),
                            ('GET', 'https://open.spotify.com.evil.example/')]:
            with self.assertRaises(ExportError): check_request(method, url, {})

    def test_browser_and_backend_not_imported_with_export_logic(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, '-B', '-c',
            'import sys; import agent; import music_tool; import engine.spotify_export; '
            'assert "spotapi" not in sys.modules; assert "playwright" not in sys.modules'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class FailureTests(unittest.TestCase):
    def test_rate_limit_stops_export_and_preserves_partial(self):
        backend = FakeBackend()
        backend.likes = lambda *a: (_ for _ in ()).throw(ExportError('rate_limited_retry_later'))
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.print'):
            path, data = Exporter(backend, tmp, 0).run()
        self.assertFalse(data['complete'])
        self.assertEqual(backend.calls, [])
        self.assertTrue(data['errors'])

    def test_retry_budget_sanitizes_exception(self):
        from engine.spotify_session import Backend
        backend = Backend.__new__(Backend)
        with patch('engine.spotify_session.time.sleep') as sleep:
            calls = []
            def fail():
                calls.append(1)
                raise RuntimeError('SECRET_SESSION_SENTINEL')
            with self.assertRaises(ExportError) as error:
                backend.call(fail)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn('SECRET_SESSION_SENTINEL', str(error.exception))
        from engine.spotify_session import deadline
        import time
        with self.assertRaises(ExportError):
            with deadline(0.01):
                try:
                    time.sleep(0.1)
                except Exception:
                    self.fail('Upstream could swallow the deadline')

    def test_renamed_json_with_credentials_rejected(self):
        from scripts.build_archive import validate_content
        for key in ('cookies', 'sp_dc', 'accessToken', 'password'):
            with self.assertRaises(ValueError):
                validate_content('innocent.json', json.dumps({'nested': {key: 'test-placeholder'}}))


class CompletenessTests(unittest.TestCase):
    def test_unknown_track_shape_cannot_be_complete(self):
        backend = FakeBackend()
        backend.liked = [{'new_unsupported_wrapper': {'name': 'Song'}}]
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.print'):
            _, data = Exporter(backend, tmp, 0).run()
        self.assertFalse(data['complete'])
        self.assertEqual(data['playlists'][0]['skipped'][0]['reason'], 'unknown_item_shape')

    def test_apply_requires_counters_not_just_complete_flag(self):
        from engine.sources import require_complete_spotify
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.print'):
            _, data = Exporter(FakeBackend(), tmp, 0).run()
        require_complete_spotify(data)
        data['playlists'][0]['tracks'].pop()
        with self.assertRaises(ValueError): require_complete_spotify(data)
        with self.assertRaises(ValueError): require_complete_spotify({'complete': True, 'mode': 'full'})

    def test_full_export_with_truncations_cannot_be_applied(self):
        from engine.sources import require_complete_spotify
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.print'):
            _, data = Exporter(FakeBackend(), tmp, 0).run()
        data.pop('truncations')
        require_complete_spotify(data)  # Old schema-2 exports remain valid.
        data['truncations'] = [{'stage': 'library', 'code': 'sample_playlist_limit'}]
        with self.assertRaises(ValueError): require_complete_spotify(data)


class ObservedResponseTests(unittest.TestCase):
    def test_liked_track_uri_on_wrapper(self):
        data = song('liked')['itemV2']['data']
        uri = data.pop('uri')
        raw = {'track': {'_uri': uri, 'data': data}}
        original = copy.deepcopy(raw)
        track, skipped = normalize_track(raw, 1)
        self.assertIsNone(skipped)
        self.assertEqual(track['source_id'], 'liked')
        self.assertEqual(track['duration_ms'], 201123)
        self.assertEqual(track['review_reasons'], [])
        self.assertEqual(raw, original)

    def test_playlist_track_duration_variant(self):
        raw = song('playlist')
        data = raw['itemV2']['data']
        data['trackDuration'] = data.pop('duration')
        track, skipped = normalize_track(raw, 1)
        self.assertIsNone(skipped)
        self.assertEqual(track['duration_ms'], 201123)
        self.assertEqual(track['review_reasons'], [])
        for value in (None, 0, -1, True, '201123'):
            data['trackDuration'] = {'totalMilliseconds': value}
            track, _ = normalize_track(raw, 1)
            self.assertIsNone(track['duration_ms'])
            self.assertIn('missing_metadata', track['review_reasons'])

    def test_explicit_uri_wins_and_absent_duration_stays_unknown(self):
        raw = song('actual')
        raw['itemV2']['_uri'] = 'spotify:track:other'
        raw['itemV2']['data'].pop('duration')
        track, _ = normalize_track(raw, 1)
        self.assertEqual(track['source_id'], 'actual')
        self.assertIsNone(track['duration_ms'])
