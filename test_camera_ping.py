"""Deterministic camera reachability tests; no real ICMP or camera requests."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


COMPONENT = Path(__file__).with_name('components') / 'camera_ping.py'
SPEC = importlib.util.spec_from_file_location('camera_ping_under_test', COMPONENT)
ping = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ping)


def sample(timestamp, name='cam1', ip='192.0.2.1', ok=True):
    return {'camera': name, 'ip': ip, 'time': ping._ping_iso(timestamp), 'ok': ok,
            'rtt_ms': 2.5 if ok else None, 'error': '' if ok else 'timeout', 'rtt_upper_bound': False}


class PingProbeTests(unittest.TestCase):
    def test_literal_addresses_only_and_invalid_destinations_never_execute(self):
        self.assertEqual(ping._ping_literal_ip('2001:db8::0001'), '2001:db8::1')
        for value in ('camera.local', '127.0.0.1', '0.0.0.0', '224.0.0.1', '::', '::1', 'fe80::1%eth0', '192.0.2.1;echo secret'):
            with self.subTest(value=value), mock.patch.object(ping._ping_subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    ping._ping_one(value)
                run.assert_not_called()

    def test_missing_ping_command_returns_an_explicit_result(self):
        with mock.patch.object(ping._ping_sys, 'platform', 'linux'), \
                mock.patch.object(ping._ping_shutil, 'which', return_value=None), \
                mock.patch.object(ping._ping_subprocess, 'run') as run:
            self.assertEqual(ping._ping_one('192.0.2.1')['error'], 'ping_unavailable')
            run.assert_not_called()

    def test_one_bounded_echo_parses_upper_bound_rtt_and_exposes_no_raw_output(self):
        response = SimpleNamespace(returncode=0, stdout='64 bytes: time<1 ms PRIVATE_OUTPUT', stderr='')
        with mock.patch.object(ping._ping_sys, 'platform', 'linux'), \
                mock.patch.object(ping._ping_shutil, 'which', return_value='/usr/bin/ping'), \
                mock.patch.object(ping._ping_subprocess, 'run', return_value=response) as run:
            value = ping._ping_one('192.0.2.1')
        self.assertEqual(value, {'ok': True, 'rtt_ms': 1.0, 'error': '', 'rtt_upper_bound': True})
        self.assertEqual(run.call_args.args[0], ['/usr/bin/ping', '-n', '-c', '1', '-W', '1', '192.0.2.1'])
        self.assertEqual(run.call_args.kwargs['timeout'], 3)
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
        self.assertNotIn('PRIVATE_OUTPUT', repr(value))

    def test_probe_errors_are_classified_without_raw_error_details(self):
        cases = [(subprocess.TimeoutExpired('dummy', 3), 'timeout'),
                 (FileNotFoundError('PRIVATE'), 'ping_unavailable'),
                 (PermissionError('PRIVATE'), 'permission_denied'), (OSError('PRIVATE'), 'probe_error')]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(ping._ping_sys, 'platform', 'linux'), \
                    mock.patch.object(ping._ping_shutil, 'which', return_value='/usr/bin/ping'), \
                    mock.patch.object(ping._ping_subprocess, 'run', side_effect=error):
                self.assertEqual(ping._ping_one('192.0.2.1'), {'ok': False, 'rtt_ms': None, 'error': expected})
        for response, expected in [(SimpleNamespace(returncode=1, stdout='', stderr=''), 'timeout'),
                                   (SimpleNamespace(returncode=2, stdout='', stderr='Operation not permitted'), 'permission_denied'),
                                   (SimpleNamespace(returncode=1, stdout='Destination Host Unreachable', stderr=''), 'unreachable')]:
            with mock.patch.object(ping._ping_sys, 'platform', 'linux'), \
                    mock.patch.object(ping._ping_shutil, 'which', return_value='/usr/bin/ping'), \
                    mock.patch.object(ping._ping_subprocess, 'run', return_value=response):
                self.assertEqual(ping._ping_one('192.0.2.1')['error'], expected)

    def test_non_linux_monitor_does_not_read_history_spawn_threads_or_probe(self):
        with mock.patch.object(ping._ping_sys, 'platform', 'win32'), \
                mock.patch.object(ping._ping_os, 'open') as opened, \
                mock.patch.object(ping._ping_threading, 'Thread') as thread, \
                mock.patch.object(ping._ping_subprocess, 'run') as run:
            monitor = ping.CameraPingMonitor(lambda: SimpleNamespace(cameras=[]), 'recording.json')
            monitor.start()
            monitor._save()
            self.assertFalse(monitor.request_check()['accepted'])
            self.assertIn('Linux', monitor.history()['notice'])
            self.assertEqual(ping._ping_one('192.0.2.1')['error'], 'ping_unavailable')
            monitor.close()
            opened.assert_not_called()
            thread.assert_not_called()
            run.assert_not_called()


class PingMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtsp-ping-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1793941200.0
        self.cameras = [SimpleNamespace(name='cam1', url='rtsp://user:PRIVATE_PASSWORD@192.0.2.1/live')]
        self.runner = mock.Mock(return_value={'ok': True, 'rtt_ms': 2.5, 'error': ''})
        self.monitor = ping.CameraPingMonitor(lambda: SimpleNamespace(cameras=self.cameras), self.root/'recording.json',
                                              runner=self.runner, clock=lambda: self.now)
        self.addCleanup(self.monitor.close)
        self.persist = mock.patch.object(self.monitor, '_save')
        self.save = self.persist.start()
        self.addCleanup(self.persist.stop)

    def test_construction_never_starts_probes_and_default_interval_is_one_minute(self):
        self.assertEqual(self.monitor.interval_seconds, 60)
        self.assertIsNone(self.monitor.thread)
        self.runner.assert_not_called()
        self.assertFalse(self.monitor.history()['monitoring'])
        for interval in (True, 0, 9, 3601, float('inf'), float('nan')):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                ping.CameraPingMonitor(lambda: None, self.root/'recording.json', interval_seconds=interval)

    def test_loop_waits_the_rest_of_the_minute_not_another_full_minute(self):
        waits = []
        def wait(timeout):
            waits.append(timeout)
            self.monitor.stop_event.set()
        with mock.patch.object(self.monitor, '_check_round') as check, \
                mock.patch.object(self.monitor.wake_event, 'wait', side_effect=wait), \
                mock.patch.object(ping._ping_time, 'monotonic', side_effect=[100.0, 104.0]):
            self.monitor._loop()
        check.assert_called_once_with()
        self.assertEqual(waits, [56.0])

    def test_manual_checks_are_rate_limited_and_do_not_queue_while_busy_or_closed(self):
        self.monitor.thread = mock.Mock()
        self.monitor.thread.is_alive.return_value = True
        self.monitor.last_probe_monotonic = 100.0
        with mock.patch.object(ping._ping_time, 'monotonic', return_value=105.0):
            self.assertEqual(self.monitor.request_check()['next_check_in'], 5.0)
        with mock.patch.object(ping._ping_time, 'monotonic', return_value=111.0):
            self.assertTrue(self.monitor.request_check()['accepted'])
            self.assertFalse(self.monitor.request_check()['accepted'])
        self.assertTrue(self.monitor.wake_event.is_set())
        self.monitor.checking = True
        with mock.patch.object(ping._ping_time, 'monotonic', return_value=200.0):
            self.assertFalse(self.monitor.request_check()['accepted'])
        self.monitor.checking = False
        self.monitor.close()
        self.assertFalse(self.monitor.request_check()['monitoring'])

    def test_only_supported_saved_ips_are_probed_and_no_credentials_are_stored(self):
        self.cameras.extend([SimpleNamespace(name='dns', url='rtsp://secret@camera.invalid/live'),
                             SimpleNamespace(name='loop', url='rtsp://127.0.0.1/live'),
                             SimpleNamespace(name='cam1', url='rtsp://192.0.2.3/live')])
        self.monitor._check_round()
        self.runner.assert_called_once_with('192.0.2.1')
        history = self.monitor.history()
        self.assertEqual(len(history['cameras']), 3)
        self.assertEqual(len(history['samples']), 1)
        self.assertNotIn('PRIVATE_PASSWORD', repr(history))
        self.assertNotIn('rtsp://', repr(history))
        self.assertFalse(history['cameras'][1]['supported'])

    def test_changed_ip_preserves_history_but_current_status_only_uses_new_ip(self):
        self.monitor._check_round()
        self.now += 60
        self.cameras = [SimpleNamespace(name='cam1', url='rtsp://different:PRIVATE@192.0.2.9/another')]
        self.monitor._check_round()
        history = self.monitor.history(camera='cam1')
        self.assertEqual([row['ip'] for row in history['samples']], ['192.0.2.1', '192.0.2.9'])
        self.assertEqual(history['cameras'][0]['latest']['ip'], '192.0.2.9')
        self.assertEqual(history['cameras'][0]['sample_count'], 1)
        self.assertEqual(history['cameras'][0]['success_count'], 1)

    def test_history_does_not_invent_results_during_shutdown_or_missing_settings(self):
        self.monitor._check_round()
        self.now += 3600
        self.monitor.close()
        self.assertEqual(len(self.monitor.history()['samples']), 1)
        self.monitor.settings_callback = mock.Mock(side_effect=ValueError('PRIVATE'))
        self.monitor._check_round()
        self.assertFalse(self.monitor.checking)
        self.assertEqual(len(self.monitor.samples), 1)
        self.assertNotIn('PRIVATE', self.monitor.notice)

    def test_probe_exception_and_unknown_fields_are_sanitized(self):
        self.runner.side_effect = RuntimeError('rtsp://user:PRIVATE@192.0.2.1/live')
        self.monitor._check_round()
        self.assertEqual(self.monitor.samples[0]['error'], 'probe_error')
        self.assertNotIn('PRIVATE', repr(self.monitor.history()))
        value = dict(sample(self.now), password='PRIVATE', url='rtsp://secret')
        self.assertNotIn('password', self.monitor._sample(value))
        self.assertNotIn('url', self.monitor._sample(value))

    def test_retention_and_sample_cap_keep_only_the_newest_day(self):
        self.monitor.samples = [sample(self.now - 90000)]
        self.monitor.samples.extend(sample(self.now - 40000 + index) for index in range(ping._PING_MAX_SAMPLES + 2))
        self.monitor.samples.append(sample(self.now + 61))
        self.monitor._prune(self.now)
        self.assertEqual(len(self.monitor.samples), ping._PING_MAX_SAMPLES)
        self.assertEqual(self.monitor.samples[0]['time'], ping._ping_iso(self.now - 39998))
        self.assertLessEqual(self.monitor.samples[-1]['time'], ping._ping_iso(self.now))

    def test_history_filters_validate_bounds(self):
        self.monitor.samples = [sample(self.now - 7200), sample(self.now - 30), sample(self.now - 20, name='cam2')]
        self.assertEqual(len(self.monitor.history(camera='cam1', hours='1')['samples']), 1)
        for hours in (0, 25, True, 'nan', 'invalid', float('inf')):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                self.monitor.history(hours=hours)
        with self.assertRaises(ValueError):
            self.monitor.history(camera='../bad')

    def test_maximum_four_in_flight_and_close_stops_later_batches(self):
        self.cameras = [SimpleNamespace(name='cam%d' % n, url='rtsp://192.0.2.%d/live' % n) for n in range(1, 13)]
        release, four_started = threading.Event(), threading.Event()
        lock = threading.Lock()
        counters = {'active': 0, 'maximum': 0, 'calls': 0}
        def probe(_ip):
            with lock:
                counters['active'] += 1
                counters['calls'] += 1
                counters['maximum'] = max(counters['maximum'], counters['active'])
                if counters['active'] == 4:
                    four_started.set()
            release.wait(timeout=2)
            with lock:
                counters['active'] -= 1
            return {'ok': True, 'rtt_ms': 1, 'error': ''}
        self.monitor.runner = probe
        try:
            with mock.patch.object(ping._ping_sys, 'platform', 'linux'):
                self.monitor.start()
                worker = self.monitor.thread
                self.monitor.start()
                self.assertIs(self.monitor.thread, worker)
                self.assertTrue(four_started.wait(timeout=2))
                self.monitor.stop_event.set()
                release.set()
                self.monitor.close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(counters, {'active': 0, 'maximum': 4, 'calls': 4})
            self.assertEqual(len(self.monitor.samples), 4)
            self.assertFalse(self.monitor.checking)
        finally:
            release.set()
            self.monitor.close()

    def test_camera_registration_is_capped_at_64(self):
        self.cameras = [SimpleNamespace(name='cam%d' % n, url='rtsp://192.0.2.%d/live' % n) for n in range(1, 66)]
        self.monitor._check_round()
        self.assertEqual(self.runner.call_count, 64)
        self.assertEqual(len(self.monitor.samples), 64)


@unittest.skipUnless(sys.platform == 'linux', 'Linux private file flags and permissions')
class PingHistoryFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rtsp-ping-file-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1793941200.0
        self.path = self.root/'recording.json'
        self.settings = lambda: SimpleNamespace(cameras=[])

    def monitor(self):
        value = ping.CameraPingMonitor(self.settings, self.path, runner=mock.Mock(), clock=lambda: self.now)
        self.addCleanup(value.close)
        return value

    def test_private_atomic_save_reload_and_prune(self):
        monitor = self.monitor()
        monitor.samples = [sample(self.now - 60)]
        monitor.path.write_text('old', encoding='utf-8')
        monitor.path.chmod(0o644)
        monitor._save()
        self.assertEqual(stat.S_IMODE(monitor.path.stat().st_mode), 0o600)
        restored = self.monitor()
        self.assertEqual(restored.samples, monitor.samples)
        self.assertEqual(restored.last_checked, monitor.samples[0]['time'])
        self.assertFalse(restored.history()['monitoring'])
        self.assertEqual(list(self.root.glob('.camera-ping-history.*.tmp')), [])
        self.now += 86401
        self.assertEqual(self.monitor().samples, [])

    def test_symlink_history_is_neither_read_nor_overwritten(self):
        victim = self.root/'private.json'
        original = b'{"keep":"PRIVATE"}'
        victim.write_bytes(original)
        history = self.root/'.camera-ping-history.json'
        history.symlink_to(victim)
        monitor = self.monitor()
        self.assertEqual(monitor.samples, [])
        self.assertTrue(monitor.notice)
        monitor.samples = [sample(self.now)]
        monitor._save()
        self.assertTrue(history.is_symlink())
        self.assertEqual(victim.read_bytes(), original)

    def test_bad_history_resets_safely_and_old_extra_fields_are_removed(self):
        history = self.root/'.camera-ping-history.json'
        history.write_text('{invalid', encoding='utf-8')
        monitor = self.monitor()
        self.assertEqual(monitor.samples, [])
        self.assertIn('손상', monitor.notice)
        history.write_text(json.dumps({'version': 1, 'samples': [dict(sample(self.now), password='PRIVATE')]}), encoding='utf-8')
        restored = self.monitor()
        self.assertEqual(restored.samples, [sample(self.now)])
        self.assertNotIn('PRIVATE', repr(restored.history()))


if __name__ == '__main__':
    unittest.main()
