"""IP filenames retain historical camera identity without exposing RTSP credentials."""
from dataclasses import replace
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


SOURCE = Path(__file__).with_name('record.py')
SPEC = importlib.util.spec_from_file_location('ip_filename_under_test', SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def stamp():
    return datetime(2026, 11, 5, 6, 30, tzinfo=record.KST)


def basic_camera():
    return record.Camera('camera_12', 'rtsp://TEST_USER:TEST_SECRET@192.0.2.12:554/profile2/media.smp?token=TEST_TOKEN')


class IpFilenameTests(unittest.TestCase):
    def test_direct_ipv4_name_contains_source_and_omits_unknown_metadata(self):
        prefix = record.camera_filename_prefix(basic_camera())
        self.assertEqual(prefix, 'camera_12_ip192.0.2.12')
        self.assertNotIn('Unknown', prefix)
        self.assertEqual(record.recording_filename_host(prefix + '_20261105_063000_KST_000001.mp4'), '192.0.2.12')

    def test_known_client_intersection_direction_and_weather_are_preserved(self):
        camera = replace(basic_camera(), client_no='00029', intersection='부산교차로', direction='West', weather='비예보일')
        self.assertEqual(record.camera_filename_prefix(camera),
                         'camera_12_ip192.0.2.12__client29_부산교차로_West_비예보일')

    def test_only_hostname_survives_credentials_port_path_and_query(self):
        urls = ('rtsp://TEST_USER:TEST_SECRET@192.0.2.12:8554/TEST_PATH?token=TEST_TOKEN',
                'rtsp://TEST_USER:TEST%40SECRET@192.0.2.12/TEST_PATH?secret=TEST_TOKEN')
        for url in urls:
            with self.subTest(url_kind=urls.index(url)):
                prefix = record.camera_filename_prefix(replace(basic_camera(), url=url))
                self.assertEqual(prefix, 'camera_12_ip192.0.2.12')
                for secret in ('TEST_USER', 'TEST_SECRET', 'TEST_TOKEN', 'TEST_PATH', '@', '?', ':'):
                    self.assertNotIn(secret, prefix)

    def test_sanitized_ipv6_and_dns_are_safe_labels_not_raw_urls(self):
        for url, expected in (
                ('rtsp://user:secret@[2001:db8::1]:554/live', '2001-db8--1'),
                ('rtsp://user:secret@Camera.Example.test/live', 'camera.example.test')):
            with self.subTest(url=url):
                prefix = record.camera_filename_prefix(replace(basic_camera(), url=url))
                self.assertEqual(prefix, 'camera_12_ip' + expected)
                self.assertEqual(record.recording_filename_host(prefix + '_pending_000001.mp4'), expected)

    def test_maximum_camera_host_and_unicode_metadata_fit_collision_filename(self):
        camera = replace(basic_camera(), name='c' * 32, url='rtsp://' + 'h' * 63 + '/live',
                         client_no='9' * 16, intersection='교차로이름' * 50,
                         direction='North-East', weather='비예보일' * 20)
        prefix = record.camera_filename_prefix(camera)
        self.assertLessEqual(len(prefix.encode('utf-8')), 200)
        filename = prefix + '_20261105_063000_KST_r12345678_999999999.mp4'
        self.assertLessEqual(len(filename.encode('utf-8')), 255)
        self.assertEqual(filename.encode('utf-8').decode('utf-8'), filename)
        self.assertTrue(record.NAMED_PART_NAME.fullmatch(filename))
        self.assertEqual(record.part_index(Path(filename)), 999999999)
        self.assertEqual(record.recording_filename_host(filename), 'h' * 63)

    def test_new_pending_final_and_collision_names_are_managed_alongside_legacy(self):
        for name, index, host in (
                ('camera_12_ip192.0.2.12_pending_000001.mp4', 1, '192.0.2.12'),
                ('camera_12_ip192.0.2.12__client29_부산_West_20261105_063000_KST_000002.mp4', 2, '192.0.2.12'),
                ('camera_12_ip192.0.2.12_20261105_063000_KST_r12345678_000003.mp4', 3, '192.0.2.12'),
                ('client29_부산_West_20261105_063000_KST_000004.mp4', 4, ''),
                ('clientUnknown_camera_12_UnknownIntersection_UnknownDirection_pending_000005.mp4', 5, ''),
                ('part_000006.mp4', 6, '')):
            with self.subTest(name=name):
                self.assertEqual(record.part_index(Path(name)), index)
                self.assertEqual(record.recording_filename_host(name), host)

    def test_ambiguous_or_nonrecording_names_do_not_claim_a_historical_address(self):
        for name in ('camera_12_ip192.0.2.12.mp4', '../camera_12_ip192.0.2.12_pending_000001.mp4',
                     'cam_ipold__meta_ip192.0.2.12_pending_000001.mp4',
                     'camera_12_ip192.0.2.12_pending_1.mp4',
                     'camera_12_ip192.0.2.12_20261105_063000_KST_000001.mp4.bak', None):
            with self.subTest(name=name):
                self.assertEqual(record.recording_filename_host(name), '')

    def test_legacy_camera_name_containing_ip_marker_does_not_invent_a_host(self):
        for suffix in ('pending_000001.mp4', '20261105_063000_KST_000001.mp4',
                       '20261105_063000_KST_r12345678_000001.mp4'):
            name = 'clientUnknown_c_iphost__x_UnknownIntersection_UnknownDirection_' + suffix
            with self.subTest(suffix=suffix):
                self.assertEqual(record.part_index(Path(name)), 1)
                self.assertEqual(record.recording_filename_host(name), '')

    def test_new_pending_file_finalizes_without_renaming_existing_legacy_file(self):
        with tempfile.TemporaryDirectory(prefix='rtsp-ip-name-') as temporary:
            folder = Path(temporary)
            legacy = folder / 'client29_부산_West_20261001_060000_KST_000000.mp4'
            legacy.write_bytes(b'old recording')
            prefix = record.camera_filename_prefix(basic_camera())
            pending = folder / (prefix + '_pending_000000.mp4')
            pending.write_bytes(b'new recording')
            namer = record.SegmentNamer(folder, prefix)
            try:
                renamed = namer.rename_available(stamp())
                self.assertEqual(renamed, [folder / 'camera_12_ip192.0.2.12_20261105_063000_KST_000000.mp4'])
                self.assertEqual(renamed[0].read_bytes(), b'new recording')
                self.assertEqual(legacy.read_bytes(), b'old recording')
            finally:
                namer.close()

    def test_retention_cleans_old_new_and_legacy_parts_but_preserves_active_ties(self):
        with tempfile.TemporaryDirectory(prefix='rtsp-ip-retention-') as temporary:
            camera = basic_camera()
            settings = record.Settings(Path(temporary), (camera,), (record.Schedule(6, 30, timedelta(hours=1)),), retention_days=7)
            storage = record.Storage(settings)
            run = storage.create_run(camera, stamp() - timedelta(days=9))
            closed_names = ('part_000000.mp4',
                            'client29_부산_West_20261027_063000_KST_000001.mp4',
                            'camera_12_ip192.0.2.12_20261027_063000_KST_000002.mp4')
            current_names = ('camera_12_ip192.0.2.12_pending_000003.mp4',
                             'camera_12_ip192.0.2.12_20261027_063000_KST_000003.mp4',
                             'camera_12_ip192.0.2.12_20261027_063000_KST_r12345678_000003.mp4')
            old = (stamp() - timedelta(days=8)).timestamp()
            for name in closed_names + current_names + ('unrelated.mp4',):
                path = run / name
                path.write_bytes(name.encode())
                os.utime(path, (old, old))
            self.assertEqual(storage.cleanup(stamp(), [run]), len(closed_names))
            self.assertTrue(all(not (run / name).exists() for name in closed_names))
            self.assertTrue(all((run / name).is_file() for name in current_names + ('unrelated.mp4',)))


@unittest.skipUnless(sys.platform == 'linux', 'Recording library descriptor operations require Linux')
class HistoricalHostLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='rtsp-ip-library-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'videos'
        self.output.mkdir()
        self.config = self.root / 'recording.json'
        self.data = {'output_dir':str(self.output), 'time_ranges':['06:30-07:30'],
                     'cameras':[{'name':'camera_12', 'url':'rtsp://TEST_USER:TEST_SECRET@192.0.2.99/live'}]}
        self.write_config()
        self.controller = record.RecorderUiController(self.config)
        self.addCleanup(self.controller.close)
        self.name = 'camera_12_ip192.0.2.12_20261105_063000_KST_000001.mp4'
        self.video = self.output / 'camera_12/2026-11-05/run_example' / self.name
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b'historical recording bytes')

    def write_config(self):
        self.config.write_text(json.dumps(self.data), encoding='utf-8')

    def test_recorded_host_and_changed_current_host_remain_separate(self):
        first = self.controller.recordings()['items'][0]
        self.assertEqual((first['camera_host'], first['current_camera_host']), ('192.0.2.12','192.0.2.99'))
        self.data['cameras'][0]['url'] = 'rtsp://OTHER_USER:OTHER_SECRET@192.0.2.100/new?token=TEST_TOKEN'
        self.write_config()
        second = self.controller.recordings()['items'][0]
        self.assertEqual((second['camera_host'], second['current_camera_host']), ('192.0.2.12','192.0.2.100'))
        for secret in ('TEST_USER','TEST_SECRET','OTHER_USER','OTHER_SECRET','TEST_TOKEN','rtsp://'):
            self.assertNotIn(secret, json.dumps(second))
        self.assertEqual(self.video.read_bytes(), b'historical recording bytes')

    def test_old_filenames_do_not_inherit_today_host_as_historical_identity(self):
        legacy = self.video.with_name('client29_부산_West_20261001_060000_KST_000001.mp4')
        legacy.write_bytes(b'legacy bytes')
        item = next(item for item in self.controller.recordings()['items'] if item['name'] == legacy.name)
        self.assertEqual(item['camera_host'], '')
        self.assertEqual(item['current_camera_host'], '192.0.2.99')
        self.assertEqual(legacy.read_bytes(), b'legacy bytes')

    def test_legacy_ip_marker_uses_current_fallback_without_claiming_historical_host(self):
        legacy = self.video.with_name('clientUnknown_c_iphost__x_UnknownIntersection_UnknownDirection_20261001_060000_KST_000002.mp4')
        legacy.write_bytes(b'legacy recording with camera alias')
        item = next(item for item in self.controller.recordings()['items'] if item['name'] == legacy.name)
        self.assertEqual(item['camera_host'], '')
        self.assertEqual(item['current_camera_host'], '192.0.2.99')
        self.assertEqual(legacy.read_bytes(), b'legacy recording with camera alias')

    def test_removed_camera_keeps_filename_host_and_has_no_current_address(self):
        self.data['cameras'] = [{'name':'another_camera','url':'rtsp://192.0.2.77/live'}]
        self.write_config()
        item = self.controller.recordings()['items'][0]
        self.assertEqual((item['camera_host'],item['current_camera_host']), ('192.0.2.12',''))

    def test_trash_restore_preserves_historical_host_and_bytes(self):
        original = self.video.read_bytes()
        item = self.controller.recordings()['items'][0]
        moved = self.controller.trash_recording(item['id'])
        trashed = self.controller.recordings(trash=True)['items'][0]
        self.assertEqual((trashed['camera_host'],trashed['current_camera_host']), ('192.0.2.12','192.0.2.99'))
        self.assertEqual(trashed['name'],self.name)
        self.controller.restore_recording(moved['id'])
        restored = self.controller.recordings()['items'][0]
        self.assertEqual((restored['camera_host'],restored['current_camera_host']), ('192.0.2.12','192.0.2.99'))
        self.assertEqual(self.video.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
