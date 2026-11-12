"""Real OS locking with fake workers: never access Music or account data."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import music_tool
from engine import music_lock, run_transfer, sources, transfer


@unittest.skipUnless(os.name == 'posix', 'Music operations require POSIX locking')
class MusicLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'shared' / 'music.lock'
        patched = patch.object(music_lock, 'lock_path', return_value=self.path)
        patched.start()
        self.addCleanup(patched.stop)

    def test_other_checkout_process_cannot_lock_until_owner_exits(self):
        # Load a second physical copy, with another cwd and state directory.
        other = self.root / 'another-checkout'
        other.mkdir()
        shutil.copyfile(music_lock.__file__, other / 'music_lock.py')
        script = '''
import sys
from pathlib import Path
import music_lock
music_lock.lock_path = lambda: Path(sys.argv[1])
try:
    with music_lock.music_operation_lock():
        pass
except RuntimeError:
    sys.exit(23)
'''
        def contender():
            return subprocess.run([sys.executable, '-B', '-c', script, str(self.path)],
                                  cwd=other, env=dict(os.environ, MUSIC_TOOL_STATE_DIR=str(other/'data')),
                                  capture_output=True, timeout=10)
        with music_lock.music_operation_lock():
            self.assertEqual(contender().returncode, 23)
        self.assertEqual(contender().returncode, 0)

    def test_sources_accounts_and_sorting_stop_before_worker_or_music_access(self):
        states = [self.root/'data']
        states += [sources.transfer_directory(self.root/'data',
                   {'source': 'spotify', 'schema_version': 2, 'account_id': account})
                   for account in ('first', 'second')]
        with music_lock.music_operation_lock():
            for state in states:
                with self.subTest(state=state.name), patch.object(run_transfer, 'HERE', state/'transfer'), patch.object(run_transfer.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(RuntimeError, 'MusicToolkit'):
                        run_transfer.main()
                    spawn.assert_not_called()
            plan = self.root/'plan.json'
            plan.write_text('{}')
            with patch.object(music_tool, 'audit') as audit, patch.object(music_tool, 'copy_to_playlist') as write:
                with self.assertRaises(RuntimeError):
                    music_tool.main(['--data-dir', str(self.root), 'apply', str(plan)])
                audit.assert_not_called()
                write.assert_not_called()
            with patch.object(sys, 'argv', ['transfer.py', 'unused.json', '--apply']), patch.object(transfer, 'read_library') as read:
                with self.assertRaises(RuntimeError):
                    transfer.main()
                read.assert_not_called()

    def test_worker_keeps_lock_after_parent_releases_its_descriptor(self):
        child = None
        try:
            with music_lock.music_operation_lock() as fd:
                child = subprocess.Popen([sys.executable, '-B', '-c', 'import sys; sys.stdin.buffer.read()'],
                                         stdin=subprocess.PIPE, pass_fds=(fd,))
            with self.assertRaises(RuntimeError):
                with music_lock.music_operation_lock():
                    self.fail('worker lost the inherited lock')
        finally:
            if child is not None:
                child.communicate(timeout=10)
        with music_lock.music_operation_lock():
            pass

    def test_runner_passes_lock_to_worker_and_releases_on_completion(self):
        class Child:
            returncode = 0
            def poll(self): return 0
        def spawn(*args, **kwargs):
            self.assertEqual(len(kwargs['pass_fds']), 1)
            os.fstat(kwargs['pass_fds'][0])
            with self.assertRaises(RuntimeError):
                with music_lock.music_operation_lock():
                    self.fail('runner released the lock during transfer')
            return Child()
        with patch.object(run_transfer, 'HERE', self.root/'transfer'), patch.object(sys, 'argv', ['run_transfer.py', 'unused.json']), patch.object(run_transfer.subprocess, 'Popen', side_effect=spawn) as worker:
            run_transfer.main()
            worker.assert_called_once()
        with music_lock.music_operation_lock():
            pass

    def test_runner_releases_lock_after_failed_start_or_interrupt(self):
        for error in (OSError('fake worker failure'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), patch.object(run_transfer, 'HERE', self.root/'transfer'), patch.object(sys, 'argv', ['run_transfer.py', 'unused.json']), patch.object(run_transfer.subprocess, 'Popen', side_effect=error):
                with self.assertRaises((OSError, SystemExit)):
                    run_transfer.main()
            with music_lock.music_operation_lock():
                pass

    def test_preview_does_not_take_mutation_lock(self):
        export = self.root/'export.json'
        export.write_text('{"playlists": []}')
        with music_lock.music_operation_lock(), patch.object(sys, 'argv', ['transfer.py', str(export), '--report', str(self.root/'report.json')]), patch.object(transfer, 'read_library', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            transfer.main()

    def test_sorting_holds_lock_through_verification_and_releases_on_error(self):
        plan = self.root/'plan.json'
        plan.write_text('{}')
        def apply(*args):
            with self.assertRaises(RuntimeError):
                with music_lock.music_operation_lock():
                    self.fail('sorting has no Music lock')
            return {'playlists': []}
        def verify(*args):
            with self.assertRaises(RuntimeError):
                with music_lock.music_operation_lock():
                    self.fail('sorting released the lock before verification')
            raise RuntimeError('fake verification failure')
        with patch.object(music_tool, 'ROOT', self.root), patch.object(music_tool, 'audit', return_value={}), patch.object(music_tool, 'apply_plan', side_effect=apply), patch.object(music_tool, 'verify_applied', side_effect=verify):
            with self.assertRaisesRegex(RuntimeError, 'fake verification failure'):
                music_tool.main(['--data-dir', str(self.root), 'apply', str(plan)])
        with music_lock.music_operation_lock():
            pass
