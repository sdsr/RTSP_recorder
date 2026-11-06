"""Offline camera-profile tests; HTTPS and camera writes are in-memory fakes."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlsplit


SOURCE = Path(__file__).with_name('record.py')
SPEC = importlib.util.spec_from_file_location('camera_profiles_under_test', SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def capability_xml(add_by_index=True):
    parameters = {
        'Profile': '<int min="1" max="10"/>',
        'Resolution': '<enum><entry value="1280x720"/><entry value="3840x2160"/></enum>',
        'EncodingType': '<enum><entry value="H264"/><entry value="MJPEG"/></enum>',
        'FrameRate': '<int min="1" max="30"/>',
        'Bitrate': '<int min="256" max="12000"/>',
        'H264.GOVLength': '<int min="1" max="120"/>',
        'Name': '<string/>', 'H264.Profile': '<string/>',
        'H264.SmartCodecEnable': '<bool/>', 'H264.DynamicFPSEnable': '<bool/>',
        'H264.DynamicGOVEnable': '<bool/>',
    }
    cgis = '<root><cgi name="media"><submenu name="videoprofile"><action name="add/update">'
    cgis += ''.join('<parameter name="{}" request="true"><dataType>{}</dataType></parameter>'.format(k,v)
                    for k,v in parameters.items())
    cgis += ('</action></submenu><submenu name="videoprofilepolicy"><action name="set">'
             '<parameter name="DefaultProfile" request="true"/></action></submenu></cgi></root>')
    attributes = ('<root><group name="System"><category name="Property">'
                  '<attribute name="ModelName" value="TEST-CAMERA"/></category></group>'
                  '<group name="Media"><category name="Support"><channel number="0">'
                  '<attribute name="ProfileAddByIndex" value="{}"/></channel></category>'
                  '<category name="Limit"><channel number="0"><attribute name="MaxProfile" value="10"/>'
                  '<attribute name="MaxResolution" value="3840x2160"/></channel></category></group></root>').format(
                      'True' if add_by_index else 'False')
    return cgis, attributes


class FakeCamera:
    def __init__(self):
        self.profiles = {
            '1': {'Name':'Keep-one','EncodingType':'MJPEG','Resolution':'640x480','FrameRate':'5'},
            '2': {'Name':'H.264','IsFixedProfile':'True','EncodingType':'H264',
                  'Resolution':'3840x2160','FrameRate':'30','Bitrate':'8192','H264.GOVLength':'60',
                  'H264.Profile':'High','H264.SmartCodecEnable':'True'},
            '3': {'Name':'Keep-three','EncodingType':'H265','Resolution':'1920x1080','FrameRate':'20'},
        }
        self.policy = {'Channel.0.DefaultProfile':'2','Channel.0.PreservePolicy':'True'}
        self.cgis, self.attributes = capability_xml()
        self.identity = {'Model':'TEST-CAMERA','SerialNumber':'TEST-SERIAL','FirmwareVersion':'TEST-1'}
        self.calls, self.writes = [], []
        self.fail_write = None
        self.mutate_unrelated = False

    def client(self, url):
        hardware = self
        class Client:
            host = urlsplit(url).hostname
            certificate = 'TEST_CERTIFICATE'
            def get(self, path, params=None):
                params = dict(params or {})
                hardware.calls.append((path,params))
                if path.endswith('/cgis'):
                    return hardware.cgis
                if path.endswith('/attributes'):
                    return hardware.attributes
                if path.endswith('/system.cgi'):
                    return '\n'.join(k+'='+v for k,v in hardware.identity.items())
                if params.get('action') == 'view':
                    if params.get('msubmenu') == 'videoprofilepolicy':
                        return '\n'.join(k+'='+v for k,v in hardware.policy.items())
                    return '\n'.join('Channel.0.Profile.{}.{}={}'.format(n,k,v)
                                     for n,fields in hardware.profiles.items() for k,v in fields.items())
                hardware.writes.append(copy.deepcopy(params))
                if len(hardware.writes) == hardware.fail_write:
                    raise record.CameraProfileError('PRIVATE_ERROR_MUST_NOT_LEAK')
                if params['msubmenu'] == 'videoprofilepolicy':
                    hardware.policy['Channel.0.DefaultProfile'] = params['DefaultProfile']
                else:
                    destination = hardware.profiles.setdefault(params['Profile'],{})
                    destination.update({k:v for k,v in params.items() if k not in ('msubmenu','action','Channel','Profile')})
                    if hardware.mutate_unrelated:
                        hardware.profiles['1']['FrameRate'] = '99'
                return 'Success'
        return Client()


class ProfileFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtsp-profile-test-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'recording.json'
        self.path.write_text('{"fixture":true}\n',encoding='utf-8')
        self.camera = FakeCamera()
        self.other = FakeCamera()
        self.controller = SimpleNamespace(
            config_path=self.path, action_lock=threading.RLock(), closing=False,
            read_config=mock.Mock(return_value=({'fixture':True},'revision-1')),
            state=mock.Mock(return_value={'running':False,'external_running':False,'active_cameras':[],'error':''}),
            settings=mock.Mock(return_value=SimpleNamespace(cameras=[
                record.Camera('cam1','rtsp://admin:PRIVATE_PASSWORD@192.0.2.1/profile2/media.smp'),
                record.Camera('cam2','rtsp://admin:PRIVATE_PASSWORD@192.0.2.2/profile2/media.smp')]))
        )
        self.factory = mock.Mock(side_effect=lambda url: (self.camera if urlsplit(url).hostname=='192.0.2.1' else self.other).client(url))
        self.manager = record.CameraProfileManager(self.controller,self.factory)

    def plan(self):
        return self.manager.plan('cam1')

    def journal(self):
        paths = list(self.path.parent.glob('.camera-profile-backups/*/backup-and-result.json'))
        self.assertEqual(len(paths),1)
        return paths[0],json.loads(paths[0].read_text(encoding='utf-8'))


class ProfileManagerTests(ProfileFixture):
    def test_read_and_preview_select_only_saved_target_and_never_write(self):
        snapshot = self.manager.read('cam2')
        plan = self.manager.plan('cam1')
        self.assertTrue(snapshot['read_only'])
        self.assertEqual(snapshot['host'],'192.0.2.2')
        self.assertEqual(plan['host'],'192.0.2.1')
        self.assertEqual([c['profile'] for c in plan['changes']],['4','2'])
        self.assertEqual(plan['targets']['4']['Resolution'],'3840x2160')
        self.assertEqual(plan['targets']['2']['Resolution'],'1280x720')
        self.assertEqual(plan['targets']['2']['FrameRate'],'15')
        self.assertFalse(self.camera.writes)
        self.assertFalse(self.other.writes)
        self.assertEqual(self.path.read_text(),'{"fixture":true}\n')
        self.assertNotIn('PRIVATE_PASSWORD',json.dumps([snapshot,plan]))
        with self.assertRaises(record.CameraProfileError):
            self.manager.plan('not-saved')
        self.assertEqual(self.factory.call_count,2)

    def test_apply_only_changes_profiles_two_four_and_default_with_private_backup(self):
        original = copy.deepcopy(self.camera.profiles)
        plan = self.plan()
        result = self.manager.apply(plan['token'])
        self.assertTrue(result['ok'])
        self.assertEqual([w['action'] for w in self.camera.writes],['add','update','set'])
        self.assertEqual([w.get('Profile') for w in self.camera.writes],['4','2',None])
        self.assertTrue(all(w['Channel']=='0' for w in self.camera.writes))
        self.assertEqual(self.camera.profiles['1'],original['1'])
        self.assertEqual(self.camera.profiles['3'],original['3'])
        self.assertEqual(self.camera.policy,{'Channel.0.DefaultProfile':'4','Channel.0.PreservePolicy':'True'})
        self.assertFalse(self.other.writes)
        path,journal = self.journal()
        self.assertTrue(journal['complete'])
        self.assertEqual(journal['before']['profiles'],original)
        self.assertTrue(all(step['verified'] for step in journal['steps']))
        self.assertNotIn('PRIVATE_PASSWORD',path.read_text())
        if sys.platform=='linux':
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777,0o700)
        with self.assertRaises(record.CameraProfileError):
            self.manager.apply(plan['token'])
        self.assertEqual(len(self.camera.writes),3)

    def test_profile_plan_can_leave_default_policy_unchanged(self):
        plan = self.manager.plan('cam1',default_profile4=False)
        self.manager.apply(plan['token'])
        self.assertEqual(self.camera.policy['Channel.0.DefaultProfile'],'2')
        self.assertEqual(len(self.camera.writes),2)

    def test_running_external_active_and_error_states_block_before_camera_writes(self):
        for state in ({'running':True},{'external_running':True},{'active_cameras':['cam1']},{'error':'bad configuration'}):
            with self.subTest(state=state):
                token = self.plan()['token']
                self.controller.state.return_value = state
                with self.assertRaises(record.CameraProfileError):
                    self.manager.apply(token)
                self.assertFalse(self.camera.writes)
        self.assertFalse((self.path.parent/'.camera-profile-backups').exists())

    def test_closing_controller_rejects_queued_camera_apply(self):
        token = self.plan()['token']
        before = len(self.camera.calls)
        self.controller.closing = True
        with self.assertRaisesRegex(record.CameraProfileError,'종료'):
            self.manager.apply(token)
        self.assertEqual(len(self.camera.calls),before)
        self.assertFalse(self.camera.writes)

    def test_plan_expiry_and_changed_config_or_camera_identity_are_rejected(self):
        token = self.plan()['token']
        self.manager.plans[token]['created'] -= 601
        with self.assertRaisesRegex(record.CameraProfileError,'만료'):
            self.manager.apply(token)
        token = self.plan()['token']
        self.controller.read_config.return_value = ({'fixture':True},'revision-2')
        with self.assertRaisesRegex(record.CameraProfileError,'설정이 변경'):
            self.manager.apply(token)
        token = self.plan()['token']
        self.camera.identity['SerialNumber'] = 'DIFFERENT_CAMERA'
        with self.assertRaisesRegex(record.CameraProfileError,'장치가 변경'):
            self.manager.apply(token)
        self.assertFalse(self.camera.writes)

    def test_changed_camera_url_is_rejected_even_if_revision_were_unchanged(self):
        token = self.plan()['token']
        self.controller.settings.return_value.cameras[0] = record.Camera('cam1','rtsp://admin:PRIVATE_PASSWORD@192.0.2.3/profile2/media.smp')
        with self.assertRaisesRegex(record.CameraProfileError,'설정이 변경'):
            self.manager.apply(token)
        self.assertFalse(self.camera.writes)

    def test_partial_failure_keeps_backup_stops_without_rollback_or_retry(self):
        token = self.plan()['token']
        self.camera.fail_write = 2
        with self.assertRaises(record.CameraProfileError) as caught:
            self.manager.apply(token)
        self.assertIn('일부 단계',str(caught.exception))
        self.assertNotIn('PRIVATE_ERROR',str(caught.exception))
        path,journal = self.journal()
        self.assertIn(str(path),str(caught.exception))
        self.assertFalse(journal['complete'])
        self.assertTrue(journal['steps'][0]['verified'])
        self.assertFalse(journal['steps'][1]['verified'])
        self.assertEqual(len(self.camera.writes),2)
        self.assertIn('4',self.camera.profiles)
        self.assertEqual(self.camera.profiles['2']['Resolution'],'3840x2160')
        self.assertEqual(self.camera.policy['Channel.0.DefaultProfile'],'2')

    def test_camera_mutating_an_unrelated_profile_stops_subsequent_steps(self):
        token = self.plan()['token']
        self.camera.mutate_unrelated = True
        with self.assertRaises(record.CameraProfileError):
            self.manager.apply(token)
        self.assertEqual(len(self.camera.writes),1)
        _,journal = self.journal()
        self.assertFalse(journal['complete'])
        self.assertFalse(journal['steps'][0]['verified'])

    def test_failure_to_journal_after_write_still_reports_partial_camera_change(self):
        token = self.plan()['token']
        real_write = record._camera_private_json
        calls = []
        def failing_write(path,value):
            calls.append(1)
            if len(calls)>=3:
                raise OSError('disk full')
            return real_write(path,value)
        with mock.patch.object(record,'_camera_private_json',side_effect=failing_write):
            with self.assertRaisesRegex(record.CameraProfileError,'일부 단계'):
                self.manager.apply(token)
        self.assertEqual(len(self.camera.writes),1)
        _,journal = self.journal()
        self.assertEqual(journal['steps'][0]['profile'],'4')

    def test_unsupported_capabilities_and_xml_do_not_create_a_write_plan(self):
        self.camera.cgis = self.camera.cgis.replace('3840x2160','1920x1080')
        with self.assertRaises(record.CameraProfileError):
            self.plan()
        self.assertFalse(self.camera.writes)
        self.assertEqual(self.manager.plans,{})
        for xml in ('<!DOCTYPE a><a/>','<!ENTITY x "x"><a/>','<broken>'):
            with self.subTest(xml=xml),self.assertRaises(record.CameraProfileError):
                record._camera_xml(xml)

    def test_idempotent_completed_camera_requires_no_more_writes(self):
        self.manager.apply(self.plan()['token'])
        self.camera.writes.clear()
        plan = self.plan()
        self.assertEqual(plan['changes'],[])
        self.manager.apply(plan['token'])
        self.assertEqual(self.camera.writes,[])


class DigestClientTests(unittest.TestCase):
    def test_rfc_md5_digest_vector_and_sha256_do_not_expose_password(self):
        challenge = ('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                     'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
        header = record._camera_digest_header(challenge,'Mufasa','Circle Of Life','/dir/index.html',cnonce='0a4f113b')
        self.assertIn('response="6629fae49393a05397450978507c4ef1"',header)
        self.assertNotIn('Circle Of Life',header)
        h = lambda value:hashlib.sha256(value.encode()).hexdigest()
        header = record._camera_digest_header('Digest realm="CAM", nonce="NONCE", algorithm=SHA-256, qop="auth"',
                                             'operator','PRIVATE_SECRET','/view',nc=2,cnonce='CNONCE')
        expected = h(h('operator:CAM:PRIVATE_SECRET')+':NONCE:00000002:CNONCE:auth:'+h('GET:/view'))
        self.assertIn('response="'+expected+'"',header)
        self.assertNotIn('PRIVATE_SECRET',header)

    def test_bad_digest_headers_and_non_saved_network_targets_are_rejected(self):
        for challenge in ('Basic realm="CAM"','Digest realm="CAM",nonce="N",algorithm=UNSUPPORTED',
                          'Digest realm="CAM",nonce="N",qop="auth-int"'):
            with self.subTest(challenge=challenge),self.assertRaises(record.CameraProfileError):
                record._camera_digest_header(challenge,'user','secret','/view')
        for url in ('rtsp://user:secret@localhost/live','http://user:secret@192.0.2.1/live',
                    'rtsp://user:secret@127.0.0.1/live','rtsp://user:secret@0.0.0.0/live',
                    'rtsp://user:secret@224.0.0.1/live','rtsp://192.0.2.1/live'):
            with self.subTest(url=url),self.assertRaises(record.CameraProfileError):
                record.WisenetProfileClient(url)

    def connection(self,status=200,body=b'OK',certificate=b'TEST_CERT',challenge=''):
        connection = mock.Mock()
        connection.sock.getpeercert.return_value = certificate
        response = mock.Mock(status=status)
        response.read.return_value = body
        response.getheader.return_value = challenge
        connection.getresponse.return_value = response
        return connection

    def test_https_digest_challenge_once_and_certificate_change_stops_before_auth(self):
        first = self.connection(401,challenge='Digest realm="CAM",nonce="N",algorithm=SHA-256,qop="auth"')
        second = self.connection()
        with mock.patch.object(record._cp_http,'HTTPSConnection',side_effect=[first,second]) as factory:
            client = record.WisenetProfileClient('rtsp://admin:PRIVATE_SECRET@192.0.2.1/live')
            self.assertEqual(client.get('/stw-cgi/media.cgi',{'action':'view'}),'OK')
            self.assertEqual(factory.call_count,2)
            self.assertTrue(all(call.kwargs['port']==443 for call in factory.call_args_list))
            self.assertNotIn('Authorization',first.request.call_args.kwargs['headers'])
            authorization = second.request.call_args.kwargs['headers']['Authorization']
            self.assertIn('algorithm=SHA-256',authorization)
            self.assertNotIn('PRIVATE_SECRET',authorization)
            self.assertTrue(first.close.called and second.close.called)
        changed = self.connection(certificate=b'DIFFERENT_CERT')
        with mock.patch.object(record._cp_http,'HTTPSConnection',return_value=changed), self.assertRaisesRegex(record.CameraProfileError,'인증서'):
            client.get('/stw-cgi/media.cgi',{'action':'view'})
        changed.request.assert_not_called()

    def test_auth_failure_redirect_and_oversized_response_never_retry_writes(self):
        challenge='Digest realm="CAM",nonce="N",qop="auth"'
        connections=[self.connection(401,challenge=challenge),self.connection(401,challenge=challenge)]
        with mock.patch.object(record._cp_http,'HTTPSConnection',side_effect=connections) as factory:
            client=record.WisenetProfileClient('rtsp://admin:PRIVATE_SECRET@192.0.2.1/live')
            with self.assertRaisesRegex(record.CameraProfileError,'인증이 거부'):
                client.get('/stw-cgi/media.cgi',{'action':'view'})
            self.assertEqual(factory.call_count,2)
        for response in (self.connection(302),self.connection(body=b'x'*(4*1024*1024+1))):
            with mock.patch.object(record._cp_http,'HTTPSConnection',return_value=response) as factory:
                client=record.WisenetProfileClient('rtsp://admin:PRIVATE_SECRET@192.0.2.1/live')
                with self.assertRaises(record.CameraProfileError):
                    client.get('/stw-cgi/media.cgi',{'action':'view'})
                self.assertEqual(factory.call_count,1)


if __name__=='__main__':
    unittest.main()
