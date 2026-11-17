"""Offline NEXTU management checks; no real network, arping or host changes."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


COMPONENT = Path(__file__).with_name('components') / 'switch_manager.py'
SPEC = importlib.util.spec_from_file_location('switch_manager_under_test', COMPONENT)
switch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(switch)

CPU_MAC = '02:00:00:00:00:10'
ED_MAC = '02:00:00:00:00:20'
SECRET = 'TEST_ONLY_SECRET'


class FakeSwitch:
    def __init__(self):
        self.model, self.firmware, self.mac = switch._SM_MODEL, switch._SM_FIRMWARE, CPU_MAC
        self.ip, self.mask, self.gateway = '192.0.2.10', '255.255.255.0', '192.0.2.1'
        self.hostname = 'TEST-HUB'
        self.ports = {p: {'up': True, 'vlan': 100 if p == 8 else 101,
                          'mode': 'trunk' if p >= 9 else 'access', 'extra': []}
                      for p in range(1, 11)}
        self.macs = [(ED_MAC, 8), ('02:00:00:00:00:31', 1)]
        self.vlans = [100, 101, 102, 999]
        self.reload = 'Reload is not configured'
        self.commands, self.connections = [], []
        self.fail_once = None
        self.disconnect_after_ip = False
        self.close_error = False
        self.unrelated_change = False
        self.startup = self.running()

    def running(self):
        lines = ['!', 'hostname ' + self.hostname, 'username admin password ' + SECRET, '!']
        for port, state in self.ports.items():
            lines += ['interface Ethernet1/0/' + str(port), ' switchport mode ' + state['mode'],
                      ' switchport access vlan ' + str(state['vlan'])]
            if not state['up']:
                lines.append(' shutdown')
            lines += [' ' + value for value in state['extra']] + ['!']
        lines += ['interface vlan100', ' ip address ' + self.ip + ' ' + self.mask, '!',
                  'control-vlan 999']
        if self.gateway:
            lines.append('ip route 0.0.0.0 0.0.0.0 ' + self.gateway)
        return '\n'.join(lines + ['end', ''])

    def sync(self):
        self.startup = self.running()

    def client(self, connection, password):
        hardware = self
        class Client:
            def __init__(self):
                self.selected_port = None
            def __enter__(self):
                hardware.connections.append(dict(connection))
                if connection['target'] != hardware.ip:
                    raise switch._SwitchConnectionError('offline fake endpoint')
                return self
            def __exit__(self, *args):
                self.close()
            def close(self):
                if hardware.close_error:
                    raise OSError('already disconnected')
            def command(self, command, timeout=30):
                if not switch._sm_allowed_command(command):
                    raise AssertionError('Unexpected command: ' + command)
                hardware.commands.append(command)
                if hardware.fail_once == command:
                    hardware.fail_once = None
                    raise switch.SwitchManagerError('TEST_COMMAND_FAILED')
                if command == 'show version':
                    return ('Device: {}, test\nCPU MAC {}\nSoftWare Version {}\nSerial Num: TEST-SERIAL\n'
                            .format(hardware.model, hardware.mac, hardware.firmware))
                if command == 'show running-config': return hardware.running()
                if command == 'show startup-config': return hardware.startup
                if command == 'show vlan':
                    return '\n'.join('{} vlan{} Static'.format(v, v) for v in hardware.vlans)
                if command == 'show mac-address-table':
                    return '\n'.join('101 {} dynamic Ethernet1/0/{}'.format(mac, port) for mac, port in hardware.macs)
                if command == 'show reload': return hardware.reload
                if command.startswith('show interface Ethernet1/0/'):
                    port = int(command.rsplit('/', 1)[1])
                    return 'Ethernet1/0/{} is up, line protocol is {}'.format(port, 'up' if hardware.ports[port]['up'] else 'down')
                if command.startswith('show switchport interface Ethernet1/0/'):
                    state = hardware.ports[int(command.rsplit('/', 1)[1])]
                    return 'Mode : {}\nPort VID : {}\n'.format(state['mode'], state['vlan'])
                if command.startswith('interface Ethernet1/0/'):
                    self.selected_port = int(command.rsplit('/', 1)[1])
                elif command in ('shutdown', 'no shutdown'):
                    hardware.ports[self.selected_port]['up'] = command != 'shutdown'
                    if hardware.unrelated_change: hardware.hostname = 'EXTERNAL-CHANGE'
                elif command.startswith('switchport access vlan '):
                    hardware.ports[self.selected_port]['vlan'] = int(command.rsplit(' ', 1)[1])
                elif command.startswith('ip address '):
                    _, _, hardware.ip, hardware.mask = command.split()
                    if hardware.disconnect_after_ip:
                        hardware.disconnect_after_ip = False
                        raise switch._SwitchConnectionError('fake SVI disconnected')
                elif command.startswith('no ip route '): hardware.gateway = ''
                elif command.startswith('ip route '): hardware.gateway = command.rsplit(' ', 1)[1]
                elif command == 'write': hardware.sync()
                return 'OK'
        return Client()


class SwitchFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='switch-manager-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = switch.SwitchManager(self.root / 'switches.json')
        self.hardware = FakeSwitch()
        self.manager._client_factory = self.hardware.client
        self.manager._route = mock.Mock(return_value={'dev': 'enp2s0f1', 'source': '192.0.2.20',
                                                       'parent': 'enp2s0f1', 'local_mac': ED_MAC})
        self.manager._collision = mock.Mock()
        self.manager._sleep = mock.Mock()
        self.connection = {'id': 'test-hub', 'name': '시험 허브', 'target': '192.0.2.10',
                           'source': '192.0.2.20', 'interface': 'enp2s0f1', 'username': 'admin',
                           'expected_cpu_mac': CPU_MAC, 'camera_ports': [1, 2, 3, 8]}
        self.manager.save_connection(dict(self.connection, password=SECRET))
        self.request = {'switch_id': 'test-hub', 'password': SECRET, 'action': 'port', 'port': 1, 'state': 'off'}
        # Construction/save/inspect tests must never accidentally leave their fakes.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(switch._sm_socket, 'socket', side_effect=AssertionError('real socket forbidden')).start()
        mock.patch.object(switch._sm_subprocess, 'run', side_effect=AssertionError('real system command forbidden')).start()

    def plan(self, **values):
        return self.manager.plan(dict(self.request, **values))

    def apply(self, plan):
        return self.manager.apply({'plan_id': plan['plan_id'], 'password': SECRET, 'confirm': True})

    def mutations(self):
        return [c for c in self.hardware.commands if not c.startswith('show ')]


class SwitchConfigurationTests(SwitchFixture):
    def test_constructor_and_connection_save_are_offline_and_never_store_password(self):
        self.assertEqual(self.hardware.commands, [])
        self.assertEqual(self.hardware.connections, [])
        self.assertNotIn(SECRET, self.manager.config_path.read_text(encoding='utf-8'))
        self.assertEqual(self.manager.get_config(), {'switches': [self.connection]})
        if sys.platform == 'linux':
            self.assertEqual(self.manager.config_path.stat().st_mode & 0o777, 0o600)

    def test_bad_config_or_request_has_friendly_error(self):
        for value in ([], 'text', 42, {'switches': [None]}, {'switches': 'wrong'}):
            with self.subTest(value=value):
                self.manager.config_path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(switch.SwitchManagerError): self.manager.get_config()
        for value in (None, [], 1):
            for method in (self.manager.inspect, self.manager.plan, self.manager.apply):
                with self.subTest(value=value, method=method.__name__), self.assertRaises(switch.SwitchManagerError):
                    method(value)

    def test_closing_rejects_new_reads_plans_and_writes(self):
        plan = self.plan()
        self.manager.close()
        calls = list(self.hardware.commands)
        for operation in (lambda: self.manager.inspect(self.request), lambda: self.plan(),
                          lambda: self.apply(plan), lambda: self.manager.save_connection(self.connection)):
            with self.assertRaises(switch.SwitchManagerError): operation()
        self.assertEqual(self.hardware.commands, calls)

    def test_read_and_plan_do_not_issue_mutating_commands_or_backups(self):
        inspected = self.manager.inspect(self.request)
        self.assertTrue(inspected['can_modify'])
        plan = self.plan()
        self.assertEqual(plan['after']['admin_up'], False)
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.root / 'switch_backups').exists())
        self.assertNotIn(SECRET, json.dumps(plan))
        self.assertNotIn(SECRET, repr(self.manager._plans))

    def test_model_firmware_pin_unsaved_and_reload_guards(self):
        scenarios = (
            ('model', 'UNSUPPORTED'), ('firmware', 'UNKNOWN'), ('mac', '02:00:00:00:00:99'),
            ('hostname', 'UNSAVED'), ('reload', 'Reload scheduled in 1 minute'))
        for attribute, value in scenarios:
            with self.subTest(attribute=attribute):
                old = getattr(self.hardware, attribute)
                setattr(self.hardware, attribute, value)
                try:
                    with self.assertRaises(switch.SwitchManagerError): self.plan()
                finally: setattr(self.hardware, attribute, old)
        self.manager.save_connection(dict(self.connection, expected_cpu_mac=''))
        with self.assertRaises(switch.SwitchManagerError): self.plan()
        self.assertEqual(self.mutations(), [])

    def test_protected_ed_fiber_trunk_control_and_multi_mac_ports(self):
        for port in (8, 9, 10):
            with self.subTest(port=port), self.assertRaises(switch.SwitchManagerError): self.plan(port=port)
        self.hardware.ports[1]['mode'] = 'trunk'; self.hardware.sync()
        with self.assertRaises(switch.SwitchManagerError): self.plan()
        self.hardware.ports[1]['mode'] = 'access'; self.hardware.ports[1]['vlan'] = 999; self.hardware.sync()
        with self.assertRaises(switch.SwitchManagerError): self.plan()
        self.hardware.ports[1]['vlan'] = 101; self.hardware.ports[1]['extra'] = ['channel-group 1']; self.hardware.sync()
        with self.assertRaises(switch.SwitchManagerError): self.plan()
        self.hardware.ports[1]['extra'] = []; self.hardware.macs.append(('02:00:00:00:00:32', 1)); self.hardware.sync()
        with self.assertRaises(switch.SwitchManagerError): self.plan()
        self.assertEqual(self.mutations(), [])


class SwitchTransactionTests(SwitchFixture):
    def test_apply_requires_confirmation_and_consumes_plan_once(self):
        plan = self.plan()
        with self.assertRaises(switch.SwitchManagerError):
            self.manager.apply({'plan_id': plan['plan_id'], 'password': SECRET, 'confirm': 'yes'})
        self.assertEqual(self.mutations(), [])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        self.assertFalse(self.hardware.ports[1]['up'])
        self.assertTrue(result['inspection']['can_modify'])
        self.assertEqual(self.hardware.running(), self.hardware.startup)
        with self.assertRaises(switch.SwitchManagerError): self.apply(plan)
        self.assertNotIn(SECRET, json.dumps(result))
        backup = Path(result['backup_path'])
        self.assertIn(SECRET, (backup / 'show_running-config.txt').read_text())
        if sys.platform == 'linux':
            self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in backup.iterdir()))

    def test_expired_changed_config_or_changed_hardware_blocks_before_writes(self):
        plan = self.plan()
        with mock.patch.object(switch._sm_time, 'time', return_value=plan['expires_at'] + 1), self.assertRaises(switch.SwitchManagerError):
            self.apply(plan)
        plan = self.plan()
        self.manager.save_connection(dict(self.connection, name='renamed'))
        with self.assertRaises(switch.SwitchManagerError): self.apply(plan)
        plan = self.plan()
        self.hardware.ports[2]['up'] = False; self.hardware.sync()
        with self.assertRaises(switch.SwitchManagerError): self.apply(plan)
        self.assertEqual(self.mutations(), [])

    def test_port_bounce_waits_ten_seconds_without_touching_poe_or_other_ports(self):
        before = copy.deepcopy(self.hardware.ports)
        result = self.apply(self.plan(state='bounce'))
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.hardware.ports, before)
        self.manager._sleep.assert_called_once_with(10)
        self.assertEqual(self.mutations(), ['config terminal', 'interface Ethernet1/0/1', 'shutdown', 'no shutdown', 'exit', 'exit', 'write'])

    def test_vlan_allows_only_existing_non_management_non_control_vlan(self):
        for vlan in (100, 999, 2048):
            with self.subTest(vlan=vlan), self.assertRaises(switch.SwitchManagerError):
                self.plan(action='vlan', vlan=vlan)
        result = self.apply(self.plan(action='vlan', vlan=102))
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.hardware.ports[1]['vlan'], 102)
        self.assertEqual(self.hardware.ports[8]['vlan'], 100)

    def test_management_change_reconnects_to_pinned_new_ip_and_updates_local_endpoint(self):
        plan = self.plan(action='management', ip='192.0.2.11', netmask='255.255.255.0', gateway='192.0.2.1')
        self.assertEqual(self.mutations(), [])
        self.hardware.disconnect_after_ip = True
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.hardware.ip, '192.0.2.11')
        self.assertEqual(self.manager.get_config()['switches'][0]['target'], '192.0.2.11')
        self.assertTrue(any(c['target'] == '192.0.2.11' for c in self.hardware.connections))
        self.manager._collision.assert_called_with(self.connection, '192.0.2.11')

    def test_management_outside_existing_network_is_rejected(self):
        for address in ('192.0.3.11', '192.0.2.0', '192.0.2.255'):
            with self.subTest(address=address), self.assertRaises(switch.SwitchManagerError):
                self.plan(action='management', ip=address, netmask='255.255.255.0', gateway='192.0.2.1')
        self.assertEqual(self.mutations(), [])

    def test_failed_save_rolls_back_only_original_port_and_verifies_persistence(self):
        plan = self.plan()
        self.hardware.fail_once = 'write'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'rolled_back')
        self.assertTrue(result['rollback_verified'])
        self.assertFalse(result['reboot_scheduled'])
        self.assertTrue(self.hardware.ports[1]['up'])
        self.assertEqual(self.hardware.running(), self.hardware.startup)
        self.assertEqual(self.mutations().count('write'), 2)

    def test_unrelated_running_change_is_not_implicitly_saved_during_recovery(self):
        plan = self.plan()
        startup = self.hardware.startup
        self.hardware.unrelated_change = True
        result = self.apply(plan)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['rollback_verified'])
        self.assertTrue(self.hardware.ports[1]['up'])
        self.assertEqual(self.hardware.startup, startup)
        self.assertNotIn('write', self.mutations())

    def test_disconnect_during_cleanup_does_not_mask_success_or_strand_lock(self):
        plan = self.plan()
        self.hardware.close_error = True
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        acquired = []
        def other_thread():
            ok = self.manager._operation_lock.acquire(False)
            acquired.append(ok)
            if ok: self.manager._operation_lock.release()
        thread = threading.Thread(target=other_thread)
        thread.start(); thread.join(2)
        self.assertEqual(acquired, [True])


class SwitchLowLevelTests(unittest.TestCase):
    def test_command_allowlist_rejects_host_configuration_arbitrary_cli_and_injection(self):
        for command in ('reload', 'reboot', 'erase startup-config', 'show running-config\nwrite',
                        'interface Ethernet1/0/9; shutdown', 'shutdown\r', 'poe enable',
                        'ip address 192.0.2.10 255.0.255.0', 'interface Ethernet1/0/11'):
            with self.subTest(command=command): self.assertFalse(switch._sm_allowed_command(command))
        for command in ('show interface Ethernet1/0/10', 'interface Ethernet1/0/1',
                        'switchport access vlan 101', 'ip address 192.0.2.10 255.255.255.0'):
            with self.subTest(command=command): self.assertTrue(switch._sm_allowed_command(command))

    def test_redaction_removes_authentication_lines_and_cli_errors_are_detected(self):
        raw = 'hostname TEST\nusername admin password ' + SECRET + '\nsnmp community PRIVATE\ninterface vlan100'
        redacted = switch._sm_redact(raw)
        self.assertNotIn(SECRET, redacted);self.assertNotIn('PRIVATE', redacted)
        self.assertIn('hostname TEST', redacted)
        self.assertIsNotNone(switch._sm_cli_error('% Invalid input'))
        self.assertIsNotNone(switch._sm_cli_error('Saving configuration failed'))
        self.assertIsNone(switch._sm_cli_error('Failed: 0\nTEST#'))

    def test_route_queries_are_readonly_and_lte_or_gateway_routes_fail_closed(self):
        connection = {'target':'192.0.2.10','source':'192.0.2.20','interface':'nxmg100'}
        links = [{'ifindex':1,'ifname':'enp2s0f1','address':ED_MAC},
                 {'ifindex':2,'ifname':'nxmg100','address':ED_MAC,'link_index':1,'linkinfo':{'info_kind':'vlan'}}]
        with mock.patch.object(switch, '_sm_ip_json', side_effect=[links,
                [{'addr_info':[{'family':'inet','local':'192.0.2.20'}]}], [{'dev':'nxmg100'}]]) as query:
            route = switch._sm_route(connection)
        self.assertEqual(route['parent'], 'enp2s0f1')
        self.assertEqual(query.call_args_list, [mock.call('-d','link','show'),
            mock.call('address','show','dev','nxmg100'), mock.call('-4','route','get','192.0.2.10','from','192.0.2.20')])
        with mock.patch.object(switch, '_sm_ip_json', return_value=[{'ifindex':1,'ifname':'wwan0'}]), self.assertRaises(switch.SwitchManagerError):
            switch._sm_route(dict(connection,interface='wwan0'))
        with mock.patch.object(switch, '_sm_ip_json', side_effect=[links,
                [{'addr_info':[{'family':'inet','local':'192.0.2.20'}]}], [{'dev':'nxmg100','gateway':'192.0.2.1'}]]), self.assertRaises(switch.SwitchManagerError):
            switch._sm_route(connection)

    def test_unexpected_confirmation_is_declined_instead_of_answered_yes(self):
        transport = mock.Mock()
        transport.expect.return_value = (3, None, b'Are you sure?')
        client = switch._SwitchCLI({}, SECRET)
        client.transport, client.prompt = transport, rb'TEST#'
        with self.assertRaises(switch.SwitchManagerError): client.command('shutdown')
        self.assertEqual(transport.write.call_args_list, [mock.call(b'shutdown\r\n'), mock.call(b'n\r\n')])

    def test_telnet_negotiation_and_iac_escaping_do_not_enter_command_text(self):
        socket = mock.Mock()
        transport = switch._SwitchTelnet(socket)
        transport._feed(b'login:' + bytes([255,251,1]) + b' ')
        self.assertEqual(transport.buffer, b'login: ')
        socket.sendall.assert_called_once_with(bytes([255,253,1]))
        transport.write(b'x\xffy')
        socket.sendall.assert_called_with(b'x\xff\xffy')


if __name__ == '__main__':
    unittest.main()
