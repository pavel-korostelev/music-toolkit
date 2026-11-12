"""Local compatibility smoke tests; no upstream test suite or network is run."""
import importlib.util
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('spotapi'), 'optional Spotify environment absent')
class InstalledSpotAPITests(unittest.TestCase):
    def test_memory_cookie_import_never_logs_in_or_connects(self):
        import requests
        from curl_cffi.requests import Session
        from engine.spotify_session import Backend, ExportError
        with patch.object(Session, 'request', side_effect=AssertionError('unexpected network')), patch.object(requests, 'get', side_effect=AssertionError('unexpected network')):
            backend = Backend({'sp_dc': 'test-only-placeholder'})
            try:
                self.assertEqual(backend.login.password, '')
                self.assertTrue(backend.login.logged_in)
                with self.assertRaises(ExportError):
                    backend.client.post('https://api-partner.spotify.com/pathfinder/v1/query', params={'operationName': 'addToLibrary'})
            finally:
                backend.close()

    def test_backend_uses_expected_read_variables_without_network(self):
        import requests
        from curl_cffi.requests import Session
        from engine.spotify_session import Backend
        with patch.object(Session, 'request', side_effect=AssertionError('unexpected network')), patch.object(requests, 'get', side_effect=AssertionError('unexpected network')):
            backend = Backend({'sp_dc': 'test-only-placeholder'})
            try:
                response = {'data': {'me': {'libraryV3': {'items': [], 'totalCount': 0}}}}
                with patch.object(backend, 'query', return_value=response) as query:
                    backend.library('spotify:user:test:folder:one', 50, 50)
                operation, variables = query.call_args.args
                self.assertEqual(operation, 'libraryV3')
                self.assertEqual(variables['offset'], 50)
                self.assertEqual(variables['folderUri'], 'spotify:user:test:folder:one')
                self.assertFalse(variables['flatten'])
            finally:
                backend.close()
