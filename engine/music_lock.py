"""Serialize Music operations across sources, data directories and checkouts."""
from contextlib import contextmanager
from pathlib import Path


def lock_path():
    # A user-wide path, independent of the checkout, source and account.
    return Path.home() / 'Library' / 'Caches' / 'MusicToolkit' / 'music.lock'


@contextmanager
def music_operation_lock():
    import fcntl
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never truncate or unlink: all processes must lock the same inode.
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Другая операция MusicToolkit уже работает с Music. Дождись её завершения.') from None
        yield lock.fileno()
