#!/usr/bin/env python3
"""Run the transfer with a heartbeat, bounded recovery, and readable status."""
import collections
import argparse
import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CODE_DIR=Path(__file__).resolve().parent
HERE=Path(os.environ.get('MUSIC_TOOL_STATE_DIR', str(CODE_DIR.parent/'data/transfer')))


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError,ValueError):
        return {}


def make_status(phase,pid,attempt,now=None):
    now=time.time() if now is None else now
    progress=read_json(HERE/'catalog-progress.json')
    items=list(progress.get('tracks',{}).values())
    counts=dict(collections.Counter(x.get('status','unknown') for x in items))
    unique={x['apple']['id'] for x in items if x.get('status')=='verified' and x.get('apple')}
    unfinished=[x for x in items if x.get('status') in ('started','error')]
    current=unfinished[-1] if unfinished else (items[-1] if items else {})
    track=current.get('source',{})
    current_step=read_json(HERE/'current-step.json')
    return dict(phase=phase,pid=pid,attempt=attempt,heartbeat=now,
                time=datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec='seconds'),
                apple_tracks=len(unique),source_count=progress.get('source_count',0),playlist=progress.get('playlist',''),
                counts=counts,track=' & '.join(track.get('artists',[]))+' — '+track.get('title',''),
                step=current_step.get('step','Запуск'),
                step_seconds=max(0,int(now-current_step.get('since',now))),
                error=current.get('error','') if current.get('status')=='error' else '')


def publish(phase,pid,attempt):
    status=make_status(phase,pid,attempt)
    path=HERE/'live-status.json'
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(status,ensure_ascii=False,indent=2))
    tmp.replace(path)
    (HERE/'live-status.txt').write_text(
        f"Состояние: {phase}\nПоследний сигнал: {status['time']}\n"
        f"Плейлист: {status['playlist']}\nПодтверждено песен: {status['apple_tracks']}\n"
        f"Исходных записей: {status['source_count']}\n"
        f"Сейчас: {status['track']}\n"
        f"Шаг: {status['step']} ({status['step_seconds']} с)\n"
        f"Статусы: {json.dumps(status['counts'],ensure_ascii=False)}\n"
        f"{('Ошибка: '+status['error']) if status['error'] else ''}\n"
        "Живой индикатор: music_tool.py status --watch; для агента: agent.py status.\n")
    return status


def stop(child):
    if child.poll() is None:
        os.killpg(child.pid,signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid,signal.SIGKILL)
            child.wait()


def completed_count():
    progress=read_json(HERE/'catalog-progress.json')
    return sum(x.get('status') in ('verified','review','not_found','review_ui_error')
               for x in progress.get('tracks',{}).values())


def batch_arguments(arguments, completed):
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--limit',type=int,default=0)
    options,rest=parser.parse_known_args(arguments)
    if options.limit and completed>=options.limit:
        return None
    remaining=max(0,options.limit-completed) if options.limit else 0
    return rest+['--limit',str(remaining)]


def main():
    # Advisory lock prevents a second runner from writing to the same playlist.
    import fcntl
    HERE.mkdir(parents=True,exist_ok=True)
    lock=open(HERE/'runner.lock','w')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Перенос уже запущен. Проверь agent.py status.')
    child=None
    attempt=0
    failures=0
    batch_start=completed_count()
    try:
        while failures<3:
            attempt+=1
            completed_before=completed_count()
            arguments=batch_arguments(sys.argv[1:],completed_before-batch_start)
            if arguments is None:
                publish('Завершено — достигнут лимит тестовой порции',None,attempt)
                return
            (HERE/'current-step.json').write_text(json.dumps({'step':'Запуск обработки','since':time.time()},ensure_ascii=False))
            with open(HERE/'transfer.log','a',buffering=1) as log:
                log.write(f'\nRUN {attempt}: {datetime.datetime.now().isoformat()}\n')
                child=subprocess.Popen([sys.executable,'-u',str(CODE_DIR/'catalog_transfer.py'),*arguments],
                                       stdout=log,stderr=log,start_new_session=True)
                while child.poll() is None:
                    status=publish('Работает',child.pid,attempt)
                    if status['step_seconds']>120:
                        log.write('Шаг не завершился за 120 секунд. Перезапуск с сохранённого места.\n')
                        stop(child)
                        break
                    time.sleep(3)
                if child.returncode==0:
                    publish('Завершено — проверьте записи review/not_found/review_ui_error',None,attempt)
                    return
            failures=0 if completed_count()>completed_before else failures+1
            if failures<3:
                publish('Восстановление после ошибки',None,attempt)
                time.sleep(3)
        publish('Остановлен с ошибкой — требуется проверка',None,attempt)
        raise SystemExit(1)
    except KeyboardInterrupt:
        if child is not None:
            stop(child)
        publish('Остановлен пользователем',None,attempt)
        raise SystemExit(130)
    finally:
        if child is not None and child.poll() is None:
            stop(child)


if __name__=='__main__':
    main()
