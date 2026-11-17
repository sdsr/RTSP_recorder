"""Whole-window recording regressions with inert processes and local fixtures."""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).with_name('record.py')
SPEC = importlib.util.spec_from_file_location('window_recorder_under_test', SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def moment(hour=8, minute=0, second=0, day=17):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=record.KST)


def settings(mode='window', **changes):
    value = record.Settings(output_dir=Path('unused'),
        cameras=(record.Camera('cam1', 'rtsp://user:TEST_SECRET@192.0.2.1/live'),
                 record.Camera('cam2', 'rtsp://user:OTHER_SECRET@192.0.2.2/live')),
        schedules=(record.Schedule(8, 0, timedelta(hours=1)), record.Schedule(9, 0, timedelta(hours=1))),
        recording_mode=mode, segment_seconds=300, min_free_gb=0)
    return replace(value, **changes)


class WindowCommandTests(unittest.TestCase):
    def test_window_command_uses_one_literal_file_finite_duration_and_fragmented_mp4(self):
        value = settings()
        target = record.output_pattern(value.cameras[0], Path('SSD 50%/recordings'), 'window')
        command = record.build_command(value, value.cameras[0], target, 7199.25, 'ffmpeg', '-timeout')
        self.assertEqual(command[command.index('-f') + 1], 'mp4')
        self.assertEqual(command[command.index('-t') + 1], '7199.250')
        self.assertEqual(command[command.index('-movflags') + 1], '+frag_keyframe+empty_moov+default_base_moof')
        self.assertEqual(command[command.index('-c:v') + 1], 'copy')
        self.assertIn('-an', command); self.assertIn('-n', command)
        self.assertNotIn('-segment_time', command)
        self.assertNotIn('-reset_timestamps', command)
        self.assertIn('50%', command[-1]); self.assertNotIn('50%%', command[-1])
        self.assertTrue(command[-1].endswith('_pending_000000.mp4'))
        self.assertNotIn('%06d', command[-1])

    def test_split_mode_retains_number_expansion_and_percent_escaping(self):
        value = settings('split')
        target = record.output_pattern(value.cameras[0], Path('SSD 50%/recordings'), 'split')
        command = record.build_command(value, value.cameras[0], target, 3600, 'ffmpeg', '-timeout')
        self.assertEqual(command[command.index('-f') + 1], 'segment')
        self.assertEqual(command[command.index('-segment_time') + 1], '300')
        self.assertIn('50%%', command[-1])
        self.assertTrue(command[-1].endswith('_pending_%06d.mp4'))

    def test_window_mode_limits_a_daily_24_hour_window_while_split_can_be_continuous(self):
        value = settings(schedules=(record.Schedule(0, 0, timedelta(hours=24)),))
        command = record.build_command(value, value.cameras[0], 'video.mp4', 3600, 'ffmpeg', '-timeout')
        self.assertEqual(command[command.index('-t') + 1], '3600.000')
        split = record.build_command(replace(value, recording_mode='split'), value.cameras[0], 'part_%06d.mp4', 3600, 'ffmpeg', '-timeout')
        self.assertNotIn('-t', split)

    def test_adjacent_windows_are_separate_but_overlap_still_merges(self):
        value = settings()
        self.assertEqual(record.settings_window(moment(8, 30), value), (moment(8), moment(9)))
        self.assertEqual(record.settings_window(moment(9), value), (moment(9), moment(10)))
        self.assertEqual(record.settings_window(moment(8, 30), replace(value, recording_mode='split')),
                         (moment(8), moment(10)))
        overlap = replace(value, schedules=(record.Schedule(8, 0, timedelta(hours=2)),
                                           record.Schedule(9, 0, timedelta(hours=2))))
        self.assertEqual(record.settings_window(moment(9, 30), overlap), (moment(8), moment(11)))

    def test_adjacent_window_boundary_closes_previous_file_and_starts_next_without_reconnect_delay(self):
        value = replace(settings(), cameras=settings().cameras[:1])
        storage = mock.Mock(spec=record.Storage)
        storage.has_space.return_value = True
        storage.create_run.side_effect = [Path('run-a'), Path('run-b')]
        first, second = mock.Mock(last_progress=100, run_dir=Path('run-a')), mock.Mock(last_progress=101, run_dir=Path('run-b'))
        first.poll.return_value = second.poll.return_value = None
        factory = mock.Mock(side_effect=[first, second])
        recorder = record.Recorder(value, 'ffmpeg', '-timeout', storage, factory=factory)
        with mock.patch.object(record, 'stop_recordings') as stop, mock.patch.object(record, 'LOG'):
            recorder.tick(moment(8, 59, 59), monotonic=100)
            command = factory.call_args.args[0]
            self.assertEqual(command[command.index('-t') + 1], '1.000')
            recorder.tick(moment(9), monotonic=101)
            self.assertEqual(factory.call_count, 2)
            stop.assert_called_with([first])
            self.assertEqual(recorder.job_windows['cam1'], (moment(9), moment(10)))
            next_command = factory.call_args.args[0]
            self.assertEqual(next_command[next_command.index('-t') + 1], '3600.000')
            self.assertTrue(next_command[-1].endswith('_pending_000000.mp4'))
            self.assertEqual(recorder.retry_at['cam1'], 101)
            recorder.close()
            self.assertEqual(recorder.job_windows, {})

    def test_once_camera_selection_is_per_run_and_does_not_change_saved_settings(self):
        value = settings()
        selected = record.select_once_camera(value, 'cam2')
        self.assertEqual([c.name for c in selected.cameras], ['cam2'])
        self.assertEqual([c.name for c in value.cameras], ['cam1', 'cam2'])
        self.assertIs(record.select_once_camera(value, None), value)
        for invalid in ('missing', '../cam1', '', 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                record.select_once_camera(value, invalid)

    def test_fixed_window_target_still_starts_filename_publication_worker(self):
        value = settings()
        target = record.output_pattern(value.cameras[0], Path('run'), 'window')
        process = mock.Mock(stdout=io.StringIO(''))
        with mock.patch.object(record.subprocess, 'Popen', return_value=process), \
                mock.patch.object(record, 'SegmentNamer') as namer:
            running = record.Recording(['ffmpeg', target], Path('run'))
            running.reader.join(timeout=2)
        namer.assert_called_once_with(Path('run'), record.camera_filename_prefix(value.cameras[0]))
        namer.return_value.start.assert_called_once()


class WindowConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='window-config-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'recording.json'
        self.data = {'output_dir':str(self.root/'videos'), 'time_ranges':['08:00-09:00'],
                     'cameras':[{'name':'cam1','url':'rtsp://192.0.2.1/live'},
                                {'name':'cam2','url':'rtsp://192.0.2.2/live'}]}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(record.logging,'basicConfig').start()

    def test_legacy_segment_fields_remain_split_and_missing_mode_defaults_to_window(self):
        self.assertEqual(record.settings_from_data(self.data, self.root).recording_mode, 'window')
        for name, length in (('segment_minutes', 5), ('segment_seconds', 300)):
            with self.subTest(field=name):
                data = dict(self.data, **{name:length})
                self.assertEqual(record.settings_from_data(data, self.root).recording_mode, 'split')
                self.assertEqual(record.settings_from_data(dict(data, recording_mode='window'), self.root).recording_mode, 'window')
        with self.assertRaisesRegex(ValueError, 'recording_mode'):
            record.settings_from_data(dict(self.data, recording_mode='bad'), self.root)

    def test_ini_legacy_segment_setting_preserves_split_and_explicit_mode_overrides(self):
        ini = self.root/'recording.ini'
        prefix = '[CAMERAS]\nsetup_ini=auto\ncameras=auto\n[RECORDING]\n'
        ini.write_text(prefix+'segment_minutes=5\n', encoding='utf-8')
        self.assertEqual(record.ini_options(ini).recording_mode, 'split')
        ini.write_text(prefix+'segment_minutes=5\nrecording_mode=window\n', encoding='utf-8')
        self.assertEqual(record.ini_options(ini).recording_mode, 'window')
        ini.write_text(prefix, encoding='utf-8')
        self.assertEqual(record.ini_options(ini).recording_mode, 'window')
        base = record.ini_options(ini)
        self.assertEqual(record.merge_ini_overrides(base, argparse.Namespace(segment_minutes=60)).recording_mode, 'split')
        explicit = argparse.Namespace(segment_minutes=60, recording_mode='window')
        self.assertEqual(record.merge_ini_overrides(base, explicit).recording_mode, 'window')

    def test_cli_mode_and_once_selection_override_only_current_run(self):
        self.config.write_text(json.dumps(dict(self.data, recording_mode='split', segment_minutes=5)), encoding='utf-8')
        original = self.config.read_bytes()
        with mock.patch.object(record,'check_ffmpeg', return_value=('ffmpeg','-timeout')) as check, \
                mock.patch.object(record.subprocess,'Popen') as popen, mock.patch.object(record,'Storage') as storage:
            status = record.main(['--config',str(self.config),'--recording-mode','window',
                                  '--segment-minutes','60','--record-minutes','5','--record-camera','cam2','--check'])
        self.assertEqual(status, 0)
        used = check.call_args.args[0]
        self.assertEqual(used.recording_mode, 'window')
        self.assertEqual(used.segment_seconds, 3600)
        self.assertEqual([c.name for c in used.cameras], ['cam2'])
        self.assertEqual(self.config.read_bytes(), original)
        popen.assert_not_called(); storage.assert_not_called()

    def test_record_camera_without_once_mode_fails_before_camera_or_ffmpeg_access(self):
        with mock.patch.object(record,'check_ffmpeg') as check, \
                mock.patch.object(record.subprocess,'Popen') as popen, self.assertLogs(record.LOG,'ERROR'):
            self.assertEqual(record.main(['--record-camera','cam1','--check']), 1)
        check.assert_not_called(); popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
