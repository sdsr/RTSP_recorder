"""Camera administration with fake devices and loop-free local settings only."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

SOURCE = Path(__file__).with_name('record.py')
SPEC = importlib.util.spec_from_file_location('camera_admin_under_test', SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


class FakeHardware:
    def __init__(self, host='192.0.2.10', username='owner', password='Old!Secret7', mac='00:11:22:33:44:55'):
        self.host, self.username, self.password, self.mac = host, username, password, mac
        self.certificate = 'a' * 64
        self.identity = {'Model':'PG-ABS08041402', 'SerialNumber':'TEST-SERIAL-' + mac[-2:],
                         'ConnectedMACAddress':mac, 'FirmwareVersion':'TEST-FIRMWARE'}
        self.network = {'ip':host, 'netmask':'255.255.255.0', 'gateway':'192.0.2.1', 'mode':'Manual'}
        self.capabilities = {'network':True, 'password':True, 'can_change_username':True,
            'current_password_verification':True, 'new_password_policy':False,
            'password_maximum':64, 'prefix_min':8, 'prefix_max':30, 'schema_sha256':'b'*64}
        self.writes = []
        self.disconnect_after_write = False
        self.reject_auth_write = False
        self.unexpected_change = False
        self.after_write = None

    def snapshot(self, client):
        if (client.host, client.username, client.password) != (self.host, self.username, self.password):
            raise record._CameraAdminAuthError('TEST authentication denied')
        if client.certificate is not None and client.certificate != self.certificate:
            raise record.CameraAdminError('TEST certificate changed')
        client.certificate = self.certificate
        return copy.deepcopy({'identity':self.identity,'network':self.network,
            'user':{'index':0,'fields':{'UserID':self.username,'AdminAccess':'True','VideoAccess':'True'}},
            'capabilities':self.capabilities,'interface_name':'NetworkInterface1',
            'certificate_sha256':self.certificate})


class FakeClient:
    def __init__(self, devices, url):
        parsed = urlsplit(url)
        self.devices, self.host = devices, parsed.hostname
        self.username, self.password = unquote(parsed.username or ''), unquote(parsed.password or '')
        self.certificate = None
        self.allow_writes = self.write_attempted = False

    def hardware(self):
        return next(device for device in self.devices if device.host == self.host)

    def get(self, path, params=None):
        assert params == {'msubmenu':'videoprofile','action':'view'}
        self.hardware().snapshot(self)
        return 'Channel.0.Profile.2.EncodingType=H264\nChannel.0.Profile.4.EncodingType=H264'

    def _write(self, action, payload):
        assert self.allow_writes
        device = self.hardware()
        if device.reject_auth_write:
            raise record._CameraAdminAuthError('TEST authentication denied')
        self.write_attempted = True
        device.writes.append((action,copy.deepcopy(payload)))
        if action == 'network':
            device.network = copy.deepcopy(payload)
            device.host = payload['ip']
        else:
            device.username, device.password = payload['username'], payload['password']
        if device.unexpected_change:
            device.identity['SerialNumber'] = 'UNEXPECTED_DEVICE'
        if device.after_write:
            device.after_write()
        if device.disconnect_after_write:
            raise record.CameraAdminError('TEST connection closed after write')

    def update_network(self, desired, interface):
        assert interface == 'NetworkInterface1'
        self._write('network', desired)

    def update_password(self, snapshot, username, password):
        self._write('password', {'username':username,'password':password})


class AdminFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtsp-camera-admin-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'recording.json'
        self.data = json.loads(record.render_json_config())
        self.data.update(output_dir=str(self.path.parent/'videos'), intersection_name='KEEP_LABEL',
                         time_ranges=['08:00-09:00'], recording_dates=['2026-11-17'],
                         cameras=[{'name':'cam1','url':'rtsp://owner:Old!Secret7@192.0.2.10:554/profile2/media.smp?session=KEPT'},
                                  {'name':'cam2','url':'rtsp://other:OTHER_TEST@192.0.2.20/profile4/media.smp'}])
        self.write(self.data)
        self.controller = record.RecorderUiController(self.path)
        self.addCleanup(self.controller.close)
        self.primary = FakeHardware()
        self.devices = [self.primary,FakeHardware('192.0.2.20','other','OTHER_TEST','00:11:22:33:44:66')]
        self.factory = mock.Mock(side_effect=lambda url:FakeClient(self.devices,url))
        self.manager = record.CameraAdminManager(self.controller,self.factory)
        self.manager._snapshot = lambda client:client.hardware().snapshot(client)
        self.manager._route = mock.Mock(return_value={'parent':'enp2s0','dev':'enp2s0','source':'192.0.2.5'})
        self.manager._probe = mock.Mock(side_effect=lambda ip, route:{'mac':next(d.mac for d in self.devices if d.host==ip)})
        self.manager._available = mock.Mock()
        self.addCleanup(self.manager.close)
        if sys.platform != 'linux':
            # Native Linux permission tests exercise the actual exclusive writer.
            def portable_private(path, data):
                with Path(path).open('xb') as handle:handle.write(data)
            patcher = mock.patch.object(record,'_ca_private_write',side_effect=portable_private)
            patcher.start();self.addCleanup(patcher.stop)

    def write(self, data):
        self.path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')

    def plan(self, action='network', **values):
        payload = {'camera':'cam1','action':action,'expected_host':self.primary.host,
                   'expected_mac':self.primary.mac,'expected_cert_sha256':self.primary.certificate}
        if action == 'network':
            payload.update(ip='192.0.2.11',netmask='255.255.255.0',gateway='192.0.2.1')
        else:
            payload.update(new_username='newuser',new_password='New!Secret8',confirm_password='New!Secret8')
        payload.update(values)
        return self.manager.plan(payload)

    def apply(self, plan, **extra):
        payload = {'plan_id':plan['plan_id'],'confirm':True}
        if plan['action'] == 'password':
            payload.update(new_password='New!Secret8',confirm_password='New!Secret8')
        payload.update(extra)
        return self.manager.apply(payload)


class CameraAdminManagerTests(AdminFixture):
    def test_construct_inspect_and_plan_do_not_change_device_or_settings(self):
        original = self.path.read_bytes()
        self.assertEqual(self.factory.call_count,0)
        response = self.manager.inspect({'camera':'cam1'})
        plan = self.plan()
        self.assertEqual(response['host'],'192.0.2.10')
        self.assertEqual(plan['after']['ip'],'192.0.2.11')
        self.assertEqual(self.primary.writes,[])
        self.assertEqual(self.path.read_bytes(),original)
        self.assertNotIn('Old!Secret7',json.dumps(response)+json.dumps(plan))
        self.assertFalse((self.path.parent/'.camera-admin-backups').exists())

    def test_network_apply_changes_one_camera_and_preserves_schedule_labels_and_other_camera(self):
        original = self.path.read_bytes()
        result = self.apply(self.plan())
        self.assertEqual(result['status'],'applied')
        self.assertTrue(result['camera_change_verified'])
        self.assertEqual(len(self.primary.writes),1)
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['cameras'][0]['url'],'rtsp://owner:Old!Secret7@192.0.2.11:554/profile2/media.smp?session=KEPT')
        for field in ('time_ranges','recording_dates','intersection_name','output_dir','recording_mode'):
            self.assertEqual(saved[field],self.data[field])
        self.assertEqual(saved['cameras'][1],self.data['cameras'][1])
        self.assertEqual(self.devices[1].writes,[])
        self.assertIsNone(self.controller.process)
        backup = Path(result['backup_path'])
        self.assertEqual((backup/'recording_before.json').read_bytes(),original)
        self.assertNotIn('Old!Secret7',(backup/'before.json').read_text(encoding='utf-8'))
        self.assertNotIn('Old!Secret7',json.dumps(result))
        if sys.platform == 'linux':
            self.assertEqual(backup.stat().st_mode & 0o777,0o700)
            self.assertEqual((backup/'recording_before.json').stat().st_mode & 0o777,0o600)

    def test_password_plan_retains_only_hash_and_apply_preserves_other_fields(self):
        plan = self.plan('password')
        self.assertNotIn('New!Secret8',repr(self.manager._plans))
        self.assertNotIn('New!Secret8',json.dumps(plan))
        result = self.apply(plan)
        self.assertEqual(result['status'],'applied')
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        parsed = urlsplit(saved['cameras'][0]['url'])
        self.assertEqual((unquote(parsed.username),unquote(parsed.password),parsed.hostname),('newuser','New!Secret8','192.0.2.10'))
        self.assertEqual(parsed.query,'session=KEPT')
        self.assertEqual(saved['cameras'][1],self.data['cameras'][1])
        self.assertNotIn('New!Secret8',(Path(result['backup_path'])/'result.json').read_text(encoding='utf-8'))

    def test_confirm_revision_identity_and_password_mismatch_block_writes(self):
        for scenario in ('confirm','revision','identity','password'):
            with self.subTest(scenario=scenario):
                self.write(self.data)
                self.primary.identity['SerialNumber'] = 'TEST-SERIAL-55'
                plan = self.plan('password' if scenario=='password' else 'network')
                extra = {}
                if scenario=='confirm':extra['confirm']=False
                if scenario=='revision':self.write(dict(self.data,intersection_name='CHANGED'))
                if scenario=='identity':self.primary.identity['SerialNumber']='REPLACEMENT'
                if scenario=='password':extra['new_password']='Other!Secret9'
                with self.assertRaises(record.CameraAdminError):self.apply(plan,**extra)
                self.assertEqual(self.primary.writes,[])

    def test_busy_preview_capture_external_recorder_and_closed_service_block_apply(self):
        states = ({'running':True},{'external_running':True},{'active_cameras':['cam1']},{'error':'unavailable'})
        for state in states:
            plan = self.plan()
            with self.subTest(state=state),mock.patch.object(self.controller,'state',return_value=state),self.assertRaises(record.CameraAdminError):
                self.apply(plan)
        self.controller.live_view._session = object()
        try:
            with self.assertRaises(record.CameraAdminError):self.apply(self.plan())
        finally:self.controller.live_view._session = None
        lock=self.controller.camera_snapshots._capture_lock
        lock.acquire()
        try:
            with self.assertRaises(record.CameraAdminError):self.apply(self.plan())
        finally:lock.release()
        self.manager.close()
        with self.assertRaises(record.CameraAdminError):self.manager.register({})
        self.assertEqual(self.primary.writes,[])

    def test_plan_expiry_and_single_use_reject_duplicate_device_writes(self):
        plan=self.plan()
        self.manager._plans[plan['plan_id']]['created']-=301
        with self.assertRaises(record.CameraAdminError):self.apply(plan)
        plan=self.plan();self.apply(plan)
        with self.assertRaises(record.CameraAdminError):self.apply(plan)
        self.assertEqual(len(self.primary.writes),1)

    def test_route_and_duplicate_address_checks_fail_before_any_write(self):
        with self.assertRaises(record.CameraAdminError):self.plan(ip='192.0.2.20')
        self.manager._available.side_effect=record.CameraAdminError('duplicate address')
        with self.assertRaises(record.CameraAdminError):self.plan()
        self.manager._available.side_effect=None
        self.manager._route.side_effect=lambda ip: {'parent':'enp2s1' if ip.endswith('.11') else 'enp2s0',
            'dev':'enp2s1' if ip.endswith('.11') else 'enp2s0','source':'192.0.2.5'}
        with self.assertRaises(record.CameraAdminError):self.plan()
        self.assertEqual(self.primary.writes,[])

    def test_write_disconnect_is_verified_once_without_retry(self):
        self.primary.disconnect_after_write=True
        result=self.apply(self.plan())
        self.assertEqual(result['status'],'applied')
        self.assertEqual(len(self.primary.writes),1)

    def test_failed_verification_keeps_old_config_and_never_rolls_back_hardware(self):
        original=self.path.read_bytes()
        self.primary.unexpected_change=True
        result=self.apply(self.plan())
        self.assertEqual(result['status'],'unverified')
        self.assertFalse(result['recorder_updated'])
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(self.primary.host,'192.0.2.11')
        self.assertEqual(len(self.primary.writes),1)

    def test_verified_camera_but_local_save_failure_is_reported_as_pending(self):
        original=self.path.read_bytes()
        plan=self.plan()
        with mock.patch.object(record,'atomic_private_text',side_effect=OSError('disk full')):
            result=self.apply(plan)
        self.assertEqual(result['status'],'camera_changed_local_pending')
        self.assertTrue(result['camera_change_verified'])
        self.assertFalse(result['recorder_updated'])
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(len(self.primary.writes),1)

    def test_concurrent_config_change_after_device_write_is_not_overwritten(self):
        replacement=dict(self.data,intersection_name='CONCURRENT_SETTING')
        self.primary.after_write=lambda:self.write(replacement)
        result=self.apply(self.plan())
        self.assertEqual(result['status'],'camera_changed_local_pending')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),replacement)
        self.assertEqual(len(self.primary.writes),1)

    def test_backup_failure_prevents_device_write(self):
        plan=self.plan()
        with mock.patch.object(record,'_ca_private_write',side_effect=OSError('disk full')),self.assertRaises(OSError):
            self.apply(plan)
        self.assertEqual(self.primary.writes,[])

    def test_register_first_camera_requires_explicit_account_and_never_changes_hardware(self):
        self.write(dict(self.data,cameras=[]))
        payload={'ip':self.primary.host,'profile':'4','isolated':True,
                 'expected_host':self.primary.host,'expected_mac':self.primary.mac,
                 'expected_cert_sha256':self.primary.certificate}
        with self.assertRaises(record.CameraAdminError):self.manager.register(payload)
        self.factory.assert_not_called()
        result=self.manager.register(dict(payload,username='owner',password='Old!Secret7'))
        self.assertEqual(result['status'],'registered')
        saved=json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(len(saved['cameras']),1)
        self.assertEqual(urlsplit(saved['cameras'][0]['url']).username,'owner')
        self.assertEqual(urlsplit(saved['cameras'][0]['url']).path,'/profile4/media.smp')
        self.assertEqual(self.primary.writes,[])
        self.assertIsNone(self.controller.process)
        self.assertFalse(self.controller.state()['setup_required'])

    def test_registration_name_collision_adds_suffix_without_changing_previous_entry(self):
        data=copy.deepcopy(self.data)
        data['cameras'][1]['name']='camera_192_0_2_30'
        self.write(data)
        extra=FakeHardware('192.0.2.30','added','Added!Test9','00:11:22:33:44:77')
        self.devices.append(extra)
        result=self.manager.register({'ip':extra.host,'username':extra.username,'password':extra.password,
            'profile':'2','isolated':True,'expected_mac':extra.mac,'expected_cert_sha256':extra.certificate})
        self.assertEqual(result['camera'],'camera_192_0_2_30_2')
        saved=json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['cameras'][:2],data['cameras'])
        self.assertEqual(extra.writes,[])

    def test_malformed_payloads_are_safe_errors_without_contacting_camera(self):
        for payload in (None,[],42,'string'):
            for method in (self.manager.inspect,self.manager.plan,self.manager.apply,self.manager.register):
                with self.subTest(payload=payload,method=method.__name__),self.assertRaises(record.CameraAdminError):method(payload)
        with self.assertRaises(record.CameraAdminError):self.manager.apply({'confirm':True,'plan_id':[]})
        self.factory.assert_not_called()

    def test_cache_invalidation_uses_the_controller_settings_lock(self):
        events=[]
        class Lock:
            def __enter__(self):events.append('enter')
            def __exit__(self,*args):events.append('exit')
        self.controller.settings_lock=Lock()
        self.controller.cached_settings='OLD';self.controller.cached_revision='OLD'
        self.manager._invalidate_settings()
        self.assertEqual(events,['enter','exit'])
        self.assertIsNone(self.controller.cached_settings)
        self.assertIsNone(self.controller.cached_revision)


class CameraAdminClientTests(unittest.TestCase):
    def client(self):
        client=record.CameraAdminClient('rtsp://owner:Old!Secret7@192.0.2.10/profile2/media.smp')
        client._route=mock.Mock(return_value={'source':'192.0.2.5'})
        return client

    @staticmethod
    def connection(status=200,body=b'OK',challenge=''):
        connection=mock.Mock()
        connection.sock.getpeercert.return_value=b'CERTIFICATE'
        response=connection.getresponse.return_value
        response.status=status;response.read.return_value=body;response.getheader.return_value=challenge
        return connection

    def test_client_refuses_write_by_default_and_password_query_even_when_enabled(self):
        client=self.client()
        with mock.patch.object(record._ca_http,'HTTPSConnection') as connect:
            with self.assertRaises(record.CameraAdminError):
                client.update_network({'ip':'192.0.2.11','netmask':'255.255.255.0','gateway':'192.0.2.1'},'NetworkInterface1')
            client.allow_writes=True
            with self.assertRaises(record.CameraAdminError):
                client.request('/stw-cgi/security.cgi',{'msubmenu':'users','action':'update','Password':'PRIVATE'},method='POST',body=b'encrypted')
            connect.assert_not_called()

    def test_close_error_does_not_mask_response_or_prevent_subsequent_verification(self):
        client=self.client();connection=self.connection()
        connection.close.side_effect=OSError('closed by peer')
        with mock.patch.object(record._ca_http,'HTTPSConnection',return_value=connection):
            result=client.get('/stw-cgi/system.cgi',{'msubmenu':'deviceinfo','action':'view'})
        self.assertEqual(result,'OK')

    def test_certificate_change_is_rejected_before_sending_request(self):
        client=self.client();client.certificate='b'*64
        connection=self.connection()
        with mock.patch.object(record._ca_http,'HTTPSConnection',return_value=connection),self.assertRaises(record.CameraAdminError):
            client.get('/stw-cgi/system.cgi',{'msubmenu':'deviceinfo','action':'view'})
        connection.request.assert_not_called()

    def test_digest_auth_challenge_retries_once_and_never_redirects(self):
        client=self.client()
        first=self.connection(401,b'', 'Digest realm="camera",nonce="nonce",algorithm=SHA-256,qop="auth"')
        second=self.connection()
        with mock.patch.object(record._ca_http,'HTTPSConnection',side_effect=[first,second]) as connect:
            self.assertEqual(client.get('/stw-cgi/system.cgi',{'msubmenu':'deviceinfo','action':'view'}),'OK')
        self.assertEqual(connect.call_count,2)
        headers=second.request.call_args.kwargs['headers']
        self.assertIn('algorithm=SHA-256',headers['Authorization'])
        self.assertNotIn('Old!Secret7',headers['Authorization'])
        redirected=self.connection(302,b'PRIVATE_RESPONSE')
        with mock.patch.object(record._ca_http,'HTTPSConnection',return_value=redirected) as connect,self.assertRaises(record.CameraAdminError) as error:
            client.get('/stw-cgi/system.cgi',{'msubmenu':'deviceinfo','action':'view'})
        self.assertEqual(connect.call_count,1)
        self.assertNotIn('PRIVATE_RESPONSE',str(error.exception))

    def test_authenticated_401_is_not_retried(self):
        client=self.client();client.challenge='Digest realm="camera",nonce="n",qop="auth"'
        connection=self.connection(401,b'PRIVATE_BODY')
        with mock.patch.object(record._ca_http,'HTTPSConnection',return_value=connection) as connect,self.assertRaises(record._CameraAdminAuthError):
            client.get('/stw-cgi/system.cgi',{'msubmenu':'deviceinfo','action':'view'})
        self.assertEqual(connect.call_count,1)

    def test_password_is_encrypted_and_sent_in_body_not_uri(self):
        client=self.client()
        snapshot={'capabilities':{'current_password_verification':True},'user':{'index':0}}
        with mock.patch.object(client,'public_key',return_value='PUBLIC_KEY'),\
                mock.patch.object(record,'_camera_admin_encrypt',return_value='CIPHERTEXT') as encrypt,\
                mock.patch.object(client,'request',return_value='OK') as request:
            client.update_password(snapshot,'newuser','New!Secret8')
        raw=json.loads(encrypt.call_args.args[1])
        self.assertEqual(raw,{'CurrentPassword':'Old!Secret7','NewPassword':'New!Secret8'})
        self.assertNotIn('Secret',repr(request.call_args))
        self.assertEqual(request.call_args.kwargs['body'],b'CIPHERTEXT')
        self.assertEqual(request.call_args.kwargs['method'],'POST')

    def test_url_retarget_keeps_path_query_port_and_encodes_new_credentials(self):
        url='rtsp://old:p%40ss@192.0.2.10:8554/profile4/media.smp?transport=keep'
        self.assertEqual(record._ca_url(url,host='192.0.2.11'),'rtsp://old:p%40ss@192.0.2.11:8554/profile4/media.smp?transport=keep')
        value=record._ca_url(url,username='newuser',password='new@pass:#')
        self.assertEqual(value,'rtsp://newuser:new%40pass%3A%23@192.0.2.10:8554/profile4/media.smp?transport=keep')


class CameraAdminSnapshotTests(unittest.TestCase):
    def client(self, model='PG-ABS08041402', network_mac='00:11:22:33:44:55'):
        client=SimpleNamespace(host='192.0.2.10',username='owner',certificate='a'*64)
        network_parameters=''.join('<parameter name="'+name+'" request="true"/>' for name in
                                  ('IPv4Type','IPv4Address','IPv4SubnetMask','IPv4Gateway'))
        user_parameters=''.join('<parameter name="'+name+'" request="true"/>' for name in
                               ('Index','UserID','IsPasswordEncrypted'))
        cgis=('<root><cgi name="network"><submenu name="interface"><action name="set">'+network_parameters+
              '</action></submenu></cgi><cgi name="security"><submenu name="users"><action name="add/update">'+
              user_parameters+'<parameter name="Password" request="true"><dataType><string maxlen="64"/></dataType></parameter>'+
              '</action></submenu></cgi></root>')
        attrs=('<root><group name="Security"><category name="Support"><attribute name="AdminIDChangeable" value="True"/>'+
               '<attribute name="CurrentPasswordVerification" value="Mandatory"/></category></group></root>')
        def get(path,params=None):
            if path.endswith('/cgis'):return cgis
            if path.endswith('/attributes'):return attrs
            if path.endswith('/system.cgi'):
                return 'Model='+model+'\nSerialNumber=TEST\nConnectedMACAddress=00:11:22:33:44:55\nFirmwareVersion=TEST'
            if path.endswith('/network.cgi'):
                return ('MACAddress='+network_mac+'\nInterfaceName=NetworkInterface1\nIPv4Address=192.0.2.10\n'+
                        'IPv4SubnetMask=255.255.255.0\nIPv4Gateway=192.0.2.1\nIPv4Type=Manual')
            return 'Users.Index=UserID/Password/AdminAccess/VideoAccess\nUsers.0=owner/PRIVATE_FROM_CAMERA/True/True'
        client.get=mock.Mock(side_effect=get)
        return client

    def test_snapshot_checks_supported_identity_and_removes_password_field(self):
        snapshot=record._ca_snapshot(self.client())
        self.assertTrue(snapshot['capabilities']['network'])
        self.assertTrue(snapshot['capabilities']['password'])
        self.assertTrue(snapshot['capabilities']['current_password_verification'])
        self.assertNotIn('PRIVATE_FROM_CAMERA',json.dumps(snapshot))

    def test_unknown_model_or_mismatched_interface_identity_is_rejected(self):
        for client in (self.client(model='UNKNOWN'),self.client(network_mac='00:11:22:33:44:66')):
            with self.assertRaises(record.CameraAdminError):record._ca_snapshot(client)

    def test_admin_index_and_rights_must_be_exact(self):
        for raw in ('Users.Index=UserID/AdminAccess\nUsers.1=owner/True',
                    'Users.Index=UserID/AdminAccess\nUsers.0=owner/False',
                    'Users.Index=UserID/AdminAccess\nUsers.0=owner/True\nUsers.1=owner/True'):
            with self.subTest(raw=raw),self.assertRaises(record.CameraAdminError):record._ca_user(raw,'owner')


if __name__=='__main__':unittest.main()
