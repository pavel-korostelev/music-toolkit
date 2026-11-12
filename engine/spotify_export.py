"""Account-independent export logic. No SpotAPI/browser import at module load."""
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path


FATAL_CODES = {'session_expired_or_access_denied', 'rate_limited_retry_later', 'blocked_request'}


class ExportError(Exception):
    """Messages are fixed codes, never upstream exception/response text."""


def account_key(account_id):
    if not isinstance(account_id, str) or not account_id:
        raise ExportError('missing_account')
    return hashlib.sha256(account_id.encode()).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    tmp.replace(path)


def unwrap(item):
    if not isinstance(item, dict):
        return {}
    # Known Spotify wrappers. Unknown shapes stay unknown and are reported.
    for name in ('itemV2', 'track', 'item'):
        if name in item:
            item = item[name]
            break
    if isinstance(item, dict) and 'data' in item:
        wrapper_uri = item.get('_uri')
        item = item['data']
        if isinstance(item, dict) and not item.get('uri') and isinstance(wrapper_uri, str):
            item = dict(item, uri=wrapper_uri)
    return item if isinstance(item, dict) else {}


def normalize_track(item, position):
    data = unwrap(item)
    uri = data.get('uri', '')
    uri = uri if isinstance(uri, str) else ''
    kind = data.get('__typename', '')
    if uri.startswith('spotify:local:') or kind == 'LocalTrack':
        reason = 'local_file'
    elif uri.startswith('spotify:episode:') or kind in ('Episode', 'Chapter'):
        reason = 'podcast_or_audiobook'
    elif not uri.startswith('spotify:track:'):
        reason = 'unavailable_item' if kind in ('NotFound', 'RestrictedContent') else 'unknown_item_shape'
    else:
        reason = None
    if reason:
        return None, {'position': position, 'source_id': uri, 'reason': reason,
                      'title': data.get('name') if isinstance(data.get('name'), str) else ''}
    artists = data.get('artists', {})
    artists = artists.get('items', []) if isinstance(artists, dict) else []
    names = []
    for artist in artists:
        if isinstance(artist, dict):
            profile = artist.get('profile')
            name = (profile.get('name') if isinstance(profile, dict) else None) or artist.get('name')
            if isinstance(name, str) and name:
                names.append(name)
    album = data.get('albumOfTrack') or data.get('album') or {}
    album_name = album.get('name') if isinstance(album, dict) else None
    album_name = album_name if isinstance(album_name, str) else None
    duration_data = data.get('duration')
    if duration_data is None:
        duration_data = data.get('trackDuration')
    duration = duration_data.get('totalMilliseconds') if isinstance(duration_data, dict) else None
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
        duration = None
    title = data.get('name') if isinstance(data.get('name'), str) else ''
    playable = data.get('playability', {}).get('playable') if isinstance(data.get('playability'), dict) else None
    review = []
    if not title or not names or duration is None:
        review.append('missing_metadata')
    if playable is False:
        review.append('unavailable_in_spotify')
    ident = uri.split(':')[-1]
    return {'source': 'spotify', 'source_id': ident, 'uri': uri,
            'url': 'https://open.spotify.com/track/' + ident,
            'title': title, 'artists': names, 'albums': [album_name] if album_name else [],
            'duration_ms': duration, 'version': None, 'position': position,
            'available': playable, 'review_reasons': review}, None


def paginate(fetch, sink, sample=0, max_pages=10000):
    """Count positions (including duplicates/skips), never infer total from page size."""
    offset = 0
    expected = None
    seen_pages = set()
    for _ in range(max_pages):
        limit = min(50, sample - offset) if sample else 50
        page = fetch(offset, limit)
        if not isinstance(page, dict) or not isinstance(page.get('items'), list):
            raise ExportError('invalid_page')
        total = page.get('totalCount')
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ExportError('missing_total_count')
        if expected is not None and expected != total:
            raise ExportError('source_changed_during_export')
        expected = total
        items = page['items']
        if len(items) > limit or offset + len(items) > total:
            raise ExportError('count_mismatch')
        signature = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
        if items and signature in seen_pages:
            raise ExportError('repeated_page')
        seen_pages.add(signature)
        sink(items, offset, total)
        offset += len(items)
        if offset == total:
            return {'expected': total, 'received': offset, 'complete': True}
        if not items:
            raise ExportError('early_empty_page')
        if sample and offset >= sample:
            return {'expected': total, 'received': offset, 'complete': False}
    raise ExportError('page_limit')


