"""First-run drafts stay local until the operator registers a camera or region."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name('record.py')
SPEC = importlib.util.spec_from_file_location('first_run_under_test', SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


class FirstRunSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recorder-first-run-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'recording.json'
        self.data = {'cameras': [], 'output_dir': str(self.root / 'recordings'),
                     'time_ranges': ['06:30-07:30'], 'recording_mode': 'window',
                     'segment_minutes': 5, 'weather_condition': record.default_weather_config()}
        self.write(self.data)
        self.controller = record.RecorderUiController(self.path)
        self.addCleanup(self.controller.close)

    def write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def registered_data(self, weather=None):
        data = dict(self.data, cameras=[{'name': 'cam1', 'url': 'rtsp://chosen:SECRET@192.0.2.30/live'}])
        if weather is not None:
            data['weather_condition'] = weather
        return data

    def test_first_open_exposes_empty_direct_editor_without_mutating_configuration(self):
        original = self.path.read_bytes()
        with mock.patch.object(record, 'check_ffmpeg', side_effect=AssertionError('no FFmpeg before setup')), \
                mock.patch.object(record.subprocess, 'Popen', side_effect=AssertionError('no process before setup')):
            value = self.controller.configuration()
            state = self.controller.state()
        self.assertTrue(value['setup_required'])
        self.assertIn('카메라', value['setup_message'])
        self.assertEqual(value['config']['camera_connection']['mode'], 'direct')
        self.assertEqual(value['config']['camera_connection']['ips'], '')
        self.assertEqual(value['config']['camera_connection']['username'], '')
        self.assertEqual(value['config']['camera_connection']['password'], '')
        self.assertEqual(value['config']['weather_condition']['region'],
                         {'sido': '', 'sigungu': '', 'eupmyeondong': ''})
        self.assertTrue(state['setup_required'])
        self.assertEqual(state['cameras'], [])
        self.assertEqual(state['error'], '')
        self.assertFalse(state['weather_ready'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_empty_setup_rejects_all_camera_work_before_network_or_processes(self):
        operations = [lambda: self.controller.check(), lambda: self.controller.start('scheduled'),
                      lambda: self.controller.start('once', 1), lambda: self.controller.forecast(),
                      lambda: self.controller.live_view.start('cam1'),
                      lambda: self.controller.camera_profiles.read('cam1'),
                      lambda: self.controller.camera_snapshots.capture('cam1')]
        with mock.patch.object(record, 'check_ffmpeg', side_effect=AssertionError('no FFmpeg before setup')), \
                mock.patch.object(record.subprocess, 'Popen', side_effect=AssertionError('no process before setup')), \
                mock.patch.object(record, 'WisenetProfileClient', side_effect=AssertionError('no camera HTTP')):
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    operation()
        self.assertEqual(self.controller.camera_ping.history()['cameras'], [])

    def test_storage_and_weather_can_be_saved_as_draft_without_inventing_camera(self):
        revision = self.controller.read_config()[1]
        result = self.controller.save({'intersection_name': '사용자 입력', 'segment_minutes': 60}, revision)
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertTrue(result['setup_required'])
        self.assertEqual(saved['cameras'], [])
        self.assertEqual(saved['intersection_name'], '사용자 입력')
        self.assertEqual(saved['segment_minutes'], 60)
        self.assertEqual(saved['weather_condition']['region'], {'sido': '', 'sigungu': '', 'eupmyeondong': ''})
        with self.assertRaises(ValueError):
            self.controller.settings()

    def test_draft_validation_remains_strict_for_other_settings_and_recording(self):
        for change in ({'segment_minutes': 0}, {'time_ranges': []}, {'recording_dates': ['invalid']},
                       {'output_dir': '/'}, {'recording_mode': 'invalid'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.controller.save(change, self.controller.read_config()[1])
        with self.assertRaises(ValueError):
            record.settings_from_data(self.data, self.root)
        draft = record.settings_from_data(self.data, self.root, allow_unconfigured=True)
        self.assertEqual(draft.cameras, ())
        for bad in (None, 'bad', {}, False):
            with self.subTest(cameras=bad), self.assertRaises(ValueError):
                record.settings_from_data(dict(self.data, cameras=bad), self.root, allow_unconfigured=True)

    def test_operator_registration_finishes_setup_using_only_entered_credentials(self):
        connection = {'mode': 'direct', 'ips': '192.0.2.30', 'username': 'chosen',
                      'password': 'ENTERED_SECRET', 'path': '/profile4/media.smp', 'port': 554}
        result = self.controller.save({'camera_connection': connection}, self.controller.read_config()[1])
        self.assertFalse(result['setup_required'])
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['cameras'][0]['url'], 'rtsp://chosen:ENTERED_SECRET@192.0.2.30:554/profile4/media.smp')
        self.assertFalse(self.controller.configuration()['setup_required'])
        self.assertFalse(self.controller.state()['setup_required'])
        self.assertEqual(len(self.controller.settings().cameras), 1)

    def test_account_is_never_guessed_when_blank_or_omitted(self):
        for connection in ({'mode': 'direct', 'ips': '192.0.2.40', 'path': '/live'},
                           {'mode': 'direct', 'ips': '192.0.2.40', 'username': '', 'password': '', 'path': '/live'}):
            value = record.cameras_from_connection(connection, {'cameras': []})
            self.assertEqual(value[0]['url'], 'rtsp://192.0.2.40:554/live')
            self.assertNotIn('admin', value[0]['url'])

    def test_existing_camera_account_password_and_weather_region_are_preserved(self):
        weather = record.default_weather_config()
        weather['region'] = {'sido': '경상북도', 'sigungu': '구미시', 'eupmyeondong': ''}
        existing = self.registered_data(weather)
        self.write(existing)
        original = self.path.read_bytes()
        config = self.controller.configuration()
        self.assertFalse(config['setup_required'])
        self.assertEqual(config['config']['camera_connection']['username'], 'chosen')
        self.assertTrue(config['config']['camera_connection']['password_saved'])
        self.assertNotIn('SECRET', json.dumps(config))
        self.assertEqual(self.path.read_bytes(), original)
        self.controller.save({'intersection_name': '다른 이름'}, config['revision'])
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['cameras'], existing['cameras'])
        self.assertEqual(saved['weather_condition'], existing['weather_condition'])

    def test_disabled_unselected_region_is_valid_but_never_queried(self):
        weather = record.default_weather_config()
        condition = record.parse_weather_condition(weather, self.root)
        self.assertFalse(condition.enabled)
        self.assertEqual(condition.region, ('', '', ''))
        self.write(self.registered_data(weather))
        with mock.patch.object(record.PublicWeatherClient, 'fetch', side_effect=AssertionError('no forecast HTTP')) as fetch:
            self.assertFalse(self.controller.state()['weather_ready'])
            with self.assertRaisesRegex(ValueError, '지역'):
                self.controller.forecast()
            gate = record.WeatherGate(condition, self.root, persist=False)
            with self.assertLogs(record.LOG, level='WARNING'):
                gate.refresh()
            self.assertIn('지역', gate.error)
            fetch.assert_not_called()
        with self.assertRaises(ValueError):
            record.parse_weather_condition(dict(weather, enabled=True), self.root)

    def test_empty_draft_cannot_replace_an_active_recorder_configuration(self):
        original = self.path.read_bytes()
        with mock.patch.object(self.controller, '_running', return_value=True), \
                mock.patch.object(self.controller, '_stop') as stop, self.assertRaises(ValueError):
            self.controller.save({'intersection_name': 'change'}, self.controller.read_config()[1])
        stop.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
