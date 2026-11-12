#!/usr/bin/env python3
"""MusicToolkit: export Yandex metadata, transfer and organize macOS Music."""
import argparse
from engine.sources import identity as source_identity, source_name, transfer_directory, require_complete_spotify
from engine.music_lock import music_operation_lock
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
import unicodedata

ROOT=Path(__file__).resolve().parent
VERSION='0.3.0-dev'

def norm(value):
    value=unicodedata.normalize('NFKC',value or '').casefold().replace('ё','е')
    return re.sub(r'[^\w]+',' ',value).strip()

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def save(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.replace(path)

def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def osa(script,*args,js=False):
    if sys.platform!='darwin' or not shutil.which('osascript'):
        raise RuntimeError('Нужен Mac с macOS и приложением «Музыка».')
    cmd=['osascript']+(['-l','JavaScript'] if js else [])
    try:
        return subprocess.check_output(cmd+['-e',script,*map(str,args)],text=True,stderr=subprocess.PIPE,timeout=90).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('Apple Music: '+exc.stderr.strip()) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Apple Music не ответил за 90 секунд. Проверь приложение и повтори команду.') from exc

SNAPSHOT_JS='''var m=Application("Music");
function tracks(p){
if(!p.tracks.length)return [];
var a=p.tracks.name(),b=p.tracks.artist(),c=p.tracks.album(),d=p.tracks.genre(),e=p.tracks.duration(),f=p.tracks.persistentID();
return a.map(function(t,i){return {title:t,artist:b[i],album:c[i],genre:d[i],duration:e[i],id:f[i]};});
}
var lib=m.libraryPlaylists[0],library=tracks(lib),known={},extra=[],playlists=[];
library.forEach(function(t){known[t.id]=true;});
for(var i=0;i<m.userPlaylists.length;i++){
var p=m.userPlaylists[i],ids=p.tracks.length?p.tracks.persistentID():[];
playlists.push({name:p.name(),id:p.persistentID(),ids:ids});
if(ids.some(function(id){return !known[id];}))tracks(p).forEach(function(t){if(!known[t.id]){extra.push(t);known[t.id]=true;}});
}
JSON.stringify({library_id:lib.persistentID(),library:library,extra:extra,playlists:playlists});'''

def audit():
    raw=json.loads(osa(SNAPSHOT_JS,js=True))
    rows={t['id']:dict(t,in_library=True,sources=[{'kind':'library','id':raw['library_id']}]) for t in raw['library']}
    for t in raw['extra']:
        rows.setdefault(t['id'],dict(t,in_library=False,sources=[]))
    for p in raw['playlists']:
        for pid in set(p['ids']):
            if pid not in rows:
                raise RuntimeError('Состав Music изменился во время чтения. Повтори audit.')
            rows[pid]['sources'].append({'kind':'playlist','id':p['id'],'name':p['name']})
    return {'schema_version':1,'captured_at':stamp(),'library_id':raw['library_id'],
            'playlists':raw['playlists'],'tracks':list(rows.values())}

def has_artist(artist,wanted):
    return re.search(r'(?<!\w)'+re.escape(norm(wanted))+r'(?!\w)',norm(artist)) is not None

def fingerprint(track):
    # Preserve album/version differences. Ignore only case, never remix labels.
    return tuple(str(track.get(k,'')).strip().casefold() for k in ('artist','title','album'))

def same_recording(a,b):
    return fingerprint(a)==fingerprint(b) and abs(float(a.get('duration',0))-float(b.get('duration',0)))<1

def validate_rules(rules):
    def strings(value,label):
        if not isinstance(value,list) or not value or not all(isinstance(x,str) and x.strip() for x in value):
            raise ValueError(label+' должен быть непустым списком строк, например ["Бодро"].')
    if not isinstance(rules,dict):raise ValueError('rules.json должен содержать объект JSON.')
    for group,field in [('genre_rules','genres'),('artist_rules','artists')]:
        items=rules.get(group,[])
        if not isinstance(items,list):raise ValueError(group+' должен быть списком правил.')
        for rule in items:
            if not isinstance(rule,dict):raise ValueError(group+': нужно правило-объект.')
            strings(rule.get(field),group+'.'+field)
            strings(rule.get('playlists'),group+'.playlists')
    artists=rules.get('russian_rap_artists',[])
    if not isinstance(artists,list) or not all(isinstance(a,str) and a.strip() for a in artists):
        raise ValueError('russian_rap_artists должен быть списком имён.')
    rap=rules.get('rap_playlists',{})
    if not isinstance(rap,dict) or not all(isinstance(rap.get(k),str) and rap[k].strip() for k in ('russian','other')):
        raise ValueError('В rap_playlists нужны названия russian и other.')
    items=rules.get('track_rules',[])
    if not isinstance(items,list):raise ValueError('track_rules должен быть списком правил.')
    for rule in items:
        if not isinstance(rule,dict) or not all(isinstance(rule.get(k),str) and rule[k].strip() for k in ('artist','title')):
            raise ValueError('Для track_rules нужны artist и title.')
        if 'album' in rule and not isinstance(rule['album'],str):raise ValueError('album должен быть строкой.')
        if 'replace' in rule and not isinstance(rule['replace'],bool):raise ValueError('replace должен быть true или false.')
        strings(rule.get('playlists'),'track_rules.playlists')
    if not isinstance(rules.get('unmatched_playlist','Разобрать'),str) or not rules.get('unmatched_playlist','Разобрать').strip():
        raise ValueError('unmatched_playlist должен содержать название подборки.')

def classify(track,rules):
    g=norm(track.get('genre',''))
    destinations=set()
    reasons=[]
    for rule in rules.get('genre_rules',[]):
        if g and any(g==norm(value) for value in rule['genres']):
            destinations.update(rule['playlists'])
            reasons.append('жанр: '+track['genre'])
    for rule in rules.get('artist_rules',[]):
        if any(has_artist(track['artist'],a) for a in rule['artists']):
            destinations.update(rule['playlists'])
            reasons.append('правило исполнителя')
    rap=any(token in g for token in ('hip hop','hiphop','rap','рэп'))
    if rap:
        russian=bool(re.search('[А-Яа-яЁё]',track['artist'])) or 'russian' in g or any(has_artist(track['artist'],a) for a in rules.get('russian_rap_artists',[]))
        destinations.add(rules['rap_playlists']['russian' if russian else 'other'])
        reasons.append('рэп: жанр и ориентировочная группировка по исполнителю')
    for rule in rules.get('track_rules',[]):
        if norm(rule['artist'])==norm(track['artist']) and norm(rule['title'])==norm(track['title']) and ('album' not in rule or norm(rule['album'])==norm(track['album'])):
            if rule.get('replace',False):destinations.clear()
            destinations.update(rule['playlists'])
            reasons.append('правило конкретной песни')
    review=not destinations
    if review:
        destinations.add(rules.get('unmatched_playlist','Разобрать'))
        reasons.append('не удалось уверенно определить жанр')
    return sorted(destinations),reasons,review

def unique_playlist(snapshot,name,required=False):
    found=[p for p in snapshot['playlists'] if p['name']==name]
    if len(found)>1:
        raise ValueError(f'Несколько плейлистов называются «{name}». Дай им разные имена перед запуском.')
    if required and not found:raise ValueError(f'Плейлист «{name}» не найден.')
    return found[0] if found else None

def build_plan(snapshot,rules,include=(),exclude=()):
    validate_rules(rules)
    available={t['id']:t for t in snapshot['tracks']}
    selected=set(available)
    if include:
        selected=set().union(*(set(unique_playlist(snapshot,n,True)['ids']) for n in include))
    for name in exclude:
        selected-=set(unique_playlist(snapshot,name,True)['ids'])
    if not selected<=available.keys():raise ValueError('Неполный снимок медиатеки; повтори audit.')
    assignments=[]
    playlists={}
    for t in snapshot['tracks']:
        if t['id'] not in selected:continue
        destinations,reasons,review=classify(t,rules)
        assignments.append({'track':t,'destinations':destinations,'reasons':reasons,'needs_review':review})
        for name in destinations:
            if not name.strip() or name in ('Музыка','Music','Любимые песни','Favorite Songs','Сейчас нравится'):
                raise ValueError('Недопустимое название подборки: '+repr(name))
            unique_playlist(snapshot,name)
            playlists.setdefault(name,[]).append(t['id'])
    return {'schema_version':1,'created_at':stamp(),'library_id':snapshot['library_id'],
            'source_count':len(assignments),'scope':{'include':list(include),'exclude':list(exclude)},
            'review_count':sum(x['needs_review'] for x in assignments),
            'playlists':[{'name':n,'track_ids':ids} for n,ids in playlists.items()],
            'assignments':assignments}

def plan_text(plan):
    lines=[f'План: {plan["source_count"]} записей; на разбор: {plan["review_count"]}.',
           'Основа: метаданные и правила. Аудио не прослушивается.',
           'Настроенческие подборки наполняются только явными правилами track_rules.','']
    rows={r['track']['id']:r['track'] for r in plan['assignments']}
    for p in plan['playlists']:
        lines.extend([f'{p["name"]} — {len(p["track_ids"])} записей',''])
        lines.extend(f'{i}. {rows[pid]["artist"]} — {rows[pid]["title"]}' for i,pid in enumerate(p['track_ids'],1))
        lines.append('')
    return '\n'.join(lines)+'\n'

COPY_SCRIPT='''on run argv
tell application "Music"
set destName to item 1 of argv
set sourceKind to item 2 of argv
set sourceID to item 3 of argv
set trackID to item 4 of argv
if sourceKind is "library" then
set src to library playlist 1
else
set src to first user playlist whose persistent ID is sourceID
end if
set t to first track of src whose persistent ID is trackID
if not (exists user playlist destName) then make new user playlist with properties {name:destName}
set dest to first user playlist whose name is destName
if not (exists (first track of dest whose persistent ID is trackID)) then duplicate t to dest
if not (exists (first track of dest whose persistent ID is trackID)) then error "Copy could not be verified"
return count of tracks of dest
end tell
end run'''

def apply_plan(plan,snapshot,writer,dry_run=False,progress=None):
    if plan.get('schema_version')!=1 or plan['library_id']!=snapshot['library_id']:
        raise ValueError('План создан для другой медиатеки или версии. Сформируй новый план.')
    rows={t['id']:t for t in snapshot['tracks']}
    planned={r['track']['id']:r['track'] for r in plan['assignments']}
    if len(planned)!=len(plan['assignments']):raise ValueError('Повторные ID в плане.')
    # Complete validation before the first write.
    targets=set()
    for p in plan['playlists']:
        if not p['name'].strip() or p['name'] in ('Музыка','Music','Любимые песни','Favorite Songs','Сейчас нравится'):
            raise ValueError('Недопустимая подборка: '+p['name'])
        if p['name'] in targets:raise ValueError('Повторное имя подборки в плане.')
        targets.add(p['name'])
        unique_playlist(snapshot,p['name'])
        for pid in p['track_ids']:
            if pid not in planned or pid not in rows or not same_recording(planned[pid],rows[pid]):
                raise ValueError('Запись исчезла или изменилась: '+pid+'. Повтори audit и plan.')
            if not rows[pid].get('sources'):raise ValueError('Не указан источник песни: '+pid)
    selected=set().union(*(set(p['track_ids']) for p in plan['playlists'])) if plan['playlists'] else set()
    if selected!=set(planned):raise ValueError('Не все записи плана распределены.')
    result={'started_at':stamp(),'dry_run':dry_run,'source_count':len(planned),'playlists':[],'complete':False}
    for p in plan['playlists']:
        dest=unique_playlist(snapshot,p['name'])
        existing=[rows[pid] for pid in (dest['ids'] if dest else [])]
        summary={'name':p['name'],'before':len(existing),'added':0,'already_present':0,'memberships':[]}
        for pid in p['track_ids']:
            t=rows[pid]
            match=next((x for x in existing if x['id']==pid or same_recording(x,t)),None)
            if match:
                summary['already_present']+=1
                summary['memberships'].append({'source_id':pid,'destination_id':match['id']})
                continue
            if not dry_run:
                source=next((s for s in t['sources'] if s['kind']=='library'),t['sources'][0])
                writer(p['name'],source,t)
            existing.append(t)
            summary['added']+=1
            summary['memberships'].append({'source_id':pid,'destination_id':pid})
            if not dry_run:
                print(f'{p["name"]}: +{summary["added"]} — {t["artist"]} — {t["title"]}',flush=True)
                if progress:
                    progress(dict(result,current_playlist=summary,heartbeat=time.time()))
        summary['after']=len(existing)
        result['playlists'].append(summary)
        if progress:progress(dict(result,heartbeat=time.time()))
    result.update(complete=True,finished_at=stamp())
    if progress:progress(result)
    return result

def copy_to_playlist(name,source,track):
    osa(COPY_SCRIPT,name,source['kind'],source['id'],track['id'])

def verify_applied(plan,before,after,result):
    rows={t['id']:t for t in after['tracks']}
    for summary in result['playlists']:
        previous=unique_playlist(before,summary['name'])
        actual=unique_playlist(after,summary['name'],required=bool(summary['after']))
        ids=set(actual['ids']) if actual else set()
        if previous and not set(previous['ids'])<=ids:raise RuntimeError('Потерян прежний состав плейлиста.')
        if actual and len(ids)!=len(actual['ids']):raise RuntimeError('Повторные идентификаторы внутри плейлиста.')
        if not {m['destination_id'] for m in summary['memberships']}<=ids:
            raise RuntimeError('Не удалось подтвердить все добавления; повтори audit и apply.')
    result['verified_at']=stamp()
    result['verified']=True
    return result

def transfer_identity(export,playlist):
    return source_identity(export,playlist)


def run_engine(name,arguments,data_dir):
    env=dict(os.environ,MUSIC_TOOL_STATE_DIR=str(data_dir/'transfer'),PYTHONDONTWRITEBYTECODE='1')
    child=subprocess.Popen([sys.executable,'-u',str(ROOT/'engine'/name),*map(str,arguments)],env=env,start_new_session=True)
    try:
        return child.wait()
    except KeyboardInterrupt:
        # Let the runner stop its separate worker process group and save status.
        if child.poll() is None:
            os.killpg(child.pid,signal.SIGINT)
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGTERM)
                child.wait(timeout=10)
        raise

