"""Optional browser login and read-only SpotAPI backend. Imported only by Spotify CLI."""
import contextlib
import json
import logging
import os
import signal
import time
from urllib.parse import urlsplit

from engine.spotify_export import ExportError

QUERY_URL = 'https://api-partner.spotify.com/pathfinder/v1/query'
READ_QUERIES = {'libraryV3', 'fetchLibraryTracks', 'fetchPlaylist'}


def check_request(method, url, kwargs):
    """Allowlist runs at the actual transport, including SpotAPI internal requests."""
    u = urlsplit(url)
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
        raise ExportError('blocked_request')
    method = method.upper()
    if method == 'GET':
        permitted = ((u.hostname == 'open.spotify.com' and u.path in ('', '/', '/api/token')) or
                     (u.hostname == 'www.spotify.com' and u.path == '/api/account-settings/v1/profile') or
                     (u.hostname == 'open.spotifycdn.com' and u.path.startswith('/cdn/') and u.path.endswith('.js')))
    elif method == 'POST' and url == QUERY_URL:
        payload = kwargs.get('params') or kwargs.get('json') or {}
        permitted = payload.get('operationName') in READ_QUERIES and 'query' not in payload
    else:
        permitted = method == 'POST' and url == 'https://clienttoken.spotify.com/v1/clienttoken'
    if not permitted:
        raise ExportError('blocked_request')


class _DeadlineExpired(BaseException):
    """Do not let upstream broad except Exception swallow the time limit."""


@contextlib.contextmanager
def deadline(seconds=90):
    def expired(*_):
        raise _DeadlineExpired()
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    except _DeadlineExpired:
        raise ExportError('request_timeout') from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextlib.contextmanager
def quiet():
    # Upstream exceptions can contain response bodies. Never print their text.
    old = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            yield
        finally:
            logging.disable(old)


def browser_cookies():
    from playwright.sync_api import sync_playwright
    print('Spotify: войдите в открывшемся окне браузера. Ожидание до 10 минут; пароль вводится только на сайте Spotify.', flush=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=False)
        try:
            page = context.new_page()
            page.goto('https://accounts.spotify.com/login?continue=https%3A%2F%2Fopen.spotify.com%2F', timeout=60000)
            until = time.monotonic() + 600
            while time.monotonic() < until:
                cookies = {c['name']: c['value'] for c in context.cookies('https://open.spotify.com/')
                           if c['name'] in ('sp_dc', 'sp_key', 'sp_t')}
                if cookies.get('sp_dc'):
                    return cookies
                page.wait_for_timeout(1000)
            raise ExportError('login_timeout')
        finally:
            context.close()
            browser.close()


class Backend:
    def __init__(self, cookies):
        with quiet():
            from spotapi import Login, Config, NoopLogger, PrivatePlaylist
            from spotapi.http.request import TLSClient
            class ReadOnlyClient(TLSClient):
                def request(self, method, url, **kwargs):
                    check_request(method, url, kwargs)
                    kwargs['timeout'] = 20
                    kwargs['allow_redirects'] = False
                    return super().request(method, url, **kwargs)
            self.client = ReadOnlyClient('chrome120', '', auto_retries=0)
            # No password, saver, disk session, login() or CAPTCHA solver.
            self.login = Login.from_cookies({'identifier': 'browser-session', 'cookies': cookies},
                                           Config(logger=NoopLogger(), client=self.client))
            self.private = PrivatePlaylist(self.login)
            self.base = self.private.base
        self._account = None

    def close(self):
        self.client.cookies.clear()
        self.client.close()
        self.login = self.private = self.base = None

    def call(self, callback):
        for attempt in range(3):
            try:
                with quiet(), deadline():
                    return callback()
            except ExportError:
                raise
            except Exception:
                if attempt == 2:
                    raise ExportError('request_or_schema_error') from None
                time.sleep(2 ** attempt)

    def account_id(self):
        if self._account is None:
            print('Spotify: проверяю учётную запись (только чтение).', flush=True)
            self._account = self.call(lambda: self.private.user.username)
        return self._account

    def query(self, operation, variables):
        print('Spotify: чтение %s, смещение %d' % (operation, variables.get('offset', 0)), flush=True)
        def read():
            params = {'operationName': operation, 'variables': json.dumps(variables),
                      'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': self.base.part_hash(operation)}})}
            response = self.client.post(QUERY_URL, params=params, authenticate=True)
            if response.status_code in (401, 403):
                raise ExportError('session_expired_or_access_denied')
            if response.status_code == 429:
                raise ExportError('rate_limited_retry_later')
            if response.fail:
                raise RuntimeError('request_failed')
            data = response.response
            if not isinstance(data, dict) or data.get('errors'):
                raise ExportError('graphql_error')
            return data
        return self.call(read)

    def likes(self, offset, limit):
        return self.query('fetchLibraryTracks', {'offset': offset, 'limit': limit})['data']['me']['library']['tracks']

    def playlist(self, uri, offset, limit):
        return self.query('fetchPlaylist', {'uri': uri, 'offset': offset, 'limit': limit,
                                          'enableWatchFeedEntrypoint': False})['data']['playlistV2']['content']

    def library(self, folder, offset, limit):
        # Upstream get_library() hardcodes offset=0 and folderUri=None.
        data = self.query('libraryV3', {'filters': [], 'order': None, 'textFilter': '',
                         'features': ['LIKED_SONGS', 'YOUR_EPISODES', 'PRERELEASES'],
                         'limit': limit, 'offset': offset, 'flatten': False,
                         'expandedFolders': [], 'folderUri': folder,
                         'includeFoldersWhenFlattening': True})
        return data['data']['me']['libraryV3']
