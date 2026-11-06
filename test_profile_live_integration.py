"""Local integration checks; camera actions and ping probes are mocked."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCE=Path(__file__).with_name('record.py')
SPEC=importlib.util.spec_from_file_location('profile_live_integration_under_test',SOURCE)
record=importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name]=record
SPEC.loader.exec_module(record)


class ControllerFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='profile-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=self.root/'recording.json'
        self.data={'output_dir':str(self.root/'videos'),'time_ranges':['06:00-07:00'],
                   'cameras':[{'name':'cam1','url':'rtsp://admin:TEST_SECRET@192.0.2.1:554/profile2/media.smp'},
                              {'name':'cam2','url':'rtsp://admin:OTHER_SECRET@192.0.2.2:554/profile2/media.smp'}]}
        self.write(self.data)
        self.controller=record.RecorderUiController(self.config)
        self.addCleanup(self.controller.close)

    def write(self,data):
        self.config.write_text(json.dumps(data),encoding='utf-8')


class ControllerIntegrationTests(ControllerFixture):
    def test_recording_profile_change_only_retargets_selected_url_and_backs_up(self):
        original=self.config.read_bytes()
        revision=self.controller.read_config()[1]
        with mock.patch.object(self.controller.camera_profiles,'read',return_value={'profiles':{'2':{},'4':{}}}) as read:
            result=self.controller.use_recording_profile('cam1','4',revision)
        read.assert_called_once_with('cam1')
        saved=json.loads(self.config.read_text())
        self.assertEqual(saved['cameras'][0]['url'],'rtsp://admin:TEST_SECRET@192.0.2.1:554/profile4/media.smp')
        self.assertEqual(saved['cameras'][1],self.data['cameras'][1])
        self.assertEqual(saved['time_ranges'],self.data['time_ranges'])
        backup=list(self.root.glob('.recording-profile-backup_*.json'))
        self.assertEqual(len(backup),1)
        self.assertEqual(json.loads(backup[0].read_text()),json.loads(original))
        self.assertNotIn('SECRET',json.dumps(result))
        if sys.platform=='linux':
            self.assertEqual(backup[0].stat().st_mode & 0o777,0o600)

    def test_stale_revision_busy_closed_or_absent_profile_cannot_change_config(self):
        original=self.config.read_bytes()
        revision=self.controller.read_config()[1]
        for state in ({'running':True},{'external_running':True}):
            with self.subTest(state=state),mock.patch.object(self.controller,'state',return_value=state),\
                    mock.patch.object(self.controller.camera_profiles,'read') as read,self.assertRaises(ValueError):
                self.controller.use_recording_profile('cam1','4',revision)
            read.assert_not_called()
        with mock.patch.object(self.controller.camera_profiles,'read') as read,self.assertRaises(ValueError):
            self.controller.use_recording_profile('cam1','4','stale')
        read.assert_not_called()
        with mock.patch.object(self.controller.camera_profiles,'read',return_value={'profiles':{'2':{}}}),self.assertRaises(ValueError):
            self.controller.use_recording_profile('cam1','4',revision)
        self.controller.closing=True
        with mock.patch.object(self.controller.camera_profiles,'read') as read,self.assertRaises(ValueError):
            self.controller.use_recording_profile('cam1','4',revision)
        read.assert_not_called()
        self.assertEqual(self.config.read_bytes(),original)
        self.assertEqual(list(self.root.glob('.recording-profile-backup_*.json')),[])

    def test_concurrent_settings_parses_cannot_replace_new_snapshot_with_old_one(self):
        entered,release,second_done=threading.Event(),threading.Event(),threading.Event()
        real=record.settings_from_data
        results,errors={},[]
        def parse(data,base):
            if data.get('intersection_name')=='old':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test release timeout')
            return real(data,base)
        def first():
            try: results['first']=self.controller.settings()
            except Exception as exc: errors.append(exc)
        def second():
            try: results['second']=self.controller.settings()
            except Exception as exc: errors.append(exc)
            finally: second_done.set()
        self.write(dict(self.data,intersection_name='old'))
        with mock.patch.object(record,'settings_from_data',side_effect=parse):
            a=threading.Thread(target=first);b=threading.Thread(target=second)
            a.start()
            try:
                self.assertTrue(entered.wait(2))
                self.write(dict(self.data,intersection_name='new'))
                b.start()
                self.assertFalse(second_done.wait(.1))
            finally:
                release.set();a.join(3)
                if b.ident is not None: b.join(3)
        self.assertFalse(errors)
        self.assertEqual(results['first'].cameras[0].intersection,'old')
        self.assertEqual(results['second'].cameras[0].intersection,'new')
        self.assertEqual(self.controller.cached_settings.cameras[0].intersection,'new')
        self.assertEqual(self.controller.cached_revision,self.controller.read_config()[1])

    def test_desktop_browser_bridge_reuses_controller_and_stops_on_close(self):
        server=mock.Mock()
        server.server_address=('127.0.0.1',8765)
        with mock.patch.object(record,'ThreadingHTTPServer',return_value=server) as factory,\
                mock.patch.object(record,'make_ui_handler',return_value=object()) as handler,\
                mock.patch.object(record.webbrowser,'open') as browser,\
                mock.patch.object(self.controller.camera_ping,'start'):
            self.controller.open_browser_controls()
            self.controller.open_browser_controls()
        factory.assert_called_once()
        self.assertIs(handler.call_args.args[0],self.controller)
        self.assertEqual(browser.call_args.args[0],'http://127.0.0.1:8765/#live')
        self.controller.close()
        server.shutdown.assert_called_once();server.server_close.assert_called_once()


class HttpIntegrationTests(ControllerFixture):
    def setUp(self):
        super().setUp()
        self.token='local-integration-token'
        self.server=record.ThreadingHTTPServer(('127.0.0.1',0),record.BaseHTTPRequestHandler)
        self.port=self.server.server_address[1]
        self.server.RequestHandlerClass=record.make_ui_handler(self.controller,self.token,self.port)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def request(self,path,body=None,token=True):
        headers={'X-Recorder-Token':self.token} if token else {}
        data=None if body is None else json.dumps(body).encode()
        if data is not None: headers['Content-Type']='application/json'
        return urlopen(Request('http://127.0.0.1:%d/api/'%self.port+path,data=data,headers=headers),timeout=3)

    def test_new_endpoints_keep_authentication_and_camera_dispatch_explicit(self):
        with mock.patch.object(self.controller.live_view,'cameras',return_value=[]) as cameras,\
                mock.patch.object(self.controller.camera_profiles,'apply',return_value={'ok':True}) as apply,\
                mock.patch.object(self.controller.live_view,'stop'):
            with self.assertRaises(HTTPError) as denied:
                self.request('live/cameras',token=False)
            self.assertEqual(denied.exception.code,403);denied.exception.close()
            cameras.assert_not_called()
            with self.request('live/cameras') as response:
                self.assertEqual(json.load(response),{'cameras':[]})
            with self.assertRaises(HTTPError) as denied:
                self.request('camera-profiles/apply',{'token':'plan'},token=False)
            self.assertEqual(denied.exception.code,403);denied.exception.close()
            apply.assert_not_called()
            with self.request('camera-profiles/apply',{'token':'plan'}) as response:
                self.assertTrue(json.load(response)['ok'])
            apply.assert_called_once_with('plan')

    def test_live_stream_and_ping_history_response_use_header_token(self):
        with mock.patch.object(self.controller.live_view,'stream',return_value=(chunk for chunk in (b'init',b'fragment'))) as stream,\
                mock.patch.object(self.controller.camera_ping,'history',return_value={'samples':[]}) as history:
            with self.request('live/stream?session=session1') as response:
                self.assertEqual(response.read(),b'initfragment')
                self.assertIn('blob:',response.headers['Content-Security-Policy'])
            stream.assert_called_once_with('session1')
            with self.request('ping/history?hours=6&camera=cam1') as response:
                self.assertEqual(json.load(response),{'samples':[]})
            history.assert_called_once_with('cam1',6)


if __name__=='__main__':
    unittest.main()
