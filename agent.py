#!/usr/bin/env python3
"""Non-interactive entry point for local AI agents; see AGENTS.md."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
PYTHON=ROOT/'.venv/bin/python3'

def emit(value):
    print(json.dumps(value,ensure_ascii=False,indent=2),flush=True)

def read(path):
    path=Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

def environment():
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',
             PIP_CACHE_DIR=str(ROOT/'.venv/pip-cache'),PIP_DISABLE_PIP_VERSION_CHECK='1')
    env.pop('PYTHONPYCACHEPREFIX',None)
    return env

def dependency_version(python):
    if not python.exists():return None
    code='import importlib.metadata; print(importlib.metadata.version("yandex-music"))'
    result=subprocess.run([str(python),'-B','-c',code],capture_output=True,text=True,timeout=20,env=environment())
    return result.stdout.strip() if result.returncode==0 else None

def diagnostics(data_dir,with_music=False):
    local_mac=sys.platform=='darwin'
    checks={
        'local_mac':local_mac,'python_supported':sys.version_info>=(3,9),
        'music_installed':Path('/System/Applications/Music.app').is_dir() if local_mac else False,
        'osascript_available':shutil.which('osascript') is not None,
        'virtualenv_ready':PYTHON.exists(),'yandex_dependency':dependency_version(PYTHON),
    }
    result={'checks':checks,'supported_ui_languages':['ru','en'],'data_dir':str(data_dir),
            'ready_for_library_read':False,'needs_user_action':[]}
    if not local_mac:
        result['needs_user_action'].append({'kind':'local_mac_required','message':'Нужен агент на Mac пользователя с доступом к Music; облачный Linux-контейнер не подходит.'})
    elif with_music:
        from music_tool import osa
        try:
            result['music_version']=osa('Application("Music").version()',js=True)
            result['ready_for_library_read']=True
        except RuntimeError as exc:
            result['needs_user_action'].append({'kind':'music_automation','message':str(exc),
                'settings_url':'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation'})
    else:
        result['next_step']='doctor --music проверит реальную связь и может вызвать штатный запрос разрешения macOS.'
    if checks['yandex_dependency']!='3.2.2':result['setup_needed']=True
    result['accessibility_note']='Для поиска через интерфейс macOS может отдельно запросить Automation к System Events и Accessibility у запускающего приложения.'
    return result

def setup(check_only=False):
    if sys.platform!='darwin':raise RuntimeError('Подготовку для переноса нужно запускать на локальном Mac.')
    if sys.version_info<(3,9):raise RuntimeError('Нужен Python 3.9 или новее.')
    if check_only:
        emit({'virtualenv_ready':PYTHON.exists(),'yandex_dependency':dependency_version(PYTHON)})
        return 0
    if not PYTHON.exists():
        subprocess.run([sys.executable,'-m','venv',str(ROOT/'.venv')],check=True,env=environment())
    if dependency_version(PYTHON)!='3.2.2':
        subprocess.run([str(PYTHON),'-m','pip','install','-r',str(ROOT/'requirements.txt')],check=True,env=environment())
    emit({'ready':True,'python':str(PYTHON),'yandex_dependency':dependency_version(PYTHON)})
    return 0

def resolve_export(data_dir,explicit=None):
    if explicit:
        path=Path(explicit).expanduser().resolve()
        if not path.is_file():raise ValueError('Экспорт не найден: '+str(path))
        return path
    candidates=sorted((data_dir/'exports').glob('*/yandex_library.json'))
    identity=read(data_dir/'transfer/import.json')
    if identity:
        candidates=[p for p in candidates if hashlib.sha256(p.read_bytes()).hexdigest()==identity['sha256']]
    if not candidates:
        raise ValueError('Нет подходящего экспорта. Выполни export или передай --export; для чужого экспорта используй отдельный --data-dir.')
    return candidates[-1].resolve()

def status(data_dir):
    result={'data_dir':str(data_dir),'authorization':read(data_dir/'auth-request.json'),
            'transfer':read(data_dir/'transfer/live-status.json'),
            'sorting':read(data_dir/'sorting-result.json')}
    if result['transfer']:
        t=result['transfer']
        t['heartbeat_age_seconds']=max(0,int(time.time()-t['heartbeat']))
        t['stale']=t['phase']=='Работает' and t['heartbeat_age_seconds']>15
    progress=read(data_dir/'transfer/catalog-progress.json')
    if progress:
        counts={}
        for entry in progress.get('tracks',{}).values():
            key=entry.get('status','unknown');counts[key]=counts.get(key,0)+1
        result['transfer_counts']=counts
        result['source_count']=progress.get('source_count',0)
        result['unprocessed_count']=max(0,result['source_count']-len(progress.get('tracks',{})))
    auth=result['authorization']
    if auth:
        auth['expired']=time.time()>auth['created_at_unix']+auth.get('expires_in',600)
    result['suggested_state']='authorization_expired' if auth and auth['expired'] else 'awaiting_yandex_login' if auth else 'running' if result['transfer'] and result['transfer']['phase']=='Работает' and not result['transfer']['stale'] else 'inspect_reports'
    return result

def call_tool(data_dir,arguments,needs_dependency=False):
    if sys.platform!='darwin':raise RuntimeError('Для работы с Music нужен локальный Mac, а не облачная среда.')
    if needs_dependency and dependency_version(PYTHON)!='3.2.2':
        raise RuntimeError('Сначала выполни: python3 agent.py setup. Это создаст локальную .venv без глобальных установок.')
    python=str(PYTHON) if PYTHON.exists() else sys.executable
    env=environment()
    env['MUSIC_TOOL_AUTH_FILE']=str(data_dir/'auth-request.json')
    command=[python,'-B',str(ROOT/'music_tool.py'),'--data-dir',str(data_dir),*map(str,arguments)]
    # Forward interruption so each managed layer can stop its own worker.
    child=subprocess.Popen(command,env=env,start_new_session=True)
    try:return child.wait()
    except KeyboardInterrupt:
        import signal
        if child.poll() is None:
            os.killpg(child.pid,signal.SIGINT)
            try:child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGTERM)
                child.wait(timeout=10)
        raise

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data')
    sub=parser.add_subparsers(dest='command',required=True)
    q=sub.add_parser('setup',help='Подготовить локальную .venv; можно повторять')
    q.add_argument('--check',action='store_true')
    q=sub.add_parser('doctor',help='Диагностика JSON без изменения Music')
    q.add_argument('--music',action='store_true',help='Проверить реальный доступ к Music')
    sub.add_parser('export',help='Экспортировать Яндекс; код входа — в status')
    q=sub.add_parser('transfer',help='Предпросмотр; добавление только с --apply')
    q.add_argument('--export',type=Path)
    q.add_argument('--playlist',default='ЯM')
    q.add_argument('--limit',type=int,default=0)
    q.add_argument('--apply',action='store_true')
    q=sub.add_parser('setup-spotify',help='Отдельное необязательное окружение Spotify')
    q.add_argument('--check',action='store_true')
    q.add_argument('--python')
    q=sub.add_parser('export-spotify',help='Экспериментальный Spotify: выборка из 5, полный экспорт с --full')
    q.add_argument('--full',action='store_true')
    q=sub.add_parser('transfer-spotify',help='Предпросмотр Spotify; запись только с --apply')
    q.add_argument('--export',type=Path,required=True)
    q.add_argument('--playlist',default='Spotify')
    q.add_argument('--limit',type=int,default=0)
    q.add_argument('--apply',action='store_true')
    q=sub.add_parser('status-spotify')
    q.add_argument('--export',type=Path,required=True)
    sub.add_parser('audit')
    q=sub.add_parser('plan')
    q.add_argument('--source',action='append',default=[])
    q.add_argument('--exclude-playlist',action='append',default=[])
    q.add_argument('--rules',type=Path,default=ROOT/'rules.json')
    q=sub.add_parser('apply')
    q.add_argument('--dry-run',action='store_true')
    sub.add_parser('status',help='Состояние, прогресс и запрос входа в JSON')
    args=parser.parse_args(argv)
    data=args.data_dir.expanduser().resolve()
    if args.command=='setup-spotify':
        from engine.spotify_commands import setup as spotify_setup
        return spotify_setup(args.check,args.python)
    if args.command=='export-spotify':
        from engine.spotify_commands import export as spotify_export
        return spotify_export(data,args.full)
    if args.command in ('transfer-spotify','status-spotify'):
        from engine.sources import source_name, transfer_directory
        path=args.export.expanduser().resolve()
        source=read(path)
        if not source or source_name(source)!='spotify':raise ValueError('Нужен spotify_library.json')
        if args.command=='status-spotify':emit(status(transfer_directory(data,source)));return 0
        forwarded=['transfer',path,'--playlist',args.playlist,'--limit',args.limit]
        if args.apply:forwarded.append('--apply')
        return call_tool(data,forwarded)
    if args.command=='setup':return setup(args.check)
    if args.command=='doctor':emit(diagnostics(data,args.music));return 0
    if args.command=='status':emit(status(data));return 0
    if args.command=='export':return call_tool(data,['export-yandex'],needs_dependency=True)
    if args.command=='transfer':
        path=resolve_export(data,args.export)
        forwarded=['transfer',path,'--playlist',args.playlist,'--limit',args.limit]
        if args.apply:forwarded.append('--apply')
        return call_tool(data,forwarded)
    if args.command=='plan':
        forwarded=['plan','--refresh','--rules',args.rules.expanduser().resolve()]
        for name in args.source:forwarded+=['--source',name]
        for name in args.exclude_playlist:forwarded+=['--exclude-playlist',name]
        return call_tool(data,forwarded)
    return call_tool(data,[args.command]+(['--dry-run'] if args.command=='apply' and args.dry_run else []))

if __name__=='__main__':
    try:sys.exit(main())
    except KeyboardInterrupt:
        emit({'stopped':True,'resume':'Повтори ту же команду с тем же --data-dir.'});sys.exit(130)
    except (ValueError,RuntimeError,OSError,KeyError,subprocess.SubprocessError) as exc:
        emit({'error':str(exc),'error_type':type(exc).__name__});sys.exit(1)
