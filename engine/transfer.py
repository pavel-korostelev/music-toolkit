#!/usr/bin/env python3
"""Local Yandex export -> existing Apple Music library -> playlist.

Does not download audio, use account tokens, or change existing tracks.
Only verified matches already present in the library are copied.
"""
import argparse
import json
import re
import subprocess
import unicodedata
from pathlib import Path


def osa(script, *args, javascript=False):
    cmd = ['osascript'] + (['-l', 'JavaScript'] if javascript else [])
    return subprocess.check_output(cmd + ['-e', script, *args], text=True, timeout=90).strip()


def norm(text):
    text = unicodedata.normalize('NFKC', text or '').casefold().replace('ё', 'е')
    return re.sub(r'[^\w]+', ' ', text).strip()


def base_title(text):
    return norm(re.sub(r'\s*[\[(](?:feat\.?|ft\.?).*?[\])]', '', text, flags=re.I))


def artist_match(source, candidate):
    alias = {'guf': 'гуф', 'masha medvedi': 'маша и медведи',
             'aigel':'аигел','chizh co':'чиж co','the чиж co':'чиж co',
             'the chizh co':'чиж co','группа скриптонит':'gruppa skryptonite',
             'max korzh':'макс корж','krec':'крек',
             'amir obe':'amir obè','damian jr gong marley':'damian marley'}
    canonical = lambda s: alias.get(norm(s), norm(s))
    raw_candidate = candidate
    candidate = canonical(candidate)
    source_names = [canonical(a) for a in source['artists']]
    parts = [canonical(x) for x in re.split(r'\s*(?:,|&|;|\bfeat\.?|\bft\.?|\band\b|\bи\b)\s*', raw_candidate, flags=re.I)]
    return candidate in source_names or any(a in parts for a in source_names if a)


def matches(source, candidate):
    title = candidate['title']
    if not artist_match(source, candidate['artist']):
        return False
    src_title, dst_title = base_title(source['title']), base_title(title)
    # A version may be in a dedicated Yandex field and in Apple's track title.
    version = source.get('version')
    if version:
        if norm(version) not in norm(title):
            return False
        dst_title = base_title(re.sub(r'\s*[\[(]' + re.escape(version) + r'[\])]', '', title, flags=re.I))
    if src_title != dst_title:
        return False
    if source.get('duration_ms') is None:
        return False
    return bool(source.get('artists') and src_title) and abs(source['duration_ms'] / 1000 - candidate['duration']) <= 3


LIBRARY_JS = '''var m=Application("Music"), p=m.libraryPlaylists[0];
if (!p.tracks.length) { JSON.stringify({names:[],artists:[],albums:[],durations:[],ids:[]}); } else {
JSON.stringify({names:p.tracks.name(),artists:p.tracks.artist(),albums:p.tracks.album(),
durations:p.tracks.duration(),ids:p.tracks.persistentID()}); }'''


def read_library():
    data = json.loads(osa(LIBRARY_JS, javascript=True))
    return [dict(title=t, artist=a, album=b, duration=d, id=i) for t,a,b,d,i in
            zip(data['names'],data['artists'],data['albums'],data['durations'],data['ids'])]


def copy_track(track_id, playlist):
    # IDs and playlist are passed as argv; no generated AppleScript interpolation.
    return osa('''on run argv
tell application "Music"
set destName to item 1 of argv
set trackID to item 2 of argv
if (count of (every user playlist whose name is destName)) > 1 then error "Multiple playlists have this name"
if not (exists user playlist destName) then make new user playlist with properties {name:destName}
set p to first user playlist whose name is destName
if not (exists (first track of p whose persistent ID is trackID)) then
set t to first track of library playlist 1 whose persistent ID is trackID
duplicate t to p
end if
if not (exists (first track of p whose persistent ID is trackID)) then error "Copy verification failed"
return count of tracks of p
end tell
end run''', playlist, track_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', type=Path)
    parser.add_argument('--playlist', default='ЯM')
    parser.add_argument('--apply', action='store_true', help='Copy matches; default only writes a report')
    parser.add_argument('--report', type=Path, default=Path(__file__).with_name('progress.json'))
    args = parser.parse_args()
    export = json.loads(args.export.read_text())
    # First occurrence preserves the source order.
    unique = {}
    for playlist in export['playlists']:
        for track in playlist['tracks']:
            unique.setdefault(track['yandex_id'], track)
    library = read_library()
    report = {'source_count':len(unique), 'playlist':args.playlist, 'applied':args.apply, 'tracks':[]}
    copied_ids = set()
    for track in unique.values():
        candidates = [c for c in library if matches(track, c)]
        preferred = [c for c in candidates if norm(c['album']) in [norm(a) for a in track['albums']]]
        candidates = preferred or candidates
        entry = {'source':track, 'status':'needs_catalog_search'}
        if len(candidates) == 1:
            c = candidates[0]
            entry.update(status='matched_existing', apple=c)
            if args.apply:
                copy_track(c['id'], args.playlist)
                entry['status'] = 'verified_in_playlist'
                copied_ids.add(c['id'])
        elif candidates:
            entry.update(status='ambiguous_existing', candidates=candidates)
        report['tracks'].append(entry)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    report['verified_unique_apple_tracks'] = len(copied_ids)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='tracks'},ensure_ascii=False))


if __name__ == '__main__':
    main()
