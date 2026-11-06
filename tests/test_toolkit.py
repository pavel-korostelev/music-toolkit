import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import music_tool as tool
from engine import transfer

def track(pid='T1',artist='Sample Artist',title='Sample Song',genre='Rock',album='Album',duration=200):
    return dict(id=pid,artist=artist,title=title,genre=genre,album=album,duration=duration,
                in_library=True,sources=[{'kind':'library','id':'TEST-LIBRARY'}])

def snapshot(tracks,playlists=None):
    return dict(schema_version=1,library_id='TEST-LIBRARY',tracks=tracks,playlists=playlists or [])

class ClassifierTests(unittest.TestCase):
    def setUp(self):self.rules=tool.load(ROOT/'rules.json')

    def test_russian_rapper_rock_remix_gets_both_genres(self):
        t=track(artist='MORGENSHTERN & SLAVA MARLOW',title='Sample (ROCK REMIX)',genre='Alternative')
        dest,_,review=tool.classify(t,self.rules)
        self.assertEqual(set(dest),{'Рок и инди','Русский рэп'})
        self.assertFalse(review)

    def test_latin_russian_rap_alias_and_foreign_rap(self):
        for artist in ('ATL','Oxxxymiron','GUF','Aarne, Toxi$ & Big Baby Tape'):
            dest,_,_=tool.classify(track(artist=artist,genre='Hip-Hop/Rap'),self.rules)
            self.assertIn('Русский рэп',dest)
            self.assertNotIn('Зарубежный хип-хоп',dest)
        self.assertIn('Зарубежный хип-хоп',tool.classify(track(artist='Example English Artist',genre='Hip-Hop/Rap'),self.rules)[0])

    def test_artist_rules_do_not_match_parts_of_other_names(self):
        self.assertFalse(tool.has_artist('Seattle Example','ATL'))
        self.assertTrue(tool.has_artist('ATL & Another Artist','ATL'))

    def test_kpop_not_silently_put_in_rock(self):
        self.assertEqual(tool.classify(track(genre='K-Pop'),self.rules)[0],['K-pop'])

    def test_unknown_is_reviewed_instead_of_dropped(self):
        dest,_,review=tool.classify(track(genre='Unfamiliar Genre'),self.rules)
        self.assertTrue(review)
        self.assertEqual(dest,['Разобрать'])

    def test_bad_rules_cannot_create_one_playlist_per_character(self):
        self.rules['track_rules']=[{'artist':'A','title':'B','playlists':'Бодро'}]
        with self.assertRaises(ValueError):tool.build_plan(snapshot([track()]),self.rules)

    def test_mood_requires_explicit_track_rule_and_preserves_genre(self):
        t=track()
        self.assertNotIn('Бодро',tool.classify(t,self.rules)[0])
        self.rules['track_rules']=[{'artist':t['artist'],'title':t['title'],'playlists':['Бодро']}]
        self.assertEqual(set(tool.classify(t,self.rules)[0]),{'Рок и инди','Бодро'})
        self.rules['track_rules'][0]['replace']=True
        self.assertEqual(tool.classify(t,self.rules)[0],['Бодро'])

    def test_album_rule_keeps_other_versions_separate(self):
        self.rules['track_rules']=[{'artist':'Sample Artist','title':'Sample Song','album':'Live Album','replace':True,'playlists':['Концерты']}]
        self.assertNotIn('Концерты',tool.classify(track(),self.rules)[0])
        self.assertEqual(tool.classify(track(album='Live Album'),self.rules)[0],['Концерты'])

