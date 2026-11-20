"""User-confirmed port workflows using fixture cameras and mocked network lookups."""

import json
import unittest
from unittest import mock
from urllib.parse import unquote

import test_camera_admin as fixture

record = fixture.record


class PortCameraTests(fixture.AdminFixture):
    def candidate(self):
        return {'ip': self.primary.host, 'username': self.primary.username,
                'password': self.primary.password, 'expected_mac': self.primary.mac,
                'expected_cert_sha256': self.primary.certificate}

    def test_port_inspect_displays_profiles_without_registration_or_secrets(self):
        self.write(dict(self.data, cameras=[]))
        original = self.path.read_bytes()
        result = self.manager.inspect_candidate(self.candidate())
        self.assertEqual([p['id'] for p in result['profiles']], [2, 4])
        self.assertFalse(result['credentials_saved'])
        self.assertNotIn(self.primary.password, json.dumps(result))
        self.assertEqual(self.path.read_bytes(), original)

    def test_port_inspect_rechecks_mac_before_using_credentials(self):
        target = dict(self.candidate(), expected_mac='02:00:00:00:00:99')
        with mock.patch.object(self.manager, '_snapshot') as authenticate, self.assertRaises(record.CameraAdminError):
            self.manager.inspect_candidate(target)
        authenticate.assert_not_called()

    def test_deferred_handover_leaves_unregistered_camera_out_of_recording_config(self):
        self.write(dict(self.data, cameras=[]))
        original = self.path.read_bytes()
        target = self.candidate()
        self.manager._handover_available = mock.Mock()
        change = dict(action='network', isolated=True, direct_target=target, expected_host=target['ip'],
                      expected_mac=target['expected_mac'], expected_cert_sha256=target['expected_cert_sha256'],
                      defer_reconnect=True, ip='198.51.100.44', netmask='255.255.255.0', gateway='198.51.100.1')
        plan = self.manager.plan(change)
        self.assertFalse(plan['recorder_will_update'])
        client = self.manager._plans[plan['plan_id']]['client']
        client.challenge = 'mock verified challenge'
        real_write = client.update_network
        def acknowledged(desired, interface):
            real_write(desired, interface)
            return 'OK'
        client.update_network = mock.Mock(side_effect=acknowledged)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'accepted_unverified')
        self.assertTrue(result['write_attempted'])
        self.assertFalse(result['camera_change_verified'])
        self.assertFalse(result['recorder_updated'])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(client.update_network.call_count, 1)
        self.assertEqual(self.manager._handover_available.call_count, 2)

    def test_explicit_write_refusal_does_not_probe_new_credentials_or_address(self):
        plan = self.plan()
        client = self.manager._plans[plan['plan_id']]['client']
        calls = self.factory.call_count
        client.update_network = mock.Mock(side_effect=record._CameraAdminWriteRejected('HTTP 403'))
        original = self.path.read_bytes()
        result = self.apply(plan)
        self.assertEqual(result['status'], 'rejected')
        self.assertFalse(result['recorder_updated'])
        self.assertEqual(self.factory.call_count, calls)
        self.assertEqual(self.path.read_bytes(), original)

    def test_id_only_change_preserves_password_and_updates_only_verified_camera(self):
        plan = self.plan('username', new_username='newowner')
        client = self.manager._plans[plan['plan_id']]['client']
        secret = self.primary.password
        def rename(before, username):
            self.assertTrue(client.allow_writes)
            self.primary.username = username
            client.write_attempted = True
        client.update_username = mock.Mock(side_effect=rename)
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        parsed = record.urlsplit(saved['cameras'][0]['url'])
        self.assertEqual((parsed.username, unquote(parsed.password)), ('newowner', secret))
        self.assertEqual(saved['cameras'][1], self.data['cameras'][1])


class PortPreviewTests(unittest.TestCase):
    def test_changed_neighbor_is_rejected_before_preview_settings_or_ffmpeg(self):
        settings = mock.Mock(side_effect=AssertionError('must not spawn preview'))
        manager = record.LiveViewManager(settings)
        with mock.patch.object(record, '_cai_route', return_value={'dev':'enp2s0','source':'192.0.2.1'}), \
                mock.patch.object(record, '_camera_admin_duplicate_probe'), \
                mock.patch.object(record._live_socket, 'create_connection') as preflight, \
                mock.patch.object(record, '_cai_neighbor', return_value='02:00:00:00:00:02'), \
                self.assertRaises(record.LiveViewError):
            manager.start_port({'direct_port':3, 'ip':'192.0.2.11','mac':'02:00:00:00:00:01',
                                'interface':'enp2s0','source':'192.0.2.1'}, 'owner', 'ManualInput', '4')
        preflight.assert_called_once_with(('192.0.2.11',554), timeout=3, source_address=('192.0.2.1',0))
        settings.assert_not_called()

    def test_displayed_write_refusal_omits_arbitrary_device_text(self):
        rejection = record._ca_write_rejection(200, 'NG [InvalidParameter: IPv4Address, Secret-Value]')
        self.assertIsInstance(rejection, record._CameraAdminWriteRejected)
        self.assertIn('IPv4Address', str(rejection))
        self.assertNotIn('Secret-Value', str(rejection))

    def test_handover_dad_duplicate_reply_rejects_new_address(self):
        route = {'source':'192.0.2.1', 'dev':'enp2s0'}
        with mock.patch.object(record, '_cai_ip_json', return_value=[]), \
                mock.patch.object(record, '_cai_arp_probe', return_value={'replies':1}) as probe, \
                self.assertRaises(record.CameraActivationError):
            record._camera_admin_handover_available('198.51.100.10', route)
        probe.assert_called_once_with('198.51.100.10', route, dad=True)
