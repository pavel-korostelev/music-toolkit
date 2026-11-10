"""Isolated optional setup/commands. Uses only the standard library until execution."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / '.venv-spotify'
PYTHON = ENV / 'bin/python3'
LOCK = ROOT / 'requirements-spotify.txt'


def environment():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1',
               PIP_CACHE_DIR=str(ENV/'pip-cache'), PIP_DISABLE_PIP_VERSION_CHECK='1',
               PLAYWRIGHT_BROWSERS_PATH=str(ENV/'browsers'))
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONPYCACHEPREFIX', 'DEBUG', 'PWDEBUG', 'SSLKEYLOGFILE'):
        env.pop(name, None)
    return env


def ready():
    marker = ENV / 'ready.json'
    try:
        return PYTHON.is_file() and json.loads(marker.read_text()).get('requirements_sha256') == hashlib.sha256(LOCK.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return False


def find_python(explicit=None):
    candidates = [explicit] if explicit else [sys.executable] + [shutil.which('python3.'+str(v)) for v in (10, 11, 12, 13)]
    if not explicit and shutil.which('uv'):
        result = subprocess.run(['uv', 'python', 'find', '--no-managed-python', '--no-python-downloads', '>=3.10,<3.14'], capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            candidates.append(result.stdout.strip())
        # Existing managed installations only; never download Python implicitly.
        managed = Path.home()/'.local/share/uv/python'
        candidates.extend(str(p) for p in sorted(managed.glob('cpython-3.1[0-3]-*/bin/python3.*')) if p.is_file())
    for candidate in candidates:
        if not candidate:
            continue
        try:
            result = subprocess.run([str(candidate), '-c', 'import sys; sys.exit(not ((3,10)<=sys.version_info[:2]<(3,14)))'], capture_output=True, timeout=10)
            if result.returncode == 0:
                return str(candidate)
        except (OSError, subprocess.SubprocessError):
            continue
    raise RuntimeError('Для Spotify нужен отдельный Python 3.10–3.13. Агент должен подготовить его; Python Яндекса менять не нужно.')


def setup(check=False, python=None):
    if check:
        print(json.dumps({'spotify_ready': ready(), 'environment': str(ENV)}, ensure_ascii=False))
        return 0
    if sys.platform != 'darwin':
        raise RuntimeError('Spotify browser workflow requires the local Mac.')
    env = environment()
    if not PYTHON.exists():
        subprocess.run([find_python(python), '-m', 'venv', str(ENV)], check=True, env=env)
    if not ready():
        # Build dependencies are pinned too; no unbounded build-isolation install.
        subprocess.run([str(PYTHON), '-m', 'pip', 'install', 'pip==25.2', 'setuptools==80.9.0', 'wheel==0.45.1'], check=True, env=env)
        subprocess.run([str(PYTHON), '-m', 'pip', 'install', '--no-build-isolation', '-r', str(LOCK)], check=True, env=env)
        subprocess.run([str(PYTHON), '-m', 'pip', 'check'], check=True, env=env)
        subprocess.run([str(PYTHON), '-m', 'playwright', 'install', '--no-shell', 'chromium'], check=True, env=env)
        # Import only: no upstream tests, login, API request or account mutation.
        result = subprocess.run([str(PYTHON), '-c', 'import spotapi; import playwright.sync_api'], capture_output=True, env=env, timeout=30)
        if result.returncode:
            raise RuntimeError('Spotify dependency import failed; the Yandex environment is unaffected.')
        (ENV/'ready.json').write_text(json.dumps({'requirements_sha256': hashlib.sha256(LOCK.read_bytes()).hexdigest()}))
    print(json.dumps({'spotify_ready': True, 'environment': str(ENV)}, ensure_ascii=False))
    return 0


def export(data_dir, full=False):
    if not ready():
        raise RuntimeError('Сначала агент должен выполнить setup-spotify.')
    command = [str(PYTHON), '-B', str(ROOT/'engine/spotify_cli.py'), '--output', str(data_dir/'spotify')]
    if full:
        command.append('--full')
    # Same process group: Ctrl+C reaches the browser/export worker too.
    child = subprocess.Popen(command, env=environment())
    try:
        return child.wait()
    except KeyboardInterrupt:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=10)
        raise
