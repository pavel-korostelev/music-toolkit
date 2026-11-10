"""Spotify-only command runner; optional imports never enter the Yandex path."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    from engine.spotify_export import Exporter
    from engine.spotify_session import Backend, browser_cookies
    os.umask(0o077)
    backend = None
    try:
        cookies = browser_cookies()
        try:
            backend = Backend(cookies)
        finally:
            cookies.clear()
        path, result = Exporter(backend, args.output, sample=0 if args.full else 5).run()
        print(json.dumps({'path': str(path), 'mode': result['mode'], 'complete': result['complete'],
                          'errors': len(result['errors']), 'playlists': len(result['playlists'])}, ensure_ascii=False), flush=True)
        # A successful sample is still NOT a complete library export.
        return 0 if result['complete'] else 2
    except KeyboardInterrupt:
        print('Spotify: остановлено; уже записанные страницы сохранены.', flush=True)
        return 130
    except Exception:
        print('Spotify: вход или чтение не выполнены. Секреты и ответы сервера не выводятся. Проверьте окружение, вход и доступность Spotify.', flush=True)
        return 1
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass  # Never expose upstream cleanup exceptions or session data.


if __name__ == '__main__':
    sys.exit(main())
