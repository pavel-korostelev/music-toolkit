import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import agent
from engine import export_yandex
spec=importlib.util.spec_from_file_location('build_archive',ROOT/'scripts/build_archive.py')
archive=importlib.util.module_from_spec(spec);spec.loader.exec_module(archive)

class AgentTests(unittest.TestCase):
    def test_latest_export_and_resume_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp)
            old=data/'exports/001/yandex_library.json';old.parent.mkdir(parents=True);old.write_text('{"playlists":[]}')
            new=data/'exports/002/yandex_library.json';new.parent.mkdir(parents=True);new.write_text('{"playlists":[],"errors":[]}')
            self.assertEqual(agent.resolve_export(data),new.resolve())
            state=data/'transfer/import.json';state.parent.mkdir()
            import hashlib
            state.write_text(json.dumps({'sha256':hashlib.sha256(old.read_bytes()).hexdigest()}))
            self.assertEqual(agent.resolve_export(data),old.resolve())
            old.unlink()
            with self.assertRaises(ValueError):agent.resolve_export(data)

    def test_stale_status_and_unprocessed_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp);(data/'transfer').mkdir()
            (data/'transfer/live-status.json').write_text(json.dumps({'heartbeat':time.time()-30,'phase':'Работает'}))
            (data/'transfer/catalog-progress.json').write_text(json.dumps({'source_count':3,'tracks':{'1':{'status':'verified'},'2':{'status':'review'}}}))
            result=agent.status(data)
            self.assertTrue(result['transfer']['stale'])
            self.assertEqual(result['unprocessed_count'],1)
            self.assertEqual(result['transfer_counts'],{'verified':1,'review':1})
            self.assertNotEqual(result['suggested_state'],'running')

    def test_authorization_status_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            data=Path(tmp)
            f=data/'auth-request.json'
            f.write_text(json.dumps({'verification_url':'https://oauth.yandex.ru/device','user_code':'EXAMPLE','created_at_unix':time.time(),'expires_in':600}))
            self.assertEqual(agent.status(data)['suggested_state'],'awaiting_yandex_login')
            f.write_text(json.dumps({'created_at_unix':0,'expires_in':1}))
            self.assertEqual(agent.status(data)['suggested_state'],'authorization_expired')

    def test_setup_idempotent_without_global_installs(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(agent.sys,'platform','darwin'):
            python=Path(tmp)/'python3';python.touch()
            with patch.object(agent,'PYTHON',python),patch.object(agent,'dependency_version',return_value='3.2.2'),patch.object(agent.subprocess,'run') as run,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(agent.setup(),0)
                run.assert_not_called()

    def test_cloud_host_stops_before_install(self):
        with patch.object(agent.sys,'platform','linux'),patch.object(agent.subprocess,'run') as run:
            with self.assertRaises(RuntimeError):agent.setup()
            run.assert_not_called()

    def test_login_handoff_contains_code_but_never_access_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'auth-request.json'
            code=SimpleNamespace(verification_url='https://oauth.yandex.ru/device',user_code='EXAMPLE-CODE',expires_in=120,access_token='DO-NOT-STORE')
            with patch.dict('os.environ',{'MUSIC_TOOL_AUTH_FILE':str(path)}),patch.object(export_yandex.webbrowser,'open'),contextlib.redirect_stdout(io.StringIO()):
                export_yandex.show_code(code)
            value=json.loads(path.read_text())
            self.assertEqual(value['user_code'],'EXAMPLE-CODE')
            self.assertNotIn('access_token',value)
            self.assertNotIn('DO-NOT-STORE',path.read_text())

    def test_failed_login_removes_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'auth-request.json'
            class FakeClient:
                def device_auth(self,on_code,**kwargs):
                    on_code(SimpleNamespace(verification_url='https://oauth.yandex.ru/device',user_code='EXAMPLE'))
                    raise RuntimeError('example failure')
            with patch.dict('os.environ',{'MUSIC_TOOL_AUTH_FILE':str(path)}),patch.dict(sys.modules,{'yandex_music':SimpleNamespace(Client=FakeClient)}),patch.object(sys,'argv',['export_yandex.py']),patch.object(export_yandex.webbrowser,'open'),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(export_yandex.main(),1)
            self.assertFalse(path.exists())

class ArchiveTests(unittest.TestCase):
    def test_private_files_and_unsafe_paths_rejected(self):
        for path in ('data/library.json','.venv/token.txt','../file.py','auth-request.json','debug.log','credentials.pem'):
            with self.subTest(path=path),self.assertRaises(ValueError):archive.validate_path(path)

    def test_personal_paths_and_credential_like_contents_rejected(self):
        personal='/'+'Users'+'/'+'example-user'+'/file.json'
        token='ghp_'+'A'*40
        for text in (personal,token):
            with self.assertRaises(ValueError):archive.validate_content('example.txt',text)

    def test_zip_download_repackages_source_without_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'checkout with spaces';root.mkdir()
            for name in ('README.md','AGENTS.md','agent.py','music_tool.py'):(root/name).write_text('example')
            (root/'data').mkdir();(root/'data/library.json').write_text('private')
            (root/'.venv').mkdir();(root/'.venv/token.txt').write_text('private')
            output=Path(tmp)/'artifact.zip'
            names=archive.build(root,output)
            self.assertEqual(set(names),{'README.md','AGENTS.md','agent.py','music_tool.py'})

if __name__=='__main__':unittest.main()
