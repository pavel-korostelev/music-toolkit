import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent
import music_tool
from engine import sources, transfer

ROOT = Path(__file__).resolve().parents[1]


def legacy():
    return {'playlists': [{'tracks': [{'yandex_id': '123', 'title': 'Song', 'artists': ['Artist'],
                                      'albums': ['Album'], 'duration_ms': 200000, 'version': None}]}]}


def spotify(account='one', complete=True):
    track = dict(legacy()['playlists'][0]['tracks'][0])
    track['source_id'] = track.pop('yandex_id')
    return {'schema_version': 2, 'source': 'spotify', 'account_id': account,
            'complete': complete, 'mode': 'full', 'errors': [], 'playlists': [{'tracks': [track]}]}


class CompatibilityTests(unittest.TestCase):
    def test_legacy_identity_exact_and_directory_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'yandex_library.json'
            path.write_text(json.dumps(legacy()))
            expected = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'playlist': 'ЯM', 'source_count': 1}
            self.assertEqual(music_tool.transfer_identity(path, 'ЯM'), expected)
            self.assertEqual(sources.transfer_directory(Path(tmp), legacy()), Path(tmp))
            self.assertEqual(list(sources.records(legacy())), ['123'])

    def test_old_import_metadata_allows_resume_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            path = data/'yandex_library.json'; path.write_text(json.dumps(legacy()))
            metadata = data/'transfer/import.json'; metadata.parent.mkdir()
            old = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'playlist': 'ЯM', 'source_count': 1}
            metadata.write_text(json.dumps(old))
            with patch.object(music_tool, 'run_engine', return_value=0) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(music_tool.main(['--data-dir', tmp, 'transfer', str(path), '--apply']), 0)
            self.assertEqual(json.loads(metadata.read_text()), old)
            self.assertEqual(run.call_args.args[2], data.resolve())

    def test_catalog_skips_old_verified_state_without_ui_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp); export = data/'old.json'; export.write_text(json.dumps(legacy()))
            state = data/'catalog-progress.json'
            old_entry = {'source': legacy()['playlists'][0]['tracks'][0], 'status': 'verified', 'apple': {'id': 'APPLE'}}
            state.write_text(json.dumps({'playlist': 'ЯM', 'tracks': {'123': old_entry}}))
            with patch.dict('os.environ', {'MUSIC_TOOL_STATE_DIR': tmp}), patch.object(sys, 'path', [str(ROOT/'engine')]+sys.path):
                spec = importlib.util.spec_from_file_location('compat_catalog', ROOT/'engine/catalog_transfer.py')
                mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            with patch.object(sys, 'argv', ['catalog_transfer.py',str(export),'--state',str(state)]), patch.object(mod, 'read_library', return_value=[]), patch.object(mod, 'copy_track') as copy_track, patch.object(mod, 'bridge') as bridge, contextlib.redirect_stdout(io.StringIO()):
                mod.main()
            copy_track.assert_not_called(); bridge.assert_not_called()
            self.assertEqual(json.loads(state.read_text())['tracks']['123'], old_entry)

    def test_source_and_account_keys_and_state_are_distinct(self):
        sets = [set(sources.records(s)) for s in (legacy(), spotify('one'), spotify('two'))]
        self.assertFalse(sets[0] & sets[1]); self.assertFalse(sets[1] & sets[2])
        self.assertNotEqual(sources.transfer_directory(Path('data'),spotify('one')), sources.transfer_directory(Path('data'),spotify('two')))
        self.assertNotIn('yandex_id', next(iter(sources.records(spotify()).values())))
        external = spotify()
        external['playlists'][0]['tracks'][0]['cookies'] = 'SECRET_SESSION_SENTINEL'
        self.assertNotIn('SECRET_SESSION_SENTINEL', json.dumps(sources.records(external)))

    def test_spotify_preview_missing_duration_is_review(self):
        export = spotify(); export['playlists'][0]['tracks'][0]['duration_ms'] = None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'export.json'; path.write_text(json.dumps(export))
            report = Path(tmp)/'report.json'
            with patch.object(sys, 'argv', ['transfer.py',str(path),'--report',str(report)]), patch.object(transfer,'read_library',return_value=[]), patch.object(transfer,'copy_track') as write, contextlib.redirect_stdout(io.StringIO()):
                transfer.main()
            write.assert_not_called()
            self.assertEqual(json.loads(report.read_text())['tracks'][0]['status'],'review')

    def test_partial_spotify_blocks_apply_but_allows_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'export.json'; path.write_text(json.dumps(spotify(complete=False)))
            with patch.object(music_tool,'run_engine',return_value=0) as run, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError): music_tool.main(['--data-dir',tmp,'transfer',str(path),'--apply'])
                run.assert_not_called()
                self.assertEqual(music_tool.main(['--data-dir',tmp,'transfer',str(path)]),0)
                self.assertIn('spotify', run.call_args.args[2].parts)

    def test_new_cli_defaults_do_not_change_old_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp)/'spotify.json'; sp.write_text(json.dumps(spotify()))
            ya = Path(tmp)/'yandex.json'; ya.write_text(json.dumps(legacy()))
            with patch.object(agent,'call_tool',return_value=0) as call:
                agent.main(['transfer','--export',str(ya)])
                self.assertEqual(call.call_args.args[1][3],'ЯM')
                agent.main(['transfer-spotify','--export',str(sp)])
                self.assertEqual(call.call_args.args[1][3],'Spotify')
                self.assertNotIn('--apply',call.call_args.args[1])

    def test_missing_or_broken_spotapi_does_not_affect_core(self):
        import subprocess
        result = subprocess.run([sys.executable, '-B', '-c', '''
import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.startswith(('spotapi','playwright')): raise ImportError('unavailable')
sys.meta_path.insert(0,Block())
import agent, music_tool
from engine import export_yandex, transfer
from engine.sources import records
assert agent.dependency_version(__import__('pathlib').Path('/nonexistent')) is None
assert records({'playlists':[]}) == {}
assert music_tool.classify({'genre':'Unknown','artist':'A','title':'B','album':'C'},music_tool.load(music_tool.ROOT/'rules.json'))[2]
'''], capture_output=True, text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_spotify_runtime_paths_rejected_by_packager(self):
        from scripts.build_archive import validate_path
        for name in ('.venv-spotify/bin/python', 'cookies.json', 'sessions.json', 'spotify_library.json', 'data/spotify/export.json'):
            with self.assertRaises(ValueError): validate_path(name)
