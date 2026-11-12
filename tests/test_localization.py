"""Exercise the same AppleScript matcher used for actual menu clicks, without UI."""
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
import catalog_transfer
from ui_labels import menu_labels

class MenuActionTests(unittest.TestCase):
    def test_catalog_passes_both_languages_to_bridge(self):
        for action in ('go-to-album','add-to-library'):
            with self.subTest(action=action), patch.object(catalog_transfer,'bridge',return_value='clicked') as bridge:
                self.assertEqual(catalog_transfer.menu_command(action),'clicked')
                bridge.assert_called_once_with('menu-command',*menu_labels(action))

    def test_unknown_action_is_rejected_before_ui(self):
        with patch.object(catalog_transfer,'bridge') as bridge:
            with self.assertRaises(ValueError):catalog_transfer.menu_command('remove-from-library')
            bridge.assert_not_called()

@unittest.skipUnless(sys.platform=='darwin' and shutil.which('osascript'),'AppleScript matcher requires macOS')
class AppleScriptMenuTests(unittest.TestCase):
    def resolve(self,action,names,aliases=None,check=True):
        wanted=aliases if aliases is not None else menu_labels(action)
        return subprocess.run(['osascript',str(ROOT/'engine/music_bridge.applescript'),
                               'resolve-menu',str(len(wanted)),*wanted,*names],
                              capture_output=True,text=True,timeout=15,check=check)

    def test_russian_and_english_menu_actions(self):
        fixtures=[
            ('go-to-album',['Play Next','Go to Album','Add to Library'],2),
            ('go-to-album',['Воспроизвести далее','Перейти к альбому','Поделиться'],2),
            ('add-to-library',['Go to Album','Add to Library','Add to Playlist…'],2),
            ('add-to-library',['Перейти к альбому','Добавить в\u00a0медиатеку','Добавить в плейлист'],2),
        ]
        for action,names,index in fixtures:
            with self.subTest(action=action,names=names):
                self.assertEqual(self.resolve(action,names).stdout.strip(),str(index))

    def test_case_and_unicode_spaces(self):
        for name in ('  ADD\u00a0To   LIBRARY  ','Add\u202fto\tlibrary','Добавить в  медиатеку'):
            with self.subTest(name=name):
                self.assertEqual(self.resolve('add-to-library',['Other',name]).stdout.strip(),'2')

    def test_similar_or_unknown_commands_are_not_selected(self):
        names=['Remove from Library','Add to Playlist…','Add Album to Library','Ajouter à la bibliothèque']
        self.assertEqual(self.resolve('add-to-library',names).stdout.strip(),'0')
        self.assertEqual(self.resolve('go-to-album',['Go to Artist','Go to Album in Store']).stdout.strip(),'0')

    def test_ambiguous_menu_fails_before_click(self):
        result=self.resolve('add-to-library',['Add to Library','Добавить в медиатеку'],check=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Ambiguous',result.stderr)

    def test_duplicate_aliases_do_not_make_one_item_ambiguous(self):
        result=self.resolve('add-to-library',['Добавить в\u00a0медиатеку'],
                            aliases=['Добавить в медиатеку','Добавить в\u00a0медиатеку','Add to Library'])
        self.assertEqual(result.stdout.strip(),'1')

    def test_empty_menu(self):
        self.assertEqual(self.resolve('add-to-library',[]).stdout.strip(),'0')

if __name__=='__main__':unittest.main()


class BatchLimitTests(unittest.TestCase):
    def test_restarts_do_not_reset_batch_budget(self):
        from run_transfer import batch_arguments
        self.assertEqual(batch_arguments(['input.json','--limit','5'],2), ['input.json','--limit','3'])
        self.assertIsNone(batch_arguments(['input.json','--limit','5'],5))
        self.assertIsNone(batch_arguments(['input.json','--limit=5'],6))
        self.assertEqual(batch_arguments(['input.json'],100),['input.json','--limit','0'])
