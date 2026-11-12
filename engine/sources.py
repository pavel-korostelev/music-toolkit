"""Common metadata adapter; legacy Yandex IDs and import identity stay unchanged."""
import hashlib
import json
import math
from pathlib import Path


def source_name(export):
    name = export.get('source', 'yandex')
    if name not in ('yandex', 'spotify'):
        raise ValueError('Unsupported music source')
    if name == 'spotify' and (export.get('schema_version') != 2 or not export.get('account_id')):
        raise ValueError('Spotify export requires schema_version=2 and account_id')
    return name


def account_key(export):
    account = export.get('account_id')
    if not isinstance(account, str) or not account:
        raise ValueError('Missing source account identity')
    return hashlib.sha256(account.encode()).hexdigest()


def records(export):
    name = source_name(export)
    if not isinstance(export.get('playlists'), list):
        raise ValueError('Expected a library export with playlists')
    unique = {}
    for playlist in export['playlists']:
        for original in playlist['tracks']:
            id_field = 'yandex_id' if name == 'yandex' else 'source_id'
            for field in (id_field, 'title', 'artists', 'albums', 'duration_ms'):
                if field not in original:
                    raise ValueError('Unsupported export: missing ' + field)
            allowed = ('source_id', 'uri', 'url', 'title', 'artists', 'albums', 'duration_ms',
                       'version', 'position', 'available', 'review_reasons')
            track = ({k: original[k] for k in allowed if k in original}
                     if name == 'spotify' else dict(original))
            ident = original[id_field]
            track['source'] = name
            track['source_id'] = str(ident)
            if name == 'spotify':
                if not isinstance(ident, str) or not ident:
                    raise ValueError('Missing Spotify track ID')
                key = 'spotify:' + account_key(export) + ':' + ident
                duration = track['duration_ms']
                reasons = list(track.get('review_reasons', []))
                if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
                    track['duration_ms'] = None
                    reasons.append('missing_metadata')
                if not track['title'] or not track['artists']:
                    reasons.append('missing_metadata')
                track['review_reasons'] = sorted(set(reasons))
            else:
                # Keep precisely the same state keys as old catalog-progress.json.
                key = ident
            unique.setdefault(key, track)
    return unique


def identity(path, playlist):
    raw = Path(path).read_bytes()
    export = json.loads(raw)
    unique = records(export)
    result = {'sha256': hashlib.sha256(raw).hexdigest(), 'playlist': playlist,
              'source_count': len({str(k) for k in unique})}
    if source_name(export) == 'spotify':
        result.update(source='spotify', account_key=account_key(export), schema_version=2)
    return result


def transfer_directory(data_dir, export):
    if source_name(export) == 'spotify':
        return Path(data_dir) / 'spotify' / account_key(export)
    return Path(data_dir)


def require_complete_spotify(export):
    """An export success flag alone is insufficient for account mutations."""
    if (export.get('complete') is not True or export.get('mode') != 'full' or
            export.get('errors') or export.get('truncations') or
            not export.get('library_pages') or not export.get('playlists')):
        raise ValueError('Spotify: нужен полный проверенный экспорт; частичный доступен только для предпросмотра.')
    for page in export['library_pages']:
        if page.get('complete') is not True or not isinstance(page.get('expected'), int) or page['expected'] != page.get('received'):
            raise ValueError('Spotify: не подтверждён полный список плейлистов.')
    for playlist in export['playlists']:
        counts = playlist.get('counts', {})
        positions = len(playlist.get('tracks', [])) + len(playlist.get('skipped', []))
        if (playlist.get('complete') is not True or counts.get('expected') != positions or
                counts.get('received') != positions or counts.get('tracks') != len(playlist.get('tracks', [])) or
                counts.get('skipped') != len(playlist.get('skipped', []))):
            raise ValueError('Spotify: счётчики экспорта не совпадают с сохранёнными позициями.')
