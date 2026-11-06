#!/usr/bin/env python3
"""Transfer through the signed-in macOS Music app, without developer keys.

Requires macOS Automation/Accessibility access for the launching terminal.
Russian and English Music UI captions are supported. Do not interact with
Music while it runs.
"""
import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from transfer import base_title, artist_match, matches, norm, read_library, copy_track
from ui_labels import menu_labels

CODE_DIR = Path(__file__).resolve().parent
HERE = Path(os.environ.get('MUSIC_TOOL_STATE_DIR', str(CODE_DIR.parent / 'data/transfer')))


def step(label):
    target=HERE / 'current-step.json'
    tmp=target.with_suffix('.tmp')
    tmp.write_text(json.dumps({'step':label,'since':time.time()},ensure_ascii=False))
    tmp.replace(target)


def bridge(*args):
    # Only search/read operations are retried here. Writes are verified by
    # reading the library; the runner can safely resume using persistent IDs.
    attempts=3 if args[0] in ('search','candidates','album-data') else 1
    for attempt in range(attempts):
        step(' / '.join(map(str,args)))
        try:
            return subprocess.check_output(['osascript', str(CODE_DIR / 'music_bridge.applescript'), *map(str,args)],
                                           text=True, stderr=subprocess.PIPE, timeout=60).strip()
        except subprocess.CalledProcessError as exc:
            if attempt+1==attempts:
                raise RuntimeError(exc.stderr.strip() or str(exc)) from exc
            time.sleep(2)


def menu_command(action):
    return bridge('menu-command',*menu_labels(action))


def candidate_title_match(source,title):
    src=base_title(source['title'])
    dst=base_title(title)
    return src==dst or bool(source.get('version') and
                           norm(source['version']) in norm(title) and src in dst)


def search_source(source,expanded):
    result=dict(source)
    if not expanded:
        return result
    # Soundtrack/producers' credits describe usage, not a different recording.
    if re.match(r'^(из (?:фильма|сериала|мультфильма)|OST\b|PROD\.? BY\b)',
                result.get('version') or '',flags=re.I):
        result['version']=None
    return result


def search_queries(source,expanded):
    artist=source['artists'][0]
    title=source['title']
    if not expanded:
        return [' '.join([artist,title,source.get('version') or '']).strip()]
    result=[f'{artist} {title}']
    if source.get('version'):
        result.append(f'{artist} {title} {source["version"]}')
    aliases={'АИГЕЛ':'AIGEL','Чиж & Co':'Chizh & Co','Gruppa Skryptonite':'Группа Скриптонит',
             'Макс Корж':'Max Korzh','KREC':'КРЕК'}
    if artist in aliases:
        result.append(f'{aliases[artist]} {title}')
    result.append(title)
    return list(dict.fromkeys(result))


def candidates(source=None):
    result = []
    seen = set()
    for line in bridge('candidates').splitlines():
        ident, _, label = line.partition('\t')
        m = re.search(r'TrackLockup\[id=(\d+),', ident)
        if not m or m[1] in seen or ', ' not in label:
            continue
        # Track titles themselves may contain commas. Evaluate all boundaries
        # against the requested artist/title instead of cutting at the first comma.
        options=[]
        for boundary in re.finditer(', ',label):
            title,artist=label[:boundary.start()],label[boundary.end():]
            if source is None or (artist_match(source,artist) and candidate_title_match(source,title)):
                options.append((title,artist))
        if not options:
            continue
        title,artist=max(options,key=lambda x:len(x[0]))
        seen.add(m[1])
        result.append({'catalog_id':m[1], 'title':title, 'artist':artist})
    return result


