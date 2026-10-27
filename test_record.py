"""Offline regression tests; no FFmpeg installation or camera connection needed.

Run: python -m unittest discover -s . -p test_record.py -v
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("scheduled_recording_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def kst(day=12, hour=7, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=record.KST)


def settings(output=None, schedules=None, **changes):
    value = record.Settings(
        output_dir=Path(output or "unused-recordings"),
        cameras=(record.Camera("cam1", "rtsp://user:PRIVATE@192.0.2.1/stream"),),
        schedules=tuple(schedules or (record.Schedule(7, 0, timedelta(hours=2)),)),
        min_free_gb=0,
    )
    return replace(value, **changes)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        logging_setup = mock.patch.object(record.logging, "basicConfig")
        logging_setup.start()
        self.addCleanup(logging_setup.stop)
        self.temp = tempfile.TemporaryDirectory(prefix=".test-config-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "recording.json"
        self.data = {
            "output_dir": "captures",
            "cameras": [{"name": "cam1", "url": "rtsp://user:PRIVATE@192.0.2.1/live"}],
            "schedules": [{"start": "07:00", "duration_hours": 2}],
        }

    def save(self):
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        return self.config

    def test_relative_paths_resolve_from_config_not_cwd(self):
        result = record.load_settings(self.save())
        self.assertEqual(result.output_dir, (self.root / "captures").resolve())
        self.assertFalse(result.output_dir.exists())
        self.assertEqual(result.retention_days, 0)

    def test_segment_defaults_to_thirty_minutes(self):
        self.assertEqual(record.load_settings(self.save()).segment_seconds, 1800)
        self.assertEqual(settings().segment_seconds, 1800)

    def test_segment_minutes_accepts_half_hour_hour_and_fractional_values(self):
        for minutes, expected_seconds in ((30, 1800), (60, 3600), (0.5, 30), (1 / 60, 1), (1440, 86400)):
            with self.subTest(minutes=minutes):
                self.data["segment_minutes"] = minutes
                self.assertEqual(record.load_settings(self.save()).segment_seconds, expected_seconds)

    def test_legacy_segment_seconds_remains_supported(self):
        self.data["segment_seconds"] = 600
        self.assertEqual(record.load_settings(self.save()).segment_seconds, 600)

    def test_segment_minutes_and_seconds_cannot_both_be_present(self):
        self.data.update(segment_minutes=30, segment_seconds=1800)
        with self.assertRaisesRegex(ValueError, "segment_minutes.*segment_seconds|segment_seconds.*segment_minutes"):
            record.load_settings(self.save())

    def test_invalid_segment_minutes_are_rejected(self):
        for minutes in (0, -1, 1 / 120, 1441, True, None, "30", float("nan"), float("inf")):
            with self.subTest(minutes=minutes):
                self.data["segment_minutes"] = minutes
                with self.assertRaises(ValueError):
                    record.load_settings(self.save())

    def test_cli_segment_minutes_overrides_config_without_writing_or_connecting(self):
        self.data["segment_minutes"] = 30
        with mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")) as check, \
                mock.patch.object(record.subprocess, "Popen") as popen, \
                mock.patch.object(record, "Storage") as storage, \
                self.assertLogs(record.LOG, level="INFO") as messages:
            result = record.main(["--config", str(self.save()), "--segment-minutes", "60", "--check"])
        self.assertEqual(result, 0)
        self.assertEqual(check.call_args.args[0].segment_seconds, 3600)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["segment_minutes"], 30)
        self.assertFalse((self.root / "captures").exists())
        popen.assert_not_called()
        storage.assert_not_called()
        logs = "\n".join(messages.output)
        self.assertRegex(logs, r"segment_minutes=60(?:\D|$)")
        self.assertNotIn("PRIVATE", logs)

    def test_cli_invalid_segment_minutes_fail_before_ffmpeg_or_camera_access(self):
        self.save()
        for minutes in ("0", "-1", "1441", "nan", "inf"):
            with self.subTest(minutes=minutes), \
                    mock.patch.object(record, "check_ffmpeg") as check, \
                    mock.patch.object(record.subprocess, "Popen") as popen, \
                    self.assertLogs(record.LOG, level="ERROR") as messages:
                result = record.main(["--config", str(self.config), "--segment-minutes", minutes, "--check"])
                self.assertEqual(result, 1)
                check.assert_not_called()
                popen.assert_not_called()
                self.assertNotIn("PRIVATE", "\n".join(messages.output))

    def test_existing_setup_ini_accepts_percent_in_password(self):
        self.data["setup_ini"] = "setup.ini"
        self.data["cameras"] = [{"name": "cam1", "section": "CAM1"}]
        (self.root / "setup.ini").write_text(
            "[CAM1]\nURL=rtsp://user:PASS%25WORD@192.0.2.1/live\n", encoding="utf-8"
        )
        result = record.load_settings(self.save())
        self.assertIn("PASS%25WORD", result.cameras[0].url)
        self.assertNotIn("PASS%25WORD", repr(result))

    def test_rejects_bad_schedules(self):
        for duration in (0, -1, True, 25, float("nan"), float("inf"), "2"):
            with self.subTest(duration=duration):
                self.data["schedules"] = [{"start": "07:00", "duration_hours": duration}]
                with self.assertRaises(ValueError):
                    record.load_settings(self.save())
        for start in ("7:00", "24:00", "12:60", "07:00:00", 700):
            with self.subTest(start=start):
                self.data["schedules"] = [{"start": start, "duration_hours": 2}]
                with self.assertRaises(ValueError):
                    record.load_settings(self.save())

    def test_rejects_duplicate_camera_names_and_missing_source(self):
        for cameras in (
            [{"name": "cam1", "url": "rtsp://192.0.2.1/live"},
             {"name": "CAM1", "url": "rtsp://192.0.2.2/live"}],
            [{"name": "cam1"}],
            [{"name": "../outside", "url": "rtsp://192.0.2.1/live"}],
            [{"name": "cam1", "url": "rtsp://192.0.2.1/live", "section": "CAM1"}],
        ):
            with self.subTest(cameras=cameras):
                self.data["cameras"] = cameras
                with self.assertRaises(ValueError):
                    record.load_settings(self.save())

    def test_rejects_filesystem_root_as_output(self):
        self.data["output_dir"] = self.root.anchor
        with self.assertRaisesRegex(ValueError, "filesystem root"):
            record.load_settings(self.save())

    def test_output_access_check_is_read_only_and_rejects_non_directory_parent(self):
        result = record.load_settings(self.save())
        record.check_output_access(result)
        self.assertFalse(result.output_dir.exists())
        parent = self.root / "not-a-directory"
        parent.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "not writable"):
            record.check_output_access(replace(result, output_dir=parent / "capture"))

    def test_output_access_check_rejects_insufficient_permissions(self):
        result = record.load_settings(self.save())
        with mock.patch.object(record.os, "access", return_value=False):
            with self.assertRaisesRegex(ValueError, "not writable"):
                record.check_output_access(result)
        self.assertFalse(result.output_dir.exists())

    def test_invalid_urls_never_expose_credentials(self):
        for url in ("https://user:PRIVATE@192.0.2.1/live",
                    "rtsp://user:PRIVATE@192.0.2.1:bad/live",
                    "rtsp://user:PRIVATE@192.0.2.1:65536/live",
                    "rtsp://user:PRIVATE@[malformed/live"):
            with self.subTest(url=url):
                self.data["cameras"][0]["url"] = url
                with self.assertLogs(record.LOG, level="ERROR") as messages:
                    result = record.main(["--config", str(self.save()), "--check"])
                self.assertEqual(result, 1)
                self.assertNotIn("PRIVATE", "\n".join(messages.output))

    def test_invalid_ini_does_not_echo_url_line(self):
        self.data["setup_ini"] = "setup.ini"
        self.data["cameras"] = [{"name": "cam1", "section": "CAM1"}]
        (self.root / "setup.ini").write_text(
            "rtsp://user:PRIVATE@192.0.2.1/live\n", encoding="utf-8"
        )
        with self.assertLogs(record.LOG, level="ERROR") as messages:
            result = record.main(["--config", str(self.save()), "--check"])
        self.assertEqual(result, 1)
        self.assertNotIn("PRIVATE", "\n".join(messages.output))
        self.assertIn("Invalid setup.ini structure", "\n".join(messages.output))

    def test_check_only_queries_local_ffmpeg_help_and_never_opens_camera(self):
        completed = subprocess.CompletedProcess([], 0, "  -timeout <int64> socket timeout\n")
        with mock.patch.object(record.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(record.subprocess, "run", return_value=completed) as run, \
                mock.patch.object(record.subprocess, "Popen") as popen, \
                mock.patch.object(record, "Storage") as storage, \
                self.assertLogs(record.LOG, level="INFO") as messages:
            result = record.main(["--config", str(self.save()), "--check"])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/ffmpeg", "-hide_banner", "-h", "demuxer=rtsp"])
        popen.assert_not_called()
        storage.assert_not_called()
        self.assertFalse((self.root / "captures").exists())
        self.assertNotIn("PRIVATE", "\n".join(messages.output))

    def test_older_ffmpeg_timeout_is_detected(self):
        completed = subprocess.CompletedProcess([], 0, "  -stimeout <int64> socket timeout\n")
        with mock.patch.object(record.shutil, "which", return_value="ffmpeg"), \
                mock.patch.object(record.subprocess, "run", return_value=completed):
            self.assertEqual(record.check_ffmpeg(settings()), ("ffmpeg", "-stimeout"))

    def test_older_ffmpeg_with_both_options_uses_socket_not_listen_timeout(self):
        completed = subprocess.CompletedProcess(
            [], 0, "  -timeout <int> listen timeout\n  -stimeout <int64> socket timeout\n"
        )
        with mock.patch.object(record.shutil, "which", return_value="ffmpeg"), \
                mock.patch.object(record.subprocess, "run", return_value=completed):
            self.assertEqual(record.check_ffmpeg(settings()), ("ffmpeg", "-stimeout"))

    def test_rejects_ffmpeg_without_rtsp_timeout_support(self):
        completed = subprocess.CompletedProcess([], 0, "Unknown format 'rtsp'.\n")
        with mock.patch.object(record.shutil, "which", return_value="ffmpeg"), \
                mock.patch.object(record.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "RTSP socket timeout"):
                record.check_ffmpeg(settings())


class ScheduleTests(unittest.TestCase):
    def test_start_inclusive_end_exclusive_in_kst(self):
        schedules = settings().schedules
        self.assertIsNone(record.active_window(kst(hour=6, minute=59, second=59), schedules))
        self.assertEqual(record.active_window(kst(), schedules), (kst(), kst(hour=9)))
        self.assertEqual(record.active_window(kst(hour=8, minute=59, second=59), schedules),
                         (kst(), kst(hour=9)))
        self.assertIsNone(record.active_window(kst(hour=9), schedules))

    def test_utc_input_and_host_timezone_do_not_change_kst_schedule(self):
        utc = datetime(2026, 10, 11, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(record.active_window(utc, settings().schedules), (kst(), kst(hour=9)))

    def test_rejects_naive_datetime(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            record.active_window(datetime(2026, 10, 12, 7), settings().schedules)

    def test_cross_midnight_window_includes_previous_day_on_restart(self):
        schedules = (record.Schedule(23, 0, timedelta(hours=3)),)
        self.assertEqual(record.active_window(kst(day=13, hour=1), schedules),
                         (kst(hour=23), kst(day=13, hour=2)))
        self.assertIsNone(record.active_window(kst(day=13, hour=2), schedules))

    def test_overlapping_and_adjacent_schedules_merge(self):
        schedules = (record.Schedule(8, 0, timedelta(hours=2)),
                     record.Schedule(7, 0, timedelta(hours=2)),
                     record.Schedule(10, 0, timedelta(minutes=30)))
        self.assertEqual(record.active_window(kst(hour=8, minute=30), schedules),
                         (kst(), kst(hour=10, minute=30)))

    def test_gaps_remain_inactive(self):
        schedules = (record.Schedule(7, 0, timedelta(hours=1)),
                     record.Schedule(9, 0, timedelta(hours=1)))
        self.assertIsNone(record.active_window(kst(hour=8, minute=30), schedules))

    def test_continuous_coverage_handles_union_and_cross_midnight(self):
        for schedules in (
            (record.Schedule(7, 0, timedelta(hours=24)),),
            (record.Schedule(7, 0, timedelta(hours=12)), record.Schedule(19, 0, timedelta(hours=12))),
            (record.Schedule(0, 0, timedelta(hours=15)), record.Schedule(10, 0, timedelta(hours=14))),
        ):
            with self.subTest(schedules=schedules):
                self.assertTrue(record.continuous_coverage(schedules))

    def test_continuous_coverage_does_not_hide_one_second_gap(self):
        schedules = (record.Schedule(0, 0, timedelta(hours=12)),
                     record.Schedule(12, 0, timedelta(hours=12) - timedelta(seconds=1)))
        self.assertFalse(record.continuous_coverage(schedules))
        self.assertIsNone(record.active_window(kst(hour=23, minute=59, second=59), schedules))


class FakeRecording:
    def __init__(self, command, run_dir):
        self.command = command
        self.run_dir = run_dir
        self.last_progress = 100
        self.code = None

    def poll(self):
        return self.code


class RecorderTests(unittest.TestCase):
    def setUp(self):
        logger = mock.patch.object(record, "LOG")
        logger.start()
        self.storage = mock.Mock(spec=record.Storage)
        self.storage.has_space.return_value = True
        self.storage.create_run.side_effect = [Path("run-1"), Path("run-2"), Path("run-3")]
        self.factory = mock.Mock(side_effect=FakeRecording)
        self.recorder = record.Recorder(settings(), "ffmpeg", "-timeout", self.storage, self.factory)
        self.stop = mock.patch.object(record, "stop_recordings").start()
        self.addCleanup(mock.patch.stopall)

    def test_restart_records_only_remaining_hour(self):
        self.recorder.tick(kst(hour=8), monotonic=100)
        command = self.factory.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "3600.000")
        self.assertEqual(Path(command[-1]), Path("run-1") /
                         "clientUnknown_cam1_UnknownIntersection_UnknownDirection_pending_%06d.mp4")
        self.assertIn("cam1", self.recorder.jobs)

    def test_overlapping_windows_do_not_start_duplicate_recorders(self):
        self.recorder.settings = settings(schedules=(
            record.Schedule(7, 0, timedelta(hours=2)),
            record.Schedule(8, 0, timedelta(hours=2)),
        ))
        self.recorder.tick(kst(hour=8), monotonic=100)
        self.recorder.tick(kst(hour=8, second=10), monotonic=110)
        self.assertEqual(self.factory.call_count, 1)
        command = self.factory.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "7200.000")

    def test_deadline_stops_even_if_input_never_makes_progress(self):
        self.recorder.tick(kst(hour=8, minute=59, second=59), monotonic=100)
        job = self.recorder.jobs["cam1"]
        self.recorder.tick(kst(hour=9), monotonic=101)
        self.assertEqual(self.recorder.jobs, {})
        self.stop.assert_called_with([job])
        self.assertEqual(self.factory.call_count, 1)

    def test_stall_retries_after_backoff_without_extending_schedule(self):
        self.recorder.tick(kst(hour=8), monotonic=100)
        job = self.recorder.jobs["cam1"]
        self.recorder.tick(kst(hour=8, second=30), monotonic=130)
        self.stop.assert_called_with([job])
        self.assertEqual(self.recorder.jobs, {})
        self.recorder.tick(kst(hour=8, second=39), monotonic=139)
        self.assertEqual(self.factory.call_count, 1)
        self.recorder.tick(kst(hour=8, second=40), monotonic=140)
        self.assertEqual(self.factory.call_count, 2)
        command = self.factory.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "3560.000")

    def test_process_exit_and_start_failure_obey_backoff(self):
        self.recorder.tick(kst(hour=8), monotonic=100)
        self.recorder.jobs["cam1"].code = 1
        self.recorder.tick(kst(hour=8, second=1), monotonic=101)
        self.factory.side_effect = OSError("simulated failure")
        self.recorder.tick(kst(hour=8, second=11), monotonic=111)
        self.recorder.tick(kst(hour=8, second=20), monotonic=120)
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.recorder.retry_at["cam1"], 121)

    def test_low_disk_pauses_existing_recording_and_resumes_within_window(self):
        self.recorder.tick(kst(hour=8), monotonic=100)
        job = self.recorder.jobs["cam1"]
        self.storage.has_space.return_value = False
        self.recorder.tick(kst(hour=8, second=5), monotonic=105)
        self.stop.assert_called_with([job])
        self.assertEqual(self.recorder.jobs, {})
        self.storage.has_space.return_value = True
        self.recorder.tick(kst(hour=8, second=15), monotonic=115)
        self.assertEqual(self.factory.call_count, 2)

    def test_cleanup_excludes_active_run(self):
        self.recorder.tick(kst(hour=8), monotonic=100)
        job = self.recorder.jobs["cam1"]
        self.recorder.next_cleanup = 0
        self.recorder.tick(kst(hour=8, second=1), monotonic=101)
        self.storage.cleanup.assert_called_with(kst(hour=8, second=1), [job.run_dir])

    def test_command_uses_stream_copy_video_only_and_fragmented_mp4_segments(self):
        value = settings(segment_seconds=1800)
        command = record.build_command(value, value.cameras[0], Path("part_%06d.mp4"),
                                       3600, "ffmpeg", "-stimeout")
        self.assertLess(command.index("-stimeout"), command.index("-i"))
        self.assertEqual(command[command.index("-stimeout") + 1], "30000000")
        for option, expected in (("-rtsp_transport", "tcp"), ("-c:v", "copy"),
                                 ("-map", "0:v:0"), ("-f", "segment"),
                                 ("-segment_format", "mp4"), ("-segment_time", "1800"),
                                 ("-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof")):
            self.assertEqual(command[command.index(option) + 1], expected)
        self.assertIn("-n", command)
        self.assertIn("-an", command)
        self.assertEqual(command[-1], "part_%06d.mp4")

    def test_continuous_recording_does_not_expire_at_artificial_day_boundary(self):
        value = settings(schedules=(record.Schedule(7, 0, timedelta(hours=24)),))
        command = record.build_command(value, value.cameras[0], Path("part_%06d.mp4"),
                                       3600, "ffmpeg", "-timeout")
        self.assertNotIn("-t", command)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-storage-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "captures"
        self.root.mkdir()
        self.now = kst(hour=12)
        self.storage = record.Storage(settings(self.root, retention_days=7))

    def make_run(self, now=None):
        return self.storage.create_run(self.storage.settings.cameras[0], now or self.now)

    def make_part(self, run, age_days=8, name="part_000000.mp4"):
        part = run / name
        part.write_bytes(b"test segment")
        when = (self.now - timedelta(days=age_days)).timestamp()
        os.utime(part, (when, when))
        return part

    def make_symlink(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError) as exc:
            self.skipTest("Symlinks unavailable on this host: {}".format(type(exc).__name__))

    def test_run_names_are_unique_even_for_same_camera_and_instant(self):
        first, second = self.make_run(), self.make_run()
        self.assertNotEqual(first, second)
        self.assertTrue(record.RUN_NAME.fullmatch(first.name))
        self.assertEqual(first.parent.name, "2026-10-12")
        self.assertEqual((first / record.RUN_MARKER).read_text(encoding="ascii"), record.MARKER_TEXT)

    def test_retention_disabled_never_deletes(self):
        self.storage.settings = replace(self.storage.settings, retention_days=0)
        part = self.make_part(self.make_run())
        self.assertEqual(self.storage.cleanup(self.now), 0)
        self.assertTrue(part.exists())

    def test_retention_deletes_only_expired_owned_segments(self):
        run = self.make_run()
        old = self.make_part(run)
        recent = self.make_part(run, age_days=1, name="part_000001.mp4")
        boundary = self.make_part(run, age_days=7, name="part_000002.mp4")
        unrelated = self.make_part(run, name="important.mp4")
        self.assertEqual(self.storage.cleanup(self.now), 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(boundary.exists())
        self.assertTrue(unrelated.exists())

    def test_retention_handles_old_mkv_and_new_mp4_without_touching_unrelated_files(self):
        run = self.make_run()
        legacy = self.make_part(run, name="part_000000.mkv")
        current = self.make_part(run, name="part_000001.mp4")
        recent = self.make_part(run, age_days=1, name="part_000002.mp4")
        unrelated = self.make_part(run, name="important.mp4")
        other_format = self.make_part(run, name="part_000003.avi")
        self.assertEqual(self.storage.cleanup(self.now), 2)
        self.assertFalse(legacy.exists())
        self.assertFalse(current.exists())
        for part in (recent, unrelated, other_format):
            self.assertTrue(part.exists())

    def test_only_open_segment_in_active_run_is_never_cleaned(self):
        run = self.make_run()
        part = self.make_part(run)
        self.assertEqual(self.storage.cleanup(self.now, [run]), 0)
        self.assertTrue(part.exists())

    def test_active_run_cleans_old_closed_segments_and_preserves_highest_index(self):
        run = self.make_run(self.now - timedelta(days=9))
        closed = self.make_part(run, name="part_000009.mp4")
        opened = self.make_part(run, name="part_000010.mp4")
        marker = run / record.RUN_MARKER
        old = (self.now - timedelta(days=9)).timestamp()
        os.utime(marker, (old, old))
        self.assertEqual(self.storage.cleanup(self.now, [run]), 1)
        self.assertFalse(closed.exists())
        self.assertTrue(opened.exists())
        self.assertTrue(marker.exists())
        self.assertTrue(run.exists())

    def test_active_mixed_format_run_preserves_current_segment_of_each_format(self):
        run = self.make_run()
        closed = [self.make_part(run, name="part_000000." + suffix) for suffix in ("mkv", "mp4")]
        opened = [self.make_part(run, name="part_000001." + suffix) for suffix in ("mkv", "mp4")]
        self.assertEqual(self.storage.cleanup(self.now, [run]), 2)
        for part in closed:
            self.assertFalse(part.exists())
        for part in opened:
            self.assertTrue(part.exists())

    def test_missing_or_invalid_markers_protect_files(self):
        for marker in (None, "not ours\n"):
            with self.subTest(marker=marker):
                run = self.make_run()
                part = self.make_part(run)
                (run / record.RUN_MARKER).unlink()
                if marker is not None:
                    (run / record.RUN_MARKER).write_text(marker, encoding="ascii")
                self.assertEqual(self.storage.cleanup(self.now), 0)
                self.assertTrue(part.exists())

    def test_closed_empty_old_run_and_day_are_removed(self):
        run = self.make_run(self.now - timedelta(days=9))
        self.make_part(run)
        marker_time = (self.now - timedelta(days=9)).timestamp()
        os.utime(run / record.RUN_MARKER, (marker_time, marker_time))
        self.assertEqual(self.storage.cleanup(self.now), 1)
        self.assertFalse(run.exists())
        self.assertFalse(run.parent.exists())

    def test_symlink_segments_do_not_delete_external_target(self):
        run = self.make_run()
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"keep me")
        link = run / "part_000000.mp4"
        self.make_symlink(link, outside)
        self.assertEqual(self.storage.cleanup(self.now), 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"keep me")

    def test_symlink_camera_is_not_traversed_or_written(self):
        outside = Path(self.temp.name) / "outside-camera"
        outside.mkdir()
        self.make_symlink(self.root / "cam1", outside)
        self.assertEqual(self.storage.cleanup(self.now), 0)
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            self.make_run()
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(sys.platform == "linux", "Linux fcntl lock")
    def test_second_recorder_cannot_acquire_same_output_root(self):
        self.storage.acquire()
        self.addCleanup(self.storage.close)
        second = record.Storage(self.storage.settings)
        self.addCleanup(second.close)
        with self.assertRaisesRegex(ValueError, "Another recorder"):
            second.acquire()


class ProcessTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Recorder process signals require Linux")
    def test_real_child_ignoring_sigint_is_killed_within_bound(self):
        code = ("import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
                "print('out_time_us=1', flush=True); time.sleep(60)")
        child = record.Recording([sys.executable, "-u", "-c", code], Path("unused-run"))
        self.addCleanup(lambda: child.process.poll() is None and child.process.kill())
        deadline = time.monotonic() + 3
        while child.last_timestamp is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(child.last_timestamp, 1, "Dummy child did not become ready")
        started = time.monotonic()
        record.stop_recordings([child])
        self.assertIsNotNone(child.poll())
        self.assertLess(time.monotonic() - started, 9)
        self.assertFalse(child.reader.is_alive())


if __name__ == "__main__":
    unittest.main()
