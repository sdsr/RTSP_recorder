"""One-time recording deadlines, CLI validation, and automatic shutdown."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("record_once_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def now():
    return datetime(2026, 10, 15, 17, 0, 24, tzinfo=record.KST)


def settings():
    return record.Settings(
        Path("unused"), (record.Camera("cam1", "rtsp://192.0.2.1/live"),),
        (record.Schedule(6, 0, timedelta(hours=5)),), recording_dates=(date(2025, 1, 1),))


class OnceWindowTests(unittest.TestCase):
    def test_immediate_duration_counts_from_seconds_not_minute_boundary(self):
        self.assertEqual(record.make_once_window(5, now=now()), (now(), now() + timedelta(minutes=5)))

    def test_duration_accepts_seconds_through_one_day_but_rejects_invalid_numbers(self):
        for minutes, seconds in ((1 / 60, 1), (0.5, 30), (1440, 86400)):
            self.assertEqual(record.make_once_window(minutes, now=now())[1] - now(), timedelta(seconds=seconds))
        for minutes in (None, "5", True, 0, -1, 0.001, 1441, float("nan"), float("inf")):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                record.make_once_window(minutes, now=now())

    def test_future_start_is_today_in_korea_and_can_end_after_midnight(self):
        start, end = record.make_once_window(5, "23:59", now().astimezone(timezone.utc))
        self.assertEqual(start, datetime(2026, 10, 15, 23, 59, tzinfo=record.KST))
        self.assertEqual(end, datetime(2026, 10, 16, 0, 4, tzinfo=record.KST))

    def test_past_start_is_rejected_instead_of_rolling_to_tomorrow(self):
        for value in ("16:59", "17:00"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "already passed today"):
                record.make_once_window(5, value, now())

    def test_bad_start_format_and_naive_clock_are_rejected(self):
        for value in ("", "9:00", "24:00", "17:60", "17:05:00", "2026-10-16T17:05"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "HH:MM"):
                record.make_once_window(5, value, now())
        with self.assertRaisesRegex(ValueError, "timezone"):
            record.make_once_window(5, now=datetime(2026, 10, 15, 17))

    def test_once_window_overrides_configured_dates_and_recurring_schedule(self):
        start = now()
        value = replace(settings(), once_window=record.make_once_window(5, now=start))
        self.assertIsNone(record.settings_window(start - timedelta(seconds=1), value))
        self.assertEqual(record.settings_window(start, value), value.once_window)
        self.assertEqual(record.settings_window(start + timedelta(minutes=4), value), value.once_window)
        self.assertIsNone(record.settings_window(start + timedelta(minutes=5), value))
        self.assertIsNone(record.settings_window(start + timedelta(days=1), value))

    def test_once_command_has_finite_limit_even_with_daily_all_day_schedule(self):
        value = replace(settings(), schedules=(record.Schedule(0, 0, timedelta(hours=24)),),
                        recording_dates=(), once_window=record.make_once_window(5, now=now()))
        command = record.build_command(value, value.cameras[0], "part_%06d.mp4", 300, "ffmpeg", "-timeout")
        self.assertEqual(command[command.index("-t") + 1], "300.000")
        self.assertEqual(command[command.index("-c:v") + 1], "copy")

    def test_runtime_waits_starts_and_stops_at_the_fixed_deadline(self):
        start = now() + timedelta(minutes=1)
        value = replace(settings(), once_window=record.make_once_window(5, now=start))
        storage = mock.Mock(spec=record.Storage)
        storage.has_space.return_value = True
        storage.create_run.return_value = Path("run")
        job = mock.Mock(last_progress=100)
        job.poll.return_value = None
        factory = mock.Mock(return_value=job)
        recorder = record.Recorder(value, "ffmpeg", "-timeout", storage, factory)
        with mock.patch.object(record, "stop_recordings") as stop, mock.patch.object(record, "LOG"):
            recorder.tick(start - timedelta(seconds=1), monotonic=99)
            factory.assert_not_called()
            recorder.tick(start, monotonic=100)
            command = factory.call_args.args[0]
            self.assertEqual(command[command.index("-t") + 1], "300.000")
            recorder.tick(start + timedelta(minutes=5), monotonic=400)
            stop.assert_called_with([job])
            recorder.tick(start + timedelta(days=1), monotonic=401)
        self.assertFalse(recorder.jobs)
        self.assertEqual(factory.call_count, 1)

    def test_reconnect_uses_only_remaining_time_without_extending_the_run(self):
        value = replace(settings(), once_window=record.make_once_window(5, now=now()))
        storage = mock.Mock(spec=record.Storage)
        storage.has_space.return_value = True
        storage.create_run.return_value = Path("run")
        job = mock.Mock(last_progress=100)
        job.poll.return_value = None
        factory = mock.Mock(return_value=job)
        recorder = record.Recorder(value, "ffmpeg", "-timeout", storage, factory)
        with mock.patch.object(record, "stop_recordings"), mock.patch.object(record, "LOG"):
            recorder.tick(now(), monotonic=100)
            job.poll.return_value = 1
            recorder.tick(now() + timedelta(seconds=2), monotonic=102)
            recorder.tick(now() + timedelta(seconds=12), monotonic=112)
        command = factory.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "288.000")
        self.assertEqual(factory.call_count, 2)


class OnceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-once-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "recording.json"
        self.config.write_text(json.dumps({
            "output_dir": "captures", "intersection_name": "시험교차로", "weather": "비",
            "recording_dates": ["2025-01-01"], "time_ranges": ["06:00-11:00"],
            "cameras": [{"name": "cam1", "url": "rtsp://user:PRIVATE@192.0.2.1/live"}],
        }, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(record.logging, "basicConfig").start()

    def test_json_once_check_preserves_metadata_and_file_without_camera_or_output_writes(self):
        before = self.config.read_bytes()
        with mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")) as ffmpeg, \
                mock.patch.object(record, "Storage") as storage, \
                mock.patch.object(record.subprocess, "Popen") as process, \
                mock.patch.object(record, "datetime", wraps=datetime) as clock, \
                self.assertLogs(record.LOG, level="INFO") as messages:
            clock.now.return_value = now()
            result = record.main(["--config", str(self.config), "--record-minutes", "5", "--check"])
        self.assertEqual(result, 0)
        value = ffmpeg.call_args.args[0]
        self.assertEqual(value.once_window, (now(), now() + timedelta(minutes=5)))
        self.assertEqual((value.cameras[0].intersection, value.cameras[0].weather), ("시험교차로", "비"))
        self.assertEqual(value.output_dir, self.root / "captures")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(value.output_dir.exists())
        storage.assert_not_called()
        process.assert_not_called()
        self.assertIn("configured schedule is ignored", "\n".join(messages.output))
        self.assertNotIn("PRIVATE", "\n".join(messages.output))

    def test_setup_ini_can_be_selected_without_json_for_one_time_recording(self):
        setup = self.root / "setup.ini"
        setup.write_text("[CAM1]\nINUSE=True\nURL=rtsp://192.0.2.1/live\n", encoding="utf-8")
        with mock.patch.object(record, "default_ini_path", return_value=self.root / "absent.ini"), \
                mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")) as ffmpeg, \
                mock.patch.object(record, "LOG"):
            self.assertEqual(record.main(["--setup-ini", str(setup), "--record-minutes", "5", "--check"]), 0)
        self.assertEqual(ffmpeg.call_args.args[0].cameras[0].name, "cam1")
        self.assertEqual(ffmpeg.call_args.args[0].once_window[1] - ffmpeg.call_args.args[0].once_window[0],
                         timedelta(minutes=5))

    def test_start_at_requires_minutes_and_incompatible_operations_fail_before_loading(self):
        cases = [["--start-at", "17:30"], ["--record-minutes", "0"], ["--record-minutes", "nan"]]
        cases += [["--record-minutes", "5", option] for option in
                  ("--install", "--print-unit", "--uninstall", "--init-config")]
        cases += [["--record-minutes", "5"] + option for option in
                  (["--time-ranges", "17:00-18:00"], ["--start-times", "17:00"], ["--duration-hours", "1"])]
        for args in cases:
            with self.subTest(args=args), \
                    mock.patch.object(record, "load_settings") as load, \
                    mock.patch.object(record, "install_service") as install, \
                    mock.patch.object(record, "create_json_config") as create, \
                    self.assertLogs(record.LOG, level="ERROR"):
                self.assertEqual(record.main(["--config", str(self.config)] + args), 1)
            load.assert_not_called()
            install.assert_not_called()
            create.assert_not_called()

    def test_main_counts_duration_after_preflight_and_exits_closing_recorder_and_storage(self):
        ready = now() + timedelta(minutes=1)
        with mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")), \
                mock.patch.object(record.sys, "platform", "linux"), \
                mock.patch.object(record.signal, "signal"), \
                mock.patch.object(record.threading, "Event") as event, \
                mock.patch.object(record, "Storage") as storage, \
                mock.patch.object(record, "Recorder") as recorder, \
                mock.patch.object(record, "write_recorder_status"), \
                mock.patch.object(record, "datetime", wraps=datetime) as clock, \
                self.assertLogs(record.LOG, level="INFO") as messages:
            clock.now.side_effect = [now(), ready, ready + timedelta(minutes=5)]
            event.return_value.is_set.return_value = False
            self.assertEqual(record.main(["--config", str(self.config), "--record-minutes", "5"]), 0)
        value = recorder.call_args.args[0]
        self.assertEqual(value.once_window, (ready, ready + timedelta(minutes=5)))
        recorder.return_value.close.assert_called_once_with()
        storage.return_value.close.assert_called_once_with()
        event.return_value.wait.assert_not_called()
        self.assertIn("One-time recording window ended", "\n".join(messages.output))

    def test_explicit_start_missed_during_startup_is_not_shifted_and_releases_storage(self):
        for initial, ready in (
                (now(), now().replace(hour=17, minute=5, second=1)),
                (now().replace(hour=23, minute=58), now().replace(day=16, hour=0, minute=0))):
            target = "17:05" if initial.hour == 17 else "23:59"
            with self.subTest(target=target), \
                    mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")), \
                    mock.patch.object(record.sys, "platform", "linux"), \
                    mock.patch.object(record.signal, "signal"), \
                    mock.patch.object(record, "Storage") as storage, \
                    mock.patch.object(record, "Recorder") as recorder, \
                    mock.patch.object(record, "datetime", wraps=datetime) as clock, \
                    self.assertLogs(record.LOG, level="ERROR") as messages:
                clock.now.side_effect = [initial, ready]
                result = record.main(["--config", str(self.config), "--record-minutes", "5", "--start-at", target])
            self.assertEqual(result, 1)
            recorder.assert_not_called()
            storage.return_value.close.assert_called_once_with()
            self.assertIn("passed during startup", "\n".join(messages.output))


if __name__ == "__main__":
    unittest.main()