class Exporter:
    def __init__(self, backend, output, sample=5):
        self.backend = backend
        self.output = Path(output)
        self.sample = sample
        self.path = None
        self.data = {'schema_version': 2, 'source': 'spotify',
                     'exported_at': datetime.now(timezone.utc).isoformat(),
                     'mode': 'sample' if sample else 'full', 'complete': False,
                     'scope': 'liked_tracks_and_library_playlists_including_folders',
                     'playlists': [], 'folders': [], 'skipped_library_items': [],
                     'library_pages': [], 'errors': [], 'truncations': []}

    def checkpoint(self):
        save_json(self.path, self.data)

    def error(self, stage, code):
        self.data['errors'].append({'stage': stage, 'code': code,
                                    'collection': self.data['playlists'][-1]['source_id'] if stage == 'collection' and self.data['playlists'] else None})
        self.checkpoint()

    def truncated(self, stage, code, source_id=None):
        entry = {'stage': stage, 'code': code, 'source_id': source_id}
        if entry not in self.data['truncations']:
            self.data['truncations'].append(entry)
            self.checkpoint()

    def collection(self, uri, title, fetch, folders):
        entry = {'source_id': uri, 'title': title, 'folder_path': folders,
                 'tracks': [], 'skipped': [], 'counts': {'received': 0}, 'complete': False}
        self.data['playlists'].append(entry)
        self.checkpoint()
        def consume(items, offset, total):
            for i, item in enumerate(items, offset + 1):
                track, skipped = normalize_track(item, i)
                if track:
                    entry['tracks'].append(track)
                else:
                    entry['skipped'].append(skipped)
                    if skipped['reason'] == 'unknown_item_shape':
                        self.error('collection', 'unknown_item_shape')
            entry['counts'] = {'expected': total, 'received': offset + len(items),
                               'tracks': len(entry['tracks']), 'skipped': len(entry['skipped'])}
            self.checkpoint()
            print('Spotify: список %d, позиций %d/%d' % (len(self.data['playlists']), offset + len(items), total), flush=True)
        try:
            counts = paginate(fetch, consume, self.sample)
            entry['complete'] = counts['complete']
            if self.sample and not counts['complete']:
                self.truncated('collection', 'sample_track_limit', uri)
        except ExportError as exc:
            self.error('collection', str(exc))
            if str(exc) in FATAL_CODES:
                raise
        except Exception:
            self.error('collection', 'request_or_schema_error')
        self.checkpoint()

    def run(self):
        account = self.backend.account_id()
        self.data['account_id'] = account
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        self.path = self.output / account_key(account) / 'exports' / stamp / 'spotify_library.json'
        self.checkpoint()
        visited = set()
        seen_playlists = {}
        try:
            self.collection('spotify:collection:tracks', 'Liked Songs', self.backend.likes, [])
            queue = [(None, [])]
            while queue:
                folder, parents = queue.pop(0)
                if folder in visited:
                    self.error('folders', 'folder_cycle_or_duplicate')
                    continue
                if len(visited) >= (5 if self.sample else 10000):
                    if self.sample:
                        self.truncated('folders', 'sample_folder_limit')
                    else:
                        self.error('folders', 'folder_limit')
                    break
                visited.add(folder)
                listing = {'folder_uri': folder, 'received': 0, 'complete': False}
                self.data['library_pages'].append(listing)
                def consume(items, offset, total):
                    listing.update(received=offset + len(items), expected=total)
                    for raw in items:
                        item = unwrap(raw)
                        uri = item.get('uri', '')
                        name = item.get('name', '')
                        kind = item.get('__typename', '')
                        if not isinstance(uri, str) or not isinstance(name, str):
                            raise ExportError('invalid_library_item')
                        if kind == 'Folder' or ':folder:' in uri:
                            if not uri:
                                raise ExportError('missing_folder_uri')
                            self.data['folders'].append({'source_id': uri, 'title': name, 'parents': parents})
                            queue.append((uri, parents + [uri]))
                        elif uri.startswith('spotify:playlist:'):
                            if uri in seen_playlists:
                                seen_playlists[uri]['folder_paths'].append(parents)
                                continue
                            if self.sample and len(seen_playlists) >= self.sample:
                                self.truncated('library', 'sample_playlist_limit')
                                continue
                            self.collection(uri, name, lambda o, n, u=uri: self.backend.playlist(u, o, n), parents)
                            seen_playlists[uri] = self.data['playlists'][-1]
                            seen_playlists[uri]['folder_paths'] = [parents]
                        elif uri.startswith(('spotify:album:', 'spotify:artist:', 'spotify:show:', 'spotify:collection:')):
                            self.data['skipped_library_items'].append({'source_id': uri, 'reason': 'outside_playlist_scope'})
                        else:
                            self.data['skipped_library_items'].append({'source_id': uri, 'reason': 'unknown_library_item'})
                            self.error('library', 'unknown_library_item')
                    self.checkpoint()
                try:
                    counts = paginate(lambda o, n: self.backend.library(folder, o, n), consume, self.sample)
                    listing.update(counts)
                    if self.sample and not counts['complete']:
                        self.truncated('library', 'sample_library_page_limit', folder)
                except ExportError as exc:
                    self.error('library', str(exc))
                    if str(exc) in FATAL_CODES:
                        raise
                except Exception:
                    self.error('library', 'request_or_schema_error')
                self.checkpoint()
            self.data['complete'] = bool(not self.sample and not self.data['errors'] and
                                         all(p['complete'] for p in self.data['playlists']) and
                                         all(p['complete'] for p in self.data['library_pages']))
        except ExportError as exc:
            self.error('export', str(exc))
        except KeyboardInterrupt:
            self.error('export', 'interrupted')
            raise
        finally:
            self.checkpoint()
        return self.path, self.data
