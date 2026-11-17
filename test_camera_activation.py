"""Factory activation checks with mocked HTTPS, ARP and OpenSSL; no device I/O."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE=Path(__file__).with_name('components')/'camera_activation.py'
SPEC=importlib.util.spec_from_file_location('camera_activation_under_test',SOURCE)
activation=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(activation)
IP='192.0.2.77'
MAC='02:11:22:33:44:55'
ROUTE={'dev':'enp2s0','source':'192.0.2.10','parent':'enp2s0','local_mac':'02:aa:bb:cc:dd:ee'}
CERT='a'*64
PUBLIC_KEY='-----BEGIN PUBLIC KEY-----\n'+'A'*256+'\n-----END PUBLIC KEY-----\n'
PASSWORD='Safer$908Yx'


class AddressAndCryptoTests(unittest.TestCase):
    def test_only_real_unicast_ipv4_and_mac_values_are_accepted(self):
        self.assertEqual(activation._cai_ip(IP),IP)
        self.assertEqual(activation._cai_mac(MAC.upper()),MAC)
        for value in ('127.0.0.1','0.0.0.0','224.0.0.1','255.255.255.255','camera.invalid','192.0.2.1;id',None):
            with self.subTest(value=value),self.assertRaises(activation.CameraActivationError):activation._cai_ip(value)
        for value in ('00:00:00:00:00:00','01:00:00:00:00:01','xx',None):
            with self.subTest(value=value),self.assertRaises(activation.CameraActivationError):activation._cai_mac(value)

    def route_data(self):
        return ([{'dev':'enp2s0','prefsrc':ROUTE['source']}],
                [{'ifindex':2,'ifname':'enp2s0','flags':['UP'],'address':ROUTE['local_mac']}],
                [{'ifname':'enp2s0','addr_info':[{'family':'inet','local':ROUTE['source'],'prefixlen':24}]}])

    def test_route_requires_existing_unique_source_and_enp2_direct_path(self):
        route,links,addresses=self.route_data()
        with mock.patch.object(activation,'_cai_ip_json',side_effect=[route,links,addresses]) as query:
            self.assertEqual(activation._cai_route(IP),ROUTE)
        self.assertEqual(query.call_args_list,[mock.call('-4','route','get',IP),mock.call('-d','link','show'),mock.call('-4','address','show')])
        for change in ('gateway','lte','down','duplicate_source','own_address','broadcast'):
            route,links,addresses=self.route_data();target=IP
            if change=='gateway':route[0]['gateway']='192.0.2.1'
            elif change=='lte':route[0]['dev']=links[0]['ifname']='enp3s0'
            elif change=='down':links[0]['flags']=[]
            elif change=='duplicate_source':addresses.append(copy.deepcopy(addresses[0]))
            elif change=='own_address':target=ROUTE['source']
            elif change=='broadcast':target='192.0.2.255'
            with self.subTest(change=change),mock.patch.object(activation,'_cai_ip_json',side_effect=[route,links,addresses]),self.assertRaises(activation.CameraActivationError):
                activation._cai_route(target)

    def test_existing_enp2_vlan_is_supported_without_creating_interfaces(self):
        route,links,addresses=self.route_data()
        route[0]['dev']='camera100'
        links.append({'ifindex':3,'ifname':'camera100','link_index':2,'linkinfo':{'info_kind':'vlan'},'flags':['UP'],'address':ROUTE['local_mac']})
        addresses[0]['ifname']='camera100'
        with mock.patch.object(activation,'_cai_ip_json',side_effect=[route,links,addresses]):
            value=activation._cai_route(IP)
        self.assertEqual(value['dev'],'camera100');self.assertEqual(value['parent'],'enp2s0')

    def test_password_validation_never_returns_secret_in_errors(self):
        activation._camera_admin_password(PASSWORD,PASSWORD)
        for value in ('admin','Abcd$876','aaaa$90Y','short','Space 78Z','\uac00\ub098Ab78$9'):
            with self.subTest(value=value),self.assertRaises(activation.CameraActivationError) as error:
                activation._camera_admin_password(value,value)
            self.assertNotIn(value,str(error.exception))
        with self.assertRaises(activation.CameraActivationError):activation._camera_admin_password(PASSWORD,'mismatch')

    def test_rsa_plaintext_uses_stdin_only_and_temp_file_contains_only_public_key(self):
        calls=[];temp_names=[]
        def run(command,**kwargs):
            calls.append((command,kwargs))
            filename=Path(command[command.index('-in')+1] if '-in' in command else command[command.index('-inkey')+1])
            temp_names.append(filename)
            self.assertEqual(filename.read_text(),PUBLIC_KEY)
            self.assertNotIn(PASSWORD,' '.join(command))
            if sys.platform=='linux':self.assertEqual(filename.stat().st_mode&0o777,0o600)
            if command[1]=='pkey':return SimpleNamespace(returncode=0,stdout=b'Public-Key: (2049 bit)\n',stderr=b'')
            self.assertEqual(kwargs['input'],PASSWORD.encode())
            return SimpleNamespace(returncode=0,stdout=b'x'*257,stderr=b'')
        with mock.patch.object(activation._cai_shutil,'which',return_value='/inert/openssl'),mock.patch.object(activation._cai_subprocess,'run',side_effect=run):
            encrypted=activation._camera_admin_encrypt(PUBLIC_KEY,PASSWORD)
        self.assertEqual(len(activation._cai_base64.b64decode(encrypted)),257)
        self.assertEqual(len(calls),2);self.assertTrue(all(not name.exists() for name in temp_names))

    def test_rsa_invalid_key_missing_tool_and_failed_encrypt_are_safe(self):
        with mock.patch.object(activation._cai_subprocess,'run') as run,self.assertRaises(activation.CameraActivationError):
            activation._camera_admin_encrypt('bad',PASSWORD)
        run.assert_not_called()
        with mock.patch.object(activation._cai_shutil,'which',return_value=None),self.assertRaises(activation.CameraActivationError):
            activation._camera_admin_encrypt(PUBLIC_KEY,PASSWORD)
        with mock.patch.object(activation._cai_shutil,'which',return_value='/inert/openssl'),\
                mock.patch.object(activation._cai_subprocess,'run',side_effect=[SimpleNamespace(returncode=0,stdout=b'Public-Key: (2048 bit)',stderr=b''),SimpleNamespace(returncode=1,stdout=b'',stderr=PASSWORD.encode())]),\
                self.assertRaises(activation.CameraActivationError) as error:
            activation._camera_admin_encrypt(PUBLIC_KEY,PASSWORD)
        self.assertNotIn(PASSWORD,str(error.exception))


class ArpTests(unittest.TestCase):
    def probe(self,flavor='iputils',macs=(),code=None,stderr='',sent=3):
        if flavor=='iputils':
            help_text='iputils arping';output='Sent {} probes\nReceived {} responses\n'.format(sent,len(macs))
            output+=''.join('Unicast reply from {} [{}]\n'.format(IP,m) for m in macs)
            code=(0 if macs else 1) if code is None else code
        else:
            help_text='Thomas Habets arping';output='{} packets transmitted, {} packets received\n'.format(sent,len(macs))
            output+=''.join('60 bytes from {} ({})\n'.format(m,IP) for m in macs);code=0 if code is None else code
        responses=[SimpleNamespace(stdout=help_text,stderr='',returncode=0),SimpleNamespace(stdout=output,stderr=stderr,returncode=code)]
        with mock.patch.object(activation._cai_shutil,'which',return_value='/inert/arping'),\
                mock.patch.object(activation._cai_subprocess,'run',side_effect=responses) as run:
            value=activation._cai_arp_probe(IP,ROUTE)
            self.assertNotIn('sudo',run.call_args.args[0]);return value,run.call_args.args[0]

    def test_two_arping_flavors_distinguish_free_address_from_one_device(self):
        for flavor in ('iputils','habets'):
            with self.subTest(flavor=flavor):
                value,command=self.probe(flavor)
                self.assertEqual(value['replies'],0);self.assertIsNone(value['mac'])
                value,command=self.probe(flavor,[MAC]*3)
                self.assertEqual(value['mac'],MAC);self.assertEqual(value['replies'],3)
                self.assertEqual(command[-1],IP)
                self.assertIn(ROUTE['source'],command);self.assertIn(ROUTE['dev'],command)

    def test_permission_errors_malformed_counts_and_conflicting_macs_never_mean_free(self):
        for kwargs in ({'stderr':'operation not permitted'},{'code':2},{'sent':2},
                       {'macs':[MAC,'02:99:88:77:66:55']},{'code':0,'macs':[]}):
            with self.subTest(kwargs=kwargs),self.assertRaises(activation.CameraActivationError):self.probe(**kwargs)
        with mock.patch.object(activation._cai_shutil,'which',return_value=None),self.assertRaises(activation.CameraActivationError):
            activation._cai_arp_probe(IP,ROUTE)

    def test_duplicate_probe_demands_two_matching_replies(self):
        for value in ({'replies':1,'mac':MAC},{'replies':3,'mac':'02:99:88:77:66:55'}):
            with mock.patch.object(activation,'_cai_arp_probe',return_value=value),self.assertRaises(activation.CameraActivationError):
                activation._camera_admin_duplicate_probe(IP,dict(ROUTE,expected_mac=MAC))
        with mock.patch.object(activation,'_cai_arp_probe',return_value={'replies':3,'mac':MAC}),mock.patch.object(activation,'_cai_neighbor') as neighbor:
            activation._camera_admin_duplicate_probe(IP,dict(ROUTE,expected_mac=MAC))
        neighbor.assert_not_called()

    def test_address_available_propagates_failed_probe_and_rejects_any_reply(self):
        with mock.patch.object(activation,'_cai_arp_probe',side_effect=activation.CameraActivationError('permission missing')),self.assertRaises(activation.CameraActivationError):
            activation._camera_admin_address_available(IP,ROUTE)
        with mock.patch.object(activation,'_cai_arp_probe',return_value={'replies':1}),self.assertRaises(activation.CameraActivationError):
            activation._camera_admin_address_available(IP,ROUTE)


class HttpsTests(unittest.TestCase):
    def connection(self,status=200,payload=b'{}'):
        connection=mock.Mock();connection.sock.getpeercert.return_value=b'TEST_CERT'
        connection.getresponse.return_value=SimpleNamespace(status=status,read=lambda maximum:payload)
        return connection

    def test_unapproved_request_is_rejected_without_opening_socket(self):
        with mock.patch.object(activation._cai_httpclient,'HTTPSConnection') as factory,self.assertRaises(activation.CameraActivationError):
            activation._cai_request(IP,ROUTE,'GET','/reset.cgi')
        factory.assert_not_called()

    def test_certificate_and_route_must_match_before_post(self):
        connection=self.connection()
        with mock.patch.object(activation._cai_httpclient,'HTTPSConnection',return_value=connection),\
                mock.patch.object(activation,'_cai_route',return_value=ROUTE),self.assertRaises(activation.CameraActivationError):
            activation._cai_request(IP,ROUTE,'POST',activation._CAI_ACTIVATE,b'ciphertext',CERT)
        connection.request.assert_not_called();connection.close.assert_called_once()
        connection=self.connection()
        with mock.patch.object(activation._cai_httpclient,'HTTPSConnection',return_value=connection),\
                mock.patch.object(activation,'_cai_route',return_value=dict(ROUTE,source='192.0.2.11')),self.assertRaises(activation.CameraActivationError):
            activation._cai_request(IP,ROUTE,'GET',activation._CAI_STATUS)
        connection.connect.assert_not_called()

    def test_redirect_oversize_and_invalid_json_are_rejected_without_leaking_body(self):
        for status,payload in ((302,b'secret redirect'),(200,b'x'*65537),(200,PASSWORD.encode()),(200,b'[]')):
            connection=self.connection(status,payload)
            with mock.patch.object(activation._cai_httpclient,'HTTPSConnection',return_value=connection),\
                    mock.patch.object(activation,'_cai_route',return_value=ROUTE),self.assertRaises(activation.CameraActivationError) as error:
                activation._cai_request(IP,ROUTE,'GET',activation._CAI_STATUS)
            self.assertNotIn(PASSWORD,str(error.exception));connection.request.assert_called_once()

    def test_close_race_preserves_response_or_safe_error(self):
        connection=self.connection(200,b'{"Initialized":true}');connection.close.side_effect=OSError('close race')
        with mock.patch.object(activation._cai_httpclient,'HTTPSConnection',return_value=connection) as factory,\
                mock.patch.object(activation,'_cai_route',return_value=ROUTE):
            result,certificate=activation._cai_request(IP,ROUTE,'GET',activation._CAI_STATUS)
        self.assertTrue(result['Initialized'])
        self.assertEqual(factory.call_args.kwargs['source_address'],(ROUTE['source'],0))
        connection.request.side_effect=OSError(PASSWORD)
        with mock.patch.object(activation._cai_httpclient,'HTTPSConnection',return_value=connection),\
                mock.patch.object(activation,'_cai_route',return_value=ROUTE),self.assertRaises(activation.CameraActivationError) as error:
            activation._cai_request(IP,ROUTE,'GET',activation._CAI_STATUS)
        self.assertNotIn(PASSWORD,str(error.exception))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='activation-offline-');self.addCleanup(self.temp.cleanup)
        self.manager=activation.CameraActivationManager(Path(self.temp.name)/'recording.json')
        self.addCleanup(self.manager.close)
        self.status={'Initialized':False,'NewPasswordPolicy':True,'MaxPasswordLength':64,'SpecialType':'','PublicKey':PUBLIC_KEY}
        self.cert=CERT;self.mac=MAC;self.route=copy.deepcopy(ROUTE);self.now=100.;self.calls=[];self.post_fail=False;self.apply_changes=True
        def request(ip,route,method,path,body=None,expected_cert=None):
            if expected_cert is not None and expected_cert!=self.cert:raise activation.CameraActivationError('changed cert')
            self.calls.append((method,path,body))
            if method=='POST':
                if self.apply_changes:self.status['Initialized']=True
                if self.post_fail:raise activation.CameraActivationError('timeout '+PASSWORD)
                return {},self.cert
            return copy.deepcopy(self.status),self.cert
        patches=(mock.patch.object(activation,'_cai_route',side_effect=lambda *args:copy.deepcopy(self.route)),
                 mock.patch.object(activation,'_cai_neighbor',side_effect=lambda *args:self.mac),
                 mock.patch.object(activation,'_cai_request',side_effect=request),
                 mock.patch.object(activation,'_camera_admin_duplicate_probe',return_value={'mac':MAC,'replies':3,'probe_flavor':'fake'}),
                 mock.patch.object(activation,'_camera_admin_encrypt',return_value='ENCRYPTED+/='),
                 mock.patch.object(activation._cai_time,'sleep'),
                 mock.patch.object(activation._cai_time,'monotonic',side_effect=lambda:self.now))
        for patch in patches:patch.start();self.addCleanup(patch.stop)

    def payload(self,**changes):
        value={'ip':IP,'expected_mac':MAC,'expected_cert_sha256':CERT,'isolated':True,'new_password':PASSWORD,'confirm_password':PASSWORD}
        value.update(changes);return value

    def plan(self):return self.manager.preview(self.payload())['token']
    def posts(self):return [row for row in self.calls if row[0]=='POST']

    def test_inspect_and_plan_are_read_only_and_do_not_persist_password(self):
        inspected=self.manager.inspect({'ip':IP})
        self.assertTrue(inspected['activation_available'])
        token=self.plan()
        self.assertEqual(self.posts(),[])
        self.assertNotIn(PASSWORD,repr(self.manager._plans))
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])
        self.assertEqual(self.manager._plans[token]['public']['username'],'admin')

    def test_preview_requires_explicit_isolation_matching_mac_and_initial_state(self):
        for changes in ({'isolated':False},{'expected_mac':'02:aa:aa:aa:aa:aa'},{'expected_cert_sha256':'bad'}):
            with self.subTest(changes=changes),self.assertRaises(activation.CameraActivationError):self.manager.preview(self.payload(**changes))
        self.status['Initialized']=True
        self.assertFalse(self.manager.inspect({'ip':IP})['activation_available'])
        with self.assertRaises(activation.CameraActivationError):self.plan()
        self.assertEqual(self.posts(),[])

    def test_apply_sends_one_encrypted_post_and_requires_verification(self):
        token=self.plan();result=self.manager.apply(self.payload(token=token))
        self.assertTrue(result['activated']);self.assertTrue(result['registration_required'])
        self.assertEqual(len(self.posts()),1)
        self.assertEqual(self.posts()[0][1],activation._CAI_ACTIVATE)
        self.assertEqual(self.posts()[0][2],b'ENCRYPTED%2B%2F%3D')
        self.assertNotIn(PASSWORD,json.dumps(result))
        with self.assertRaises(activation.CameraActivationError):self.manager.apply(self.payload(token=token))
        self.assertEqual(len(self.posts()),1)

    def test_expired_closed_and_password_mismatch_requests_cannot_write(self):
        token=self.plan();self.now+=301
        with self.assertRaises(activation.CameraActivationError):self.manager.apply(self.payload(token=token))
        token=self.plan()
        with self.assertRaises(activation.CameraActivationError):self.manager.apply(self.payload(token=token,confirm_password='wrong'))
        token=self.plan();self.manager.close()
        for action,payload in ((self.manager.inspect,{'ip':IP}),(self.manager.preview,self.payload()),(self.manager.apply,self.payload(token=token))):
            with self.assertRaises(activation.CameraActivationError):action(payload)
        self.assertEqual(self.posts(),[])

    def test_changed_identity_route_policy_or_state_blocks_activation(self):
        for change in ('mac','cert','route','policy','initialized'):
            self.manager=activation.CameraActivationManager(Path(self.temp.name)/'recording.json')
            self.status['Initialized']=False;self.status['MaxPasswordLength']=64
            self.mac=MAC;self.cert=CERT;self.route=copy.deepcopy(ROUTE)
            token=self.plan()
            if change=='mac':self.mac='02:aa:aa:aa:aa:aa'
            elif change=='cert':self.cert='b'*64
            elif change=='route':self.route['source']='192.0.2.11'
            elif change=='policy':self.status['MaxPasswordLength']=32
            else:self.status['Initialized']=True
            with self.subTest(change=change),self.assertRaises(activation.CameraActivationError):self.manager.apply(self.payload(token=token))
            self.manager.close()
        self.assertEqual(self.posts(),[])

    def test_post_timeout_is_not_retried_and_uncertainty_is_reported(self):
        token=self.plan();self.post_fail=True
        with self.assertRaises(activation.CameraActivationError) as error:self.manager.apply(self.payload(token=token))
        self.assertEqual(len(self.posts()),1)
        self.assertIn('다시 전송하지',str(error.exception));self.assertNotIn(PASSWORD,str(error.exception))

    def test_http_success_without_initialized_state_is_not_reported_as_success(self):
        token=self.plan();self.apply_changes=False
        with self.assertRaises(activation.CameraActivationError) as error:self.manager.apply(self.payload(token=token))
        self.assertEqual(len(self.posts()),1);self.assertIn('등록 완료',str(error.exception))


class PreparationHelperTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='linux','uses Linux bash help/syntax only')
    def test_helper_help_and_syntax_do_not_request_privileges(self):
        path=Path(__file__).with_name('tools')/'prepare_camera_admin.sh'
        for args in (['-n',str(path)],[str(path),'--help']):
            result=subprocess.run(['/bin/bash']+args,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
        source=path.read_text(encoding='utf-8')
        self.assertLess(source.index('if (( APPLY == 0 ))'),source.index('setcap cap_net_raw+ep'))
        self.assertLess(source.index('if (( EUID != 0 ))'),source.index('setcap cap_net_raw+ep'))
        self.assertNotIn('setcap cap_net_raw+ep python',source)


if __name__=='__main__':unittest.main()