def show_status(data_dir):
    path=data_dir/'transfer/live-status.json'
    if path.exists():
        s=load(path)
        age=max(0,int(time.time()-s['heartbeat']))
        phase='НЕТ СВЕЖЕГО СИГНАЛА' if s['phase']=='Работает' and age>15 else s['phase']
        print(f'{phase}; сигнал {age} с назад; плейлист: {s.get("playlist","")}')
        print(f'Песен подтверждено: {s["apple_tracks"]}; исходных: {s["source_count"]}')
        print(f'{s["track"]}\nШаг: {s["step"]} ({s["step_seconds"]} с)\n{s["counts"]}')
    else:print('Статус переноса пока не создан.')
    path=data_dir/'sorting-result.json'
    if path.exists():
        s=load(path)
        print('Сортировка: '+('проверена' if s.get('verified') else 'требует проверки/продолжения'))
        for p in s['playlists']:print(f'  {p["name"]}: +{p["added"]}, уже было {p["already_present"]}')

def menu(data_dir):
    while True:
        print('\nMusicToolkit '+VERSION+'\n1. Проверить Mac\n2. Экспортировать Яндекс Музыку\n3. Перенести экспорт в Apple Music\n4. Составить план сортировки всей библиотеки\n5. Применить план сортировки\n6. Статус\n0. Выход')
        choice=input('Выбор: ').strip()
        if choice=='0':return 0
        args=['--data-dir',str(data_dir)]
        if choice=='1':args+=['doctor']
        elif choice=='2':args+=['export-yandex']
        elif choice=='3':
            exports=sorted((data_dir/'exports').glob('*/yandex_library.json'))
            default=str(exports[-1]) if exports else ''
            path=input('Путь к yandex_library.json'+(f' [Enter: {default}]' if default else '')+': ').strip().strip("\"'") or default
            if not path:continue
            print('Сначала будет создан отчёт совпадений. Сам перенос меняет медиатеку и плейлист «ЯM».')
            rc=main(args+['transfer',path])
            if rc==0 and input('Запустить перенос в Apple Music? [да/нет]: ').strip().casefold()=='да':
                args+=['transfer',path,'--apply']
            else:continue
        elif choice=='4':args+=['plan','--refresh']
        elif choice=='5':
            print('Проверь data/sorting-plan.txt и при необходимости rules.json. Будут добавлены песни и созданы подборки из плана.')
            if input('Применить план? [да/нет]: ').strip().casefold()!='да':continue
            args+=['apply']
        elif choice=='6':args+=['status']
        else:continue
        try:main(args)
        except (ValueError,RuntimeError,OSError,KeyError) as exc:print('Ошибка:',exc)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',action='version',version=VERSION)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data',help='Локальные данные и прогресс')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('menu',help='Интерактивное меню')
    sub.add_parser('doctor',help='Проверка Mac, Python и связи с Music')
    sub.add_parser('export-yandex',help='Экспорт через вход на сайте Яндекса')
    q=sub.add_parser('transfer',help='Предпросмотр переноса; запись только с --apply')
    q.add_argument('export',type=Path)
    q.add_argument('--playlist',default='ЯM')
    q.add_argument('--apply',action='store_true')
    q.add_argument('--limit',type=int,default=0,help='Максимум необработанных песен; 0 — все')
    q=sub.add_parser('audit',help='Прочитать медиатеку и все плейлисты')
    q.add_argument('--output',type=Path)
    q=sub.add_parser('plan',help='Создать план без изменения Apple Music')
    q.add_argument('--snapshot',type=Path)
    q.add_argument('--refresh',action='store_true')
    q.add_argument('--rules',type=Path,default=ROOT/'rules.json')
    q.add_argument('--source',action='append',default=[],help='Только этот плейлист; можно повторять')
    q.add_argument('--exclude-playlist',action='append',default=[])
    q.add_argument('--output',type=Path)
    q=sub.add_parser('apply',help='Добавить песни по проверенному плану')
    q.add_argument('plan',type=Path,nargs='?')
    q.add_argument('--dry-run',action='store_true',help='Проверить план на текущей медиатеке без записи')
    q=sub.add_parser('status',help='Прогресс переноса и сортировки')
    q.add_argument('--watch',action='store_true')
    args=parser.parse_args(argv)
    data_dir=args.data_dir.expanduser().resolve()
    if args.command=='menu':return menu(data_dir)
    if args.command=='doctor':
        print('Python:',sys.version.split()[0],'; macOS:',sys.platform=='darwin')
        print('Экспорт Яндекса:', 'зависимость установлена' if importlib.util.find_spec('yandex_music') else 'нужен Install.command')
        print('Music:',osa('Application("Music").version()',js=True))
        print('Связь с Music работает. Доступность интерфейсного переноса проверяется при первом поиске.')
    elif args.command=='export-yandex':
        return run_engine('export_yandex.py',['--output',data_dir/'exports'],data_dir)
    elif args.command=='transfer':
        export=args.export.expanduser().resolve()
        identity=transfer_identity(export,args.playlist)
        source=load(export)
        if source_name(source)=='spotify' and args.apply:
            require_complete_spotify(source)
        data_dir=transfer_directory(data_dir,source)
        if args.limit<0:raise ValueError('--limit должен быть >= 0.')
        if args.playlist in ('Музыка','Music','Любимые песни','Favorite Songs','Сейчас нравится') or not args.playlist.strip():raise ValueError('Выбери отдельный пользовательский плейлист для переноса.')
        if not args.apply:
            report=data_dir/'transfer-preview.json'
            report.parent.mkdir(parents=True,exist_ok=True)
            rc=run_engine('transfer.py',[export,'--playlist',args.playlist,'--report',report],data_dir)
            print('Отчёт:',report,'\nЭто только совпадения с уже имеющейся медиатекой. Поиск каталога начнётся с --apply.')
            return rc
        metadata=data_dir/'transfer/import.json'
        if metadata.exists() and load(metadata)!=identity:
            raise ValueError('Здесь прогресс другого экспорта/плейлиста. Выбери отдельный --data-dir; прежний прогресс сохранён.')
        save(metadata,identity)
        print('Перенос изменяет медиатеку и плейлист. Не переключай экран/поиск Music. Остановка: Ctrl+C.',flush=True)
        return run_engine('run_transfer.py',[export,'--playlist',args.playlist,'--state',data_dir/'transfer/catalog-progress.json','--expanded-search','--limit',args.limit],data_dir)
    elif args.command=='audit':
        snapshot=audit()
        path=args.output or data_dir/'library.json'
        save(path,snapshot)
        print(f'{len(snapshot["tracks"])} уникальных записей; {len(snapshot["playlists"])} плейлистов. Снимок: {path}')
    elif args.command=='plan':
        path=args.snapshot or data_dir/'library.json'
        if args.refresh or not path.exists():save(path,audit())
        plan=build_plan(load(path),load(args.rules),args.source,args.exclude_playlist)
        output=args.output or data_dir/'sorting-plan.json'
        save(output,plan)
        output.with_suffix('.txt').write_text(plan_text(plan),encoding='utf-8')
        print(f'Записей: {plan["source_count"]}; на разбор: {plan["review_count"]}. План: {output.with_suffix(".txt")}')
        for p in plan['playlists']:print(f'{p["name"]}: {len(p["track_ids"])}')
    elif args.command=='apply':
        plan=load(args.plan or data_dir/'sorting-plan.json')
        result_path=data_dir/'sorting-result.json'
        # Share the Music lock with transfers, including other checkouts/accounts.
        import fcntl
        with music_operation_lock(), open(ROOT/'.apply.lock','w') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Применение плана уже запущено.')
            before=audit()
            result=apply_plan(plan,before,copy_to_playlist,args.dry_run,None if args.dry_run else lambda s:save(result_path,s))
            if not args.dry_run:
                result=verify_applied(plan,before,audit(),result)
                save(result_path,result)
        for p in result['playlists']:print(f'{p["name"]}: +{p["added"]}, уже было {p["already_present"]}, всего {p["after"]}')
        print('Проверка без записи завершена.' if args.dry_run else 'Состав подборок проверен в Apple Music.')
    elif args.command=='status':
        while True:
            show_status(data_dir)
            if not args.watch:break
            time.sleep(2)
    return 0

if __name__=='__main__':
    try:
        if sys.version_info<(3,9):raise RuntimeError('Нужен Python 3.9 или новее.')
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nОстановлено. Для продолжения повтори команду; результаты уже добавленных песен сохранятся.')
        sys.exit(130)
    except (ValueError,RuntimeError,OSError,KeyError) as exc:
        print('Ошибка:',exc,file=sys.stderr)
        sys.exit(1)