class PlanAndApplyTests(unittest.TestCase):
    def setUp(self):self.rules=tool.load(ROOT/'rules.json')

    def test_playlist_only_tracks_and_exclusion(self):
        a=track('A');b=track('B',title='Second')
        b.update(in_library=False,sources=[{'kind':'playlist','id':'LIKES','name':'Likes'}])
        s=snapshot([a,b],[{'name':'Archive','id':'P1','ids':['A']},{'name':'Likes','id':'LIKES','ids':['B']}])
        p=tool.build_plan(s,self.rules,exclude=['Archive'])
        self.assertEqual([x['track']['id'] for x in p['assignments']],['B'])
        self.assertEqual(p['assignments'][0]['track']['sources'][0]['id'],'LIKES')

    def test_duplicate_destination_names_stop_before_writing(self):
        s=snapshot([track()],[{'name':'Рок и инди','id':'P1','ids':[]},{'name':'Рок и инди','id':'P2','ids':[]}])
        with self.assertRaises(ValueError):tool.build_plan(s,self.rules)

    def test_dry_run_never_calls_writer(self):
        s=snapshot([track()]);p=tool.build_plan(s,self.rules)
        result=tool.apply_plan(p,s,lambda *args:self.fail('write in dry run'),dry_run=True)
        self.assertEqual(result['playlists'][0]['added'],1)
        self.assertEqual(s['playlists'],[])

    def test_stale_plan_stops_all_writes(self):
        s=snapshot([track('A'),track('B',title='Second')]);p=tool.build_plan(s,self.rules)
        s['tracks'].pop()
        with self.assertRaises(ValueError):tool.apply_plan(p,s,lambda *a:self.fail('partial write before validation'))

    def test_other_library_is_rejected(self):
        s=snapshot([track()]);p=tool.build_plan(s,self.rules);s['library_id']='OTHER'
        with self.assertRaises(ValueError):tool.apply_plan(p,s,lambda *a:self.fail('wrong library'))

    def test_repeat_apply_is_idempotent_and_preserves_existing(self):
        old=track('OLD',title='Existing Song');new=track('NEW',title='New Song')
        s=snapshot([old,new],[{'name':'Рок и инди','id':'ROCK','ids':['OLD']}])
        p=tool.build_plan(s,self.rules);before=copy.deepcopy(s);calls=[]
        def write(name,source,t):
            calls.append(t['id']);s['playlists'][0]['ids'].append(t['id'])
        result=tool.apply_plan(p,s,write)
        self.assertEqual(calls,['NEW'])
        self.assertTrue(tool.verify_applied(p,before,s,result)['verified'])
        again=tool.apply_plan(p,s,lambda *a:self.fail('duplicate write'))
        self.assertEqual(again['playlists'][0]['added'],0)
        self.assertEqual(set(s['playlists'][0]['ids']),{'OLD','NEW'})

    def test_equivalent_ids_not_duplicated_but_remix_is_added(self):
        first=track('A');duplicate=track('B');remix=track('C',title='Sample Song (Remix)')
        s=snapshot([first,duplicate,remix]);p=tool.build_plan(s,self.rules);calls=[]
        result=tool.apply_plan(p,s,lambda name,source,t:calls.append(t['id']))
        self.assertEqual(calls,['A','C'])
        self.assertEqual(result['playlists'][0]['already_present'],1)

class TransferTests(unittest.TestCase):
    def test_strict_artist_version_duration_matching(self):
        source={'title':'Example','artists':['GUF'],'version':None,'duration_ms':200000}
        self.assertTrue(transfer.matches(source,dict(artist='Гуф',title='Example',duration=201)))
        self.assertFalse(transfer.matches(source,dict(artist='Гуф',title='Example (Live)',duration=201)))
        self.assertFalse(transfer.matches(source,dict(artist='Other',title='Example',duration=201)))
        self.assertFalse(transfer.matches(source,dict(artist='Гуф',title='Example',duration=210)))
        source['version']='Remix'
        self.assertFalse(transfer.matches(source,dict(artist='Гуф',title='Example',duration=201)))
        self.assertTrue(transfer.matches(source,dict(artist='Гуф',title='Example (Remix)',duration=201)))

    def test_resume_identity_changes_with_export_or_playlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'export.json'
            path.write_text(json.dumps({'playlists':[{'tracks':[]}]}))
            first=tool.transfer_identity(path,'Archive')
            self.assertNotEqual(first,tool.transfer_identity(path,'Other'))
            path.write_text(json.dumps({'playlists':[],'errors':[]}))
            self.assertNotEqual(first,tool.transfer_identity(path,'Archive'))

if __name__=='__main__':unittest.main()