def selected_track(source):
    fields = bridge('album-data').split('\t')
    durations = [x for x in fields if re.fullmatch(r'\d+:\d\d', x)]
    if len(durations) != 1:
        raise RuntimeError('Album track duration was not available')
    duration = sum(int(v)*60**i for i,v in enumerate(reversed(durations[0].split(':'))))
    text_fields=[x for x in fields if x and not re.fullmatch(r'\d+|\d+:\d\d',x)]
    title=text_fields[0] if text_fields else ''
    artist=text_fields[1] if len(text_fields)>1 else ' & '.join(source['artists'])
    return {'title':title,'duration':duration,'artist':artist}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('export',type=Path)
    p.add_argument('--playlist',default='ЯM')
    p.add_argument('--limit',type=int,default=0,help='Maximum unprocessed tracks, 0 means all')
    p.add_argument('--state',type=Path,default=HERE / 'catalog-progress.json')
    p.add_argument('--expanded-search',action='store_true',help='Try shorter queries and verified metadata spelling variants')
    args=p.parse_args()
    source=json.loads(args.export.read_text())
    tracks={}
    for playlist in source['playlists']:
        for t in playlist['tracks']:
            tracks.setdefault(t['yandex_id'], t)
    HERE.mkdir(parents=True,exist_ok=True)
    args.state.parent.mkdir(parents=True,exist_ok=True)
    state=json.loads(args.state.read_text()) if args.state.exists() else {}
    if state.get('playlist',args.playlist) != args.playlist:
        raise RuntimeError('Use a different state file for a different destination')
    state.update(playlist=args.playlist,source_count=len(tracks))
    state.setdefault('tracks',{})
    step('Чтение медиатеки')
    library=read_library()
    processed=0
    for source_id,t in tracks.items():
        previous=state['tracks'].get(source_id,{})
        if previous.get('status') in ('verified','review','not_found','review_ui_error'):
            continue
        if args.limit and processed>=args.limit:
            break
        processed+=1
        entry={'source':t,'status':'started','failed_attempts':previous.get('failed_attempts',0)}
        state['tracks'][source_id]=entry
        def save():
            tmp=args.state.with_suffix('.tmp')
            tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2))
            tmp.replace(args.state)
            counts={}
            for item in state['tracks'].values():
                counts[item['status']]=counts.get(item['status'],0)+1
            unique_apple={item['apple']['id'] for item in state['tracks'].values()
                          if item['status']=='verified' and item.get('apple')}
            args.state.with_suffix('.txt').write_text(
                f"Плейлист: {args.playlist}\nИсходных записей: {len(tracks)}\n"
                f"Проверено уникальных песен Apple Music: {len(unique_apple)}\n"
                f"Статусы: {json.dumps(counts,ensure_ascii=False)}\n"
                f"Текущая песня: {' & '.join(t['artists'])} — {t['title']}\n")
        save()
        print(f"[{processed}] {' & '.join(t['artists'])} — {t['title']}",flush=True)
        try:
            if not t.get('artists') or not t.get('title') or t.get('duration_ms') is None:
                entry.update(status='review',reason='Missing artist, title or duration')
                save()
                continue
            wanted=search_source(t,args.expanded_search)
            existing=[c for c in library if matches(wanted,c)]
            if existing:
                preferred=[c for c in existing if norm(c['album']) in [norm(a) for a in t['albums']]]
                eligible=preferred or existing
                if len(eligible)!=1:
                    entry.update(status='review',reason='Multiple library recordings match',candidates=eligible)
                    save()
                    continue
                chosen=eligible[0]
                step('Добавление в плейлист и проверка')
                copy_track(chosen['id'],args.playlist)
                entry.update(status='verified',apple=chosen)
                save()
                print('  => verified',flush=True)
                continue
            viable=[]
            entry['queries']=[]
            for query in search_queries(wanted,args.expanded_search):
                entry['queries'].append(query)
                bridge('search',query)
                time.sleep(1.5)
                found=candidates(wanted)
                viable=[c for c in found if artist_match(wanted,c['artist']) and candidate_title_match(wanted,c['title'])]
                if not viable and args.expanded_search:
                    time.sleep(2)
                    found=candidates(wanted)
                    viable=[c for c in found if artist_match(wanted,c['artist']) and candidate_title_match(wanted,c['title'])]
                if viable:
                    break
            entry['candidates']=viable
            added=False
            for c in viable[:3]:
                bridge('menu',c['catalog_id'])
                menu_command('go-to-album')
                time.sleep(1)
                try:
                    selected=selected_track(wanted)
                except (subprocess.SubprocessError,RuntimeError):
                    time.sleep(4)
                    selected=selected_track(wanted)
                if not matches(wanted,selected):
                    entry.setdefault('rejected',[]).append(selected)
                    # Each next candidate requires returning to search results.
                    bridge('search',query)
                    time.sleep(1.5)
                    continue
                bridge('album-menu')
                menu_command('add-to-library')
                for _ in range(15):
                    step('Ожидание появления песни в медиатеке')
                    time.sleep(1)
                    library=read_library()
                    saved=[x for x in library if matches(wanted,x)]
                    if saved:
                        preferred=[x for x in saved if norm(x['album']) in [norm(a) for a in t['albums']]]
                        eligible=preferred or saved
                        if len(eligible)!=1:
                            entry.update(status='review',reason='Multiple recordings after addition',candidates=eligible)
                            added=True
                            break
                        chosen=eligible[0]
                        step('Добавление в плейлист и проверка')
                        copy_track(chosen['id'],args.playlist)
                        entry.update(status='verified',apple=chosen)
                        added=True
                        break
                if not added:
                    raise RuntimeError('Library addition could not be verified; stopping for review')
                break
            if not added:
                entry['status']='review' if viable else 'not_found'
            save()
            print('  => '+entry['status'],flush=True)
        except Exception as exc:
            failures=entry['failed_attempts']+1
            entry.update(status='error' if failures<3 else 'review_ui_error',
                         error=str(exc),failed_attempts=failures)
            save()
            if failures<3:
                print(f'RETRY REQUIRED ({failures}/3): {exc}',flush=True)
                raise
            print(f'SKIPPED FOR REVIEW after 3 UI failures: {exc}',flush=True)
            library=read_library()
    statuses={}
    for item in state['tracks'].values():
        statuses[item['status']]=statuses.get(item['status'],0)+1
    print(json.dumps(statuses,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
