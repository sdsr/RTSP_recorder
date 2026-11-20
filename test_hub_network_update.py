"""Exercise management VLAN creation and rollback with an in-memory NetworkManager."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('management_network_tests', ROOT / 'tools/switch_management_network.py')
network = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(network)
SWITCH_SPEC = importlib.util.spec_from_file_location('hub_discovery_tests', ROOT / 'components/switch_manager.py')
switch = importlib.util.module_from_spec(SWITCH_SPEC)
SWITCH_SPEC.loader.exec_module(switch)

PARENT_UUID = '12345678-1234-4234-abcd-000000000001'
PROFILE_UUID = '12345678-1234-4234-abcd-000000000002'
FOREIGN_UUID = '12345678-1234-4234-abcd-000000000003'
PARENT_MAC = '02:00:00:00:00:20'
HUB_MAC = '02:00:00:00:00:21'


class NetworkLifecycle(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(parent='enp2s0f1', vlan=100, source='192.0.2.10',
                                    target='192.0.2.20', parent_mac=PARENT_MAC,
                                    management_mac=HUB_MAC, apply=True)
        self.info = dict(interface='edmg100', parent='enp2s0f1', vlan=100,
                         source=self.args.source, target=self.args.target,
                         profile='rtsp-mgmt-enp2s0f1-100', stable_id='recorder-stable-id')
        self.existing = self.persistent = self.active = False
        self.stable_id = self.info['stable_id']
        self.writes = []
        self.patch('nm', self.fake_nm)
        self.patch('check_links', mock.Mock())
        self.patch('settings_match', mock.Mock(return_value=True))
        self.patch('profile_is_persistent', lambda uid: self.persistent)
        self.patch('verify_active', mock.Mock(return_value=HUB_MAC))

    def patch(self, name, value):
        patcher = mock.patch.object(network, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_nm(self, *args):
        if args[:2] == ('-g', 'UUID,NAME'):
            return PROFILE_UUID + ':' + self.info['profile'] if self.existing else ''
        if args[:2] == ('-g', 'GENERAL.CON-UUID'):
            return PARENT_UUID
        if args[:2] == ('-g', 'connection.stable-id'):
            return self.stable_id
        if args[:2] == ('-g', 'UUID'):
            return PROFILE_UUID if self.active else ''
        if args[0] != 'con':
            self.fail('Unexpected nmcli arguments: ' + repr(args))
        self.writes.append(args)
        if args[1] == 'add':
            self.existing = True
            return 'Connection added (' + PROFILE_UUID + ')'
        if args[1] == 'up':
            self.active = True
        elif args[1] == 'modify':
            self.persistent = True
        elif args[1] == 'delete':
            self.existing = self.active = self.persistent = False
        elif args[1] == 'down':
            self.active = False
        else:
            self.fail('Unexpected mutation: ' + repr(args))
        return ''

    def test_no_changes_before_explicit_apply(self):
        self.args.apply = False
        self.assertEqual(network._configure(self.args, self.info)['status'], 'planned')
        self.assertEqual(self.writes, [])
        network.verify_active.assert_not_called()

    def test_installs_only_host_route_and_is_idempotent(self):
        report = network._configure(self.args, self.info)
        self.assertEqual(report['management_mac'], HUB_MAC)
        add, up, persist = self.writes
        for key, value in [('save', 'no'), ('autoconnect', 'no'),
                           ('ipv4.routes', self.args.target + '/32'),
                           ('ipv4.addresses', self.args.source + '/32'),
                           ('ipv4.never-default', 'yes'), ('ipv6.method', 'disabled')]:
            self.assertEqual(add[add.index(key) + 1], value)
        self.assertNotIn('ipv4.gateway', add)
        self.assertEqual(up, ('con', 'up', 'uuid', PROFILE_UUID))
        self.assertEqual(persist, ('con', 'modify', 'uuid', PROFILE_UUID, 'connection.autoconnect', 'yes'))
        self.writes.clear()
        self.assertEqual(network._configure(self.args, self.info), report)
        self.assertEqual(self.writes, [])

    def test_failed_mac_verification_rolls_back_only_our_new_profile(self):
        network.verify_active.side_effect = network.SetupError('different MAC')
        with self.assertRaisesRegex(network.SetupError, 'different MAC'):
            network._configure(self.args, self.info)
        self.assertEqual([action[1] for action in self.writes], ['add', 'up', 'delete'])
        self.assertEqual(self.writes[-1], ('con', 'delete', 'uuid', PROFILE_UUID))

    def test_restores_preexisting_persistent_profile_inactive_after_failed_retry(self):
        self.existing = self.persistent = True
        network.verify_active.side_effect = network.SetupError('unreachable')
        with self.assertRaises(network.SetupError):
            network._configure(self.args, self.info)
        self.assertEqual(self.writes, [('con', 'up', 'uuid', PROFILE_UUID),
                                       ('con', 'down', 'uuid', PROFILE_UUID)])
        self.assertTrue(self.persistent)
        self.assertFalse(self.active)

    def test_foreign_same_name_or_changed_profile_is_never_adopted(self):
        self.existing = True
        self.stable_id = 'other-profile'
        with self.assertRaises(network.SetupError):
            network._configure(self.args, self.info)
        self.stable_id = self.info['stable_id']
        network.settings_match.return_value = False
        with self.assertRaises(network.SetupError):
            network._configure(self.args, self.info)
        self.assertEqual(self.writes, [])


class ManagementPreflight(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(parent='enp2s0f1', parent_mac=PARENT_MAC, vlan=100,
                                    source='192.0.2.10', target='192.0.2.20', management_mac=HUB_MAC)
        self.links = [{'ifindex': 2, 'ifname': 'enp2s0f1', 'link_type': 'ether',
                       'flags': ['UP', 'LOWER_UP'], 'address': PARENT_MAC}]
        self.addresses = []
        self.routes = []
        self.default = []
        self.ip = {('-d', 'link', 'show'): self.links, ('-4', 'address', 'show'): self.addresses,
                   ('-4', 'route', 'show', 'table', 'all'): self.routes,
                   ('-4', 'route', 'show', 'default'): self.default}
        self.nm = mock.patch.object(network, 'nm', return_value='').start()
        mock.patch.object(network, 'ip', side_effect=lambda *args:self.ip[args]).start()
        self.addCleanup(mock.patch.stopall)

    def test_interface_occupied_by_other_connection_is_refused_before_activation(self):
        self.links.append({'ifname': 'edmg100', 'link': 'enp2s0f1',
                           'linkinfo': {'info_kind':'vlan','info_data':{'id':100}}})
        for owner in (FOREIGN_UUID, '--', ''):
            with self.subTest(owner=owner):
                self.nm.return_value = owner
                with self.assertRaises(network.SetupError):
                    network.check_links(self.args, 'edmg100', PROFILE_UUID)
        self.assertTrue(all(c.args[:2] == ('-g', 'GENERAL.CON-UUID') for c in self.nm.call_args_list))

    def test_overlapping_subnet_other_route_or_default_gateway_prevents_setup(self):
        scenarios = [(self.addresses, {'ifname':'enp3s0','addr_info':[{'local':'192.0.2.99','prefixlen':24}]}),
                     (self.routes, {'dev':'enp3s0','dst':'192.0.2.20/32'}),
                     (self.default, {'dev':'enp2s0f1','gateway':'198.51.100.1'})]
        for entries, row in scenarios:
            with self.subTest(row=row):
                entries.append(row)
                with self.assertRaises(network.SetupError):
                    network.check_links(self.args, 'edmg100', '')
                entries.clear()

    def test_non_camera_parent_loopback_and_identical_ips_rejected(self):
        for key, value in [('parent', 'enp3s0'), ('source', '127.0.0.1'), ('source', self.args.target), ('vlan', 4095)]:
            with self.subTest(key=key):
                args = SimpleNamespace(**dict(vars(self.args), **{key: value}))
                with self.assertRaises(network.SetupError):
                    network.validate_args(args)


class CandidateLookup(unittest.TestCase):
    def test_only_named_distinct_addresses_and_at_most_four_per_request(self):
        self.assertEqual(switch._sm_candidate_ips({}), [])
        self.assertEqual(switch._sm_candidate_ips({'candidate_ips': ['192.0.2.3'] * 2}), ['192.0.2.3'])
        for values in (['192.0.2.0/24'], '192.0.2.5', ['192.0.2.5'] * 5):
            with self.subTest(values=values), self.assertRaises(switch.SwitchManagerError):
                switch._sm_candidate_ips({'candidate_ips': values})

    def test_stale_neighbor_is_not_substituted_if_fresh_check_fails(self):
        candidate = '192.0.2.31'
        observed = {'id': 'candidate', 'name': 'candidate', 'ip': candidate, 'kind': 'candidate',
                    'interface':'camera101', 'source':'192.0.2.20', 'route_error':'',
                    'neighbors':[{'mac':HUB_MAC, 'state':['STALE']}]}
        with mock.patch.object(switch, '_sm_camera_neighbors', return_value=[observed]), \
                mock.patch.object(switch, '_sm_route', return_value={'parent':'enp2s0f1', 'source':'192.0.2.20'}), \
                mock.patch.object(switch, '_cai_arp_probe', return_value={'replies':0}, create=True):
            result = switch._sm_discover_candidates([candidate], 'enp2s0f1')
        self.assertEqual(result[0]['neighbors'], [])
        self.assertFalse(result[0]['probe_fresh'])
        self.assertTrue(result[0]['probe_checked'])
        match = switch._sm_camera_matches(result, [], [], [8])[0]
        self.assertEqual(match['status'], 'probe_failed')
        self.assertIsNone(match['direct_port'])

    def test_candidate_cannot_be_queried_over_another_parent(self):
        observed = {'ip':'192.0.2.31','source':'192.0.2.20', 'interface':'enp3s0','neighbors':[]}
        with mock.patch.object(switch, '_sm_camera_neighbors', return_value=[observed]), \
                mock.patch.object(switch, '_sm_route', return_value={'parent':'enp3s0'}), \
                mock.patch.object(switch, '_cai_arp_probe', create=True) as probe:
            result = switch._sm_discover_candidates(['192.0.2.31'], 'enp2s0f1')
        probe.assert_not_called()
        self.assertFalse(result[0]['probe_fresh'])
        self.assertTrue(result[0]['probe_error'])


if __name__ == '__main__':
    unittest.main()
