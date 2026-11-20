"""Hub-panel integration tests with in-memory switch observations only."""
from contextlib import nullcontext
import json
import threading
import unittest
from unittest import mock

import record


class HubObservationTests(unittest.TestCase):
    def setUp(self):
        self.controller = record.RecorderUiController.__new__(record.RecorderUiController)
        self.controller.port_observations = {}
        self.controller.port_observation_lock = threading.Lock()
        self.controller.port_observation_epoch = 0
        self.controller.switch_manager = mock.Mock()
        self.connection = {'id': 's1', 'name': 'test', 'target': '192.0.2.1', 'camera_ports': [2, 3]}
        self.route = {'interface': 'enp2s0', 'source': '192.0.2.90'}
        self.device = {'ip': '192.0.2.22', 'mac': '02:00:00:00:00:22',
                       'source': '192.0.2.90', 'interface': 'enp2s0', 'direct_port': 2,
                       'probe_fresh': True, 'status': 'matched'}
        self.snapshot = {'supported': True, 'can_modify': True, 'switch_id': 's1', 'ed_ports': [1],
                         'identity': {'cpu_mac': '02:00:00:00:00:01'}, 'connection': self.connection,
                         'route': self.route, 'candidate_matches': [self.device], 'ports': [
                             {'port': 2, 'mode': 'access', 'editable': True, 'admin_up': True, 'link': 'up', 'pvid': 17},
                             {'port': 3, 'mode': 'access', 'admin_up': False, 'link': 'down'}]}
        self.controller.switch_manager.inspect.return_value = self.snapshot
        self.controller.switch_manager.get_config.return_value = {
            'switches': [self.connection], 'connection_health': [
                {'switch_id': 's1', 'route': self.route, 'route_ready': True, 'route_error': ''}]}

    def test_mapping_expires_and_never_retains_login(self):
        with mock.patch.object(record.time, 'monotonic', return_value=100):
            self.controller.switch_operation('inspect', {'switch_id': 's1', 'password': 'SWITCH_TEST_ONLY'})
        self.assertNotIn('SWITCH_TEST_ONLY', json.dumps(self.controller.port_observations))
        with mock.patch.object(record.time, 'monotonic', return_value=200), mock.patch.object(
                record, '_sm_ip_json', return_value=[{'dev': 'enp2s0', 'prefsrc': '192.0.2.90'}]):
            status = self.controller.camera_port_status(self.device['ip'], self.device['mac'])
        self.assertEqual([row['port'] for row in status['matches']], [2])
        with mock.patch.object(record.time, 'monotonic', return_value=220), mock.patch.object(record, '_sm_ip_json') as query:
            status = self.controller.camera_port_status(self.device['ip'], self.device['mac'])
        query.assert_not_called()
        self.assertEqual(status['matches'], [])

    def test_stale_or_later_failed_observation_cannot_suggest_a_port(self):
        self.device['probe_fresh'] = False
        self.controller.switch_operation('inspect', {'switch_id': 's1'})
        self.assertFalse(self.controller.port_observations)
        self.device['probe_fresh'] = True
        self.controller.switch_operation('inspect', {'switch_id': 's1'})
        self.assertIn('s1', self.controller.port_observations)
        self.controller.switch_manager.inspect.side_effect = ValueError('test only')
        with self.assertRaises(ValueError):
            self.controller.switch_operation('inspect', {'switch_id': 's1'})
        self.assertFalse(self.controller.port_observations)

    def test_port_identity_rechecked_and_change_plan_bound_to_port(self):
        ctl = self.controller
        ctl.closing = False
        ctl.camera_activation, ctl.camera_admin, ctl.live_view = mock.Mock(), mock.Mock(), mock.Mock()
        ctl.switch_manager.observing_port.side_effect = lambda _: nullcontext(self.snapshot)
        ctl.camera_activation.inspect.return_value = {
            **self.device, 'initialized': True, 'cert_sha256': 'a'*64}
        ctl.camera_admin.inspect_candidate.return_value = {'profiles': [{'id': 2, 'EncodingType': 'H264'}]}
        ctl.camera_admin.plan.return_value = {'plan_id': 'inner-plan', 'action': 'network'}
        workflow = record.PortCameraWorkflow(ctl)
        fields = {'port': 2, 'peer_ports': [3], 'switch_id': 's1', 'isolated': True, 'ip': self.device['ip']}
        identity = workflow.run('identify', fields)
        fields.update(binding_id=identity['binding_id'], expected_mac=self.device['mac'], expected_cert_sha256='a'*64)
        with self.assertRaises(ValueError):
            workflow.run('ip-plan', fields)
        workflow.run('account', fields)
        workflow.run('live', dict(fields, profile='2'))
        plan = workflow.run('ip-plan', dict(fields, new_ip='192.0.2.55'))
        self.assertNotEqual(plan['plan_id'], 'inner-plan')
        with self.assertRaises(ValueError):
            workflow.run('ip-apply', dict(fields, plan_id='inner-plan', confirm=True))
        self.snapshot['ports'][0]['pvid'] = 22
        with self.assertRaises(ValueError):
            workflow.run('ip-apply', dict(fields, plan_id=plan['plan_id'], confirm=True))
        ctl.camera_admin.apply.assert_not_called()
        ctl.camera_admin.discard_plan.assert_called_once_with('inner-plan')

    def test_wrong_or_unisolated_port_must_not_reach_camera(self):
        ctl = self.controller
        ctl.closing = False
        ctl.camera_activation = mock.Mock()
        ctl.switch_manager.observing_port.side_effect = lambda _: nullcontext(self.snapshot)
        workflow = record.PortCameraWorkflow(ctl)
        fields = {'port': 2, 'peer_ports': [3], 'isolated': True, 'ip': self.device['ip']}
        for patch in ({'isolated': False}, {'peer_ports': [2]}, {'port': 1}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                workflow.run('identify', dict(fields, **patch))
        self.snapshot['ports'][1].update(link='up', admin_up=True)
        with self.assertRaises(ValueError):
            workflow.run('identify', fields)
        ctl.camera_activation.inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
