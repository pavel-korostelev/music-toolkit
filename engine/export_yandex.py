#!/usr/bin/env python3
"""Read-only export of Yandex Music likes and owned playlists."""
import argparse
import csv
import json
import logging
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
import webbrowser


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def track_data(track, reference=None):
    if track is None:
        return {'yandex_id': str(reference.id), 'title': '', 'artists': [],
                'albums': [], 'duration_ms': None, 'version': None,
                'metadata_missing': True}
    return {'yandex_id': str(track.id), 'title': track.title or '',
            'artists': [a.name for a in (track.artists or [])],
            'albums': [a.title for a in (track.albums or [])],
            'duration_ms': track.duration_ms, 'version': track.version,
            'available': track.available}


def request(function, *args, **kwargs):
    from yandex_music.exceptions import NetworkError
    for attempt in range(4):
        try:
            result = function(*args, **kwargs)
            time.sleep(0.2)
            return result
        except NetworkError:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def resolve(client, references):
    output = []
    for start in range(0, len(references), 100):
        batch = references[start:start + 100]
        missing = [r for r in batch if getattr(r, 'track', None) is None]
        resolved = {}
        if missing:
            ids = [f'{r.id}:{r.album_id}' if getattr(r, 'album_id', None) else str(r.id)
                   for r in missing]
            resolved = {str(t.id): t for t in request(client.tracks, ids)}
        for r in batch:
            track = getattr(r, 'track', None) or resolved.get(str(r.id))
            output.append(track_data(track, r))
        print(f'  Метаданные: {len(output)}/{len(references)}', flush=True)
    return output


def show_code(code):
    auth_file=os.environ.get('MUSIC_TOOL_AUTH_FILE')
    if auth_file:
        path=Path(auth_file)
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        write_json(path,{'verification_url':str(code.verification_url),'user_code':str(code.user_code),
                         'created_at_unix':time.time(),'expires_in':getattr(code,'expires_in',None) or 600})
    print(f'\nОткрой в браузере: {code.verification_url}')
    print(f'Введи код: {code.user_code}\n', flush=True)
    if str(code.verification_url).startswith('https://'):
        webbrowser.open(code.verification_url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent.parent / 'data/exports')
    args = parser.parse_args()
    try:
        from yandex_music import Client
    except ImportError:
        print('Сначала открой Install.command, затем снова Start.command.')
        return 1
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    print('Экспорт названий песен и плейлистов. Аудиофайлы не скачиваются.')
    print('Вход подтверди на сайте Яндекса. Токен не сохраняется на диск.')
    client = Client()
    try:
        client.device_auth(on_code=show_code, timeout=600)
        client.init()
    except Exception as error:
        print(f'Вход не выполнен ({type(error).__name__}).')
        print('Device Flow может быть недоступен для аккаунта. Сохрани название ошибки, без токенов.')
        return 1
    finally:
        auth_file=os.environ.get('MUSIC_TOOL_AUTH_FILE')
        if auth_file:
            Path(auth_file).unlink(missing_ok=True)

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    folder = args.output.expanduser().resolve() / stamp
    folder.mkdir(parents=True, mode=0o700)
    data = {'schema_version': 1, 'exported_at': datetime.now(timezone.utc).isoformat(),
            'scope': 'liked_tracks_and_owned_playlists', 'playlists': [], 'errors': []}
    destination = folder / 'yandex_library.json'
    write_json(destination, data)

    try:
        print('\nЧитаю «Мне нравится»...', flush=True)
        likes = request(client.users_likes_tracks)
        if likes is None:
            raise RuntimeError('EmptyLikesResponse')
        data['playlists'].append({'title': 'Мне нравится', 'source': 'likes',
                                  'tracks': resolve(client, likes.tracks or [])})
        write_json(destination, data)
    except Exception as error:
        data['errors'].append({'stage': 'likes', 'error_type': type(error).__name__})
        write_json(destination, data)
        print('Не удалось полностью прочитать «Мне нравится». Остальные плейлисты будут обработаны.')

    try:
        playlists = request(client.users_playlists_list)
    except Exception as error:
        playlists = []
        data['errors'].append({'stage': 'playlist_list', 'error_type': type(error).__name__})
    for index, playlist in enumerate(playlists, 1):
        print(f'\nПлейлист {index}/{len(playlists)}: {playlist.title}', flush=True)
        try:
            refs = request(playlist.fetch_tracks)
            tracks = resolve(client, refs or [])
            expected = playlist.track_count
            if expected is not None and expected != len(tracks):
                data['errors'].append({'stage': 'track_count', 'playlist': playlist.title,
                                       'expected': expected, 'received': len(tracks)})
            data['playlists'].append({'title': playlist.title, 'source': 'owned_playlist',
                                      'kind': playlist.kind, 'tracks': tracks})
        except Exception as error:
            data['errors'].append({'stage': 'playlist', 'playlist': playlist.title,
                                   'error_type': type(error).__name__})
        write_json(destination, data)

    write_json(destination, data)
    with (folder / 'tracks.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(['playlist', 'position', 'title', 'artists', 'albums', 'duration_ms', 'version', 'yandex_id'])
        for playlist in data['playlists']:
            for position, track in enumerate(playlist['tracks'], 1):
                row = [playlist['title'], position, track['title'], '; '.join(track['artists']),
                       '; '.join(track['albums']), track['duration_ms'], track['version'], track['yandex_id']]
                writer.writerow(["'" + x if isinstance(x, str) and x.startswith(('=', '+', '-', '@')) else x for x in row])
    sample = next((p['tracks'][:10] for p in data['playlists'] if p['tracks']), [])
    write_json(folder / 'sample_10.json', {'tracks': sample})
    total = sum(len(p['tracks']) for p in data['playlists'])
    print(f'\nСохранено: {len(data["playlists"])} списков, {total} позиций (с повторами между списками).')
    print(f'Папка: {folder}')
    if data['errors']:
        print(f'Внимание: экспорт частичный. Ошибок: {len(data["errors"])}. Подробности в JSON, поле errors.')
    else:
        print('Экспорт завершён. Можно перейти к переносу файла yandex_library.json.')
    return 1 if data['errors'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nОстановлено. Уже записанные результаты сохранены; повторный запуск создаст новую папку.')
        sys.exit(130)
    except Exception as error:
        print(f'Ошибка: {type(error).__name__}. Секреты в вывод не включены.')
        sys.exit(1)
