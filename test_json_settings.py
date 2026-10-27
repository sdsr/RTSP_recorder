"""JSON field overrides, dated schedules, naming, and installation regressions."""

import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("json_settings_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def kst(day, hour=0, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=record.KST)


class JsonSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-json-settings-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.setup = self.root / "config" / "setup.ini"
        self.setup.parent.mkdir()
        self.setup.write_text(
            "[CAM1]\nINUSE=True\nCLIENTNO=29\nURL=rtsp://user:PRIVATE@192.0.2.1/live\n"
            "[CAM2]\nINUSE=False\nCLIENTNO=30\nURL=rtsp://192.0.2.2/live\n", encoding="utf-8")
        for client_no in (29, 30):
            self.setup.with_name("client{}.ini".format(client_no)).write_text(
                "[INTERSECTION]\nNAME=기존교차로\nCAMDIR=4\n", encoding="utf-8")
        self.config = self.root / "recording.json"
        self.data = {
            "setup_ini": "config/setup.ini", "output_dir": "captures", "cameras": "auto",
            "intersection_name": "요트경기장", "weather": "맑음",
            "recording_dates": ["2026-10-15"],
            "time_ranges": ["06:00-11:00", "12:00-17:00", "18:00-22:00"],
        }
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(record.logging, "basicConfig").start()

    def save(self):
        self.config.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8-sig")
        return self.config

    def load(self):
        return record.load_settings(self.save())

    def test_global_labels_override_ini_and_relative_output_is_based_on_json(self):
        settings = self.load()
        camera = settings.cameras[0]
        self.assertEqual(record.camera_filename_prefix(camera), "client29_요트경기장_West_맑음")
        self.assertEqual(camera.client_no, "29")
        self.assertEqual(settings.output_dir, self.root / "captures")
        self.assertFalse(settings.output_dir.exists())
        self.assertEqual(settings.recording_dates, (date(2026, 10, 15),))
        self.assertEqual([item.duration for item in settings.schedules],
                         [timedelta(hours=5), timedelta(hours=5), timedelta(hours=4)])
        self.assertNotIn("PRIVATE", repr(settings))

    def test_empty_labels_keep_ini_name_and_original_filename_without_weather(self):
        self.data.update(intersection_name="  ", weather="", recording_dates=[])
        settings = self.load()
        self.assertEqual(record.camera_filename_prefix(settings.cameras[0]), "client29_기존교차로_West")
        self.assertEqual(settings.recording_dates, ())

    def test_camera_labels_take_precedence_and_explicit_list_can_select_disabled_camera(self):
        self.data["cameras"] = [
            {"name": "cam1", "section": "CAM1"},
            {"name": "cam2", "section": "CAM2", "intersection_name": "다른교차로", "weather": "비"},
        ]
        settings = self.load()
        self.assertEqual([record.camera_filename_prefix(camera) for camera in settings.cameras],
                         ["client29_요트경기장_West_맑음", "client30_다른교차로_West_비"])
        self.data["cameras"][1].update(intersection_name="", weather="")
        self.assertEqual(record.camera_filename_prefix(self.load().cameras[1]), "client30_기존교차로_West")

    def test_json_auto_setup_output_and_camera_selection_work_together(self):
        self.data.update(setup_ini="auto", output_dir="auto")
        with mock.patch.object(record, "find_setup_ini", return_value=self.setup) as find:
            settings = self.load()
        find.assert_called_once_with()
        self.assertEqual(settings.output_dir, self.root / "runtime" / "scheduled_recordings")
        self.assertEqual([camera.name for camera in settings.cameras], ["cam1"])

    def test_auto_setup_does_not_choose_from_multiple_candidates(self):
        self.data["setup_ini"] = "auto"
        with mock.patch.object(record, "find_setup_ini", side_effect=ValueError("Multiple setup.ini files found")):
            with self.assertRaisesRegex(ValueError, "Multiple setup.ini"):
                self.load()

    def test_auto_cameras_with_no_enabled_input_is_an_error(self):
        self.setup.write_text(self.setup.read_text(encoding="utf-8").replace("INUSE=True", "INUSE=False"),
                              encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "No enabled cameras"):
            self.load()

    def test_direct_urls_use_manual_labels_but_require_an_explicit_output_path(self):
        self.data["cameras"] = [{"name": "custom", "url": "rtsp://192.0.2.3/live"}]
        camera = self.load().cameras[0]
        self.assertEqual(record.camera_filename_prefix(camera), "clientUnknown_custom_요트경기장_UnknownDirection_맑음")
        self.data["output_dir"] = "auto"
        with self.assertRaisesRegex(ValueError, "output_dir=auto"):
            self.load()

    def test_labels_reject_non_strings_and_control_characters(self):
        for key in ("intersection_name", "weather"):
            for value in (None, 3, False, ["맑음"], "비\n맑음"):
                with self.subTest(key=key, value=value):
                    bad = dict(self.data, **{key: value})
                    with self.assertRaisesRegex(ValueError, key):
                        record.settings_from_data(bad, self.root)
                    bad = dict(self.data, cameras=[{"name": "cam1", "section": "CAM1", key: value}])
                    with self.assertRaisesRegex(ValueError, key):
                        record.settings_from_data(bad, self.root)

    def test_dates_reject_impossible_ambiguous_and_duplicate_values(self):
        for values in (None, "2026-10-15", [True], [20261015], ["2026-2-1"], ["2026-02-30"],
                       ["2026-10-15T06:00:00"], ["2026-10-15", "2026-10-15"], ["0000-01-01"]):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "recording_dates"):
                record.settings_from_data(dict(self.data, recording_dates=values), self.root)
        self.data["recording_dates"] = ["2026-10-17", "2026-10-15", "2028-02-29"]
        self.assertEqual(self.load().recording_dates, (date(2026, 10, 15), date(2026, 10, 17), date(2028, 2, 29)))

    def test_ranges_and_legacy_schedules_cannot_be_combined(self):
        self.data["schedules"] = [{"start": "06:00", "duration_hours": 5}]
        with self.assertRaisesRegex(ValueError, "time_ranges or schedules"):
            self.load()
        del self.data["time_ranges"]
        self.assertEqual(self.load().schedules, (record.Schedule(6, 0, timedelta(hours=5)),))

    def test_init_json_needs_no_setup_or_ffmpeg_and_preserves_existing_settings(self):
        with mock.patch.object(record, "find_setup_ini") as find, \
                mock.patch.object(record, "check_ffmpeg") as ffmpeg, \
                mock.patch.object(record.subprocess, "Popen") as popen, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(record.main(["--init-config", "--config", str(self.config)]), 0)
            original = self.config.read_bytes()
            self.assertEqual(record.main(["--init-config", "--config", str(self.config)]), 0)
            self.assertEqual(self.config.read_bytes(), original)
        find.assert_not_called()
        ffmpeg.assert_not_called()
        popen.assert_not_called()
        data = json.loads(original)
        self.assertEqual((data["cameras"], data["setup_ini"], data["output_dir"]), ("auto", "auto", "auto"))
        self.assertEqual(data["recording_dates"], [])
        self.assertEqual(data["time_ranges"], list(record.DEFAULT_TIME_RANGES))
        self.assertEqual(data["intersection_name"], "")
        self.assertEqual(data["weather"], "")
        with mock.patch.object(record, "find_setup_ini", return_value=self.setup):
            self.assertEqual(record.load_settings(self.config).cameras[0].intersection, "기존교차로")

    def test_generated_json_can_seed_existing_cli_options_without_reading_camera(self):
        options = argparse.Namespace(setup_ini=self.setup, output_dir=self.root / "ssd", cameras=["CAM2"],
                                     start_times=["23:00"], duration_hours=2, segment_minutes=60)
        settings = record.settings_from_data(json.loads(record.render_json_config(options)), self.root)
        self.assertEqual(settings.output_dir, self.root / "ssd")
        self.assertEqual(settings.cameras[0].name, "cam2")
        self.assertEqual(settings.segment_seconds, 3600)
        self.assertEqual(settings.schedules, (record.Schedule(23, 0, timedelta(hours=2)),))

    def test_explicit_json_check_reports_dates_and_filename_without_recording(self):
        self.save()
        with mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")) as check, \
                mock.patch.object(record.subprocess, "Popen") as popen, \
                mock.patch.object(record, "Storage") as storage, \
                self.assertLogs(record.LOG, level="INFO") as messages:
            self.assertEqual(record.main(["--config", str(self.config), "--check"]), 0)
        self.assertEqual(check.call_args.args[0].recording_dates, (date(2026, 10, 15),))
        popen.assert_not_called()
        storage.assert_not_called()
        output = "\n".join(messages.output)
        for value in (str(self.config), "recording_dates=2026-10-15", "client29_요트경기장_West_맑음"):
            self.assertIn(value, output)
        self.assertNotIn("PRIVATE", output)

    def test_json_install_references_selected_file_and_does_not_recreate_ini(self):
        self.save()
        with mock.patch.object(record.sys, "platform", "linux"), \
                mock.patch.object(record.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(record, "resolve_service_user", return_value="dis"), \
                mock.patch.object(record, "install_service") as install, \
                mock.patch.object(record, "create_ini_config") as ini, \
                mock.patch.object(record, "LOG"):
            self.assertEqual(record.main(["--config", str(self.config), "--install"]), 0)
        install.assert_called_once_with(["--config", str(self.config)], "dis")
        ini.assert_not_called()
        self.data.update(weather="비", recording_dates=["2026-10-17"])
        updated = self.load()
        self.assertEqual(updated.recording_dates, (date(2026, 10, 17),))
        self.assertTrue(record.camera_filename_prefix(updated.cameras[0]).endswith("_비"))

    def test_weather_filename_publication_and_active_retention_preserve_current_part(self):
        settings = replace(self.load(), retention_days=7)
        settings.output_dir.mkdir()
        storage = record.Storage(settings)
        camera = settings.cameras[0]
        run = storage.create_run(camera, kst(1, 6))
        prefix = record.camera_filename_prefix(camera)
        pending = run / (prefix + "_pending_000000.mp4")
        pending.write_bytes(b"video-data")
        namer = record.SegmentNamer(run, prefix)
        self.addCleanup(namer.close)
        first = namer.rename_available(kst(1, 6))[0]
        self.assertEqual(first.name, "client29_요트경기장_West_맑음_20261001_060000_KST_000000.mp4")
        self.assertEqual(first.read_bytes(), b"video-data")
        current = run / (prefix + "_pending_000001.mp4")
        current.write_bytes(b"still-writing")
        unrelated = run / "other-video.mp4"
        unrelated.write_bytes(b"keep")
        for path in (first, current, unrelated):
            os.utime(path, (kst(1).timestamp(), kst(1).timestamp()))
        self.assertEqual(storage.cleanup(kst(15), [run]), 1)
        self.assertFalse(first.exists())
        self.assertTrue(current.exists())
        self.assertTrue(unrelated.exists())

    def test_weather_and_long_unicode_names_remain_valid_safe_filename_components(self):
        self.data["cameras"] = [{"name": "a" * 32, "url": "rtsp://192.0.2.3/live"}]
        self.data.update(intersection_name="/교차로%?*" * 100, weather="비/눈%?*" * 100)
        camera = self.load().cameras[0]
        prefix = record.camera_filename_prefix(camera)
        filename = prefix + "_20261015_060000_KST_r12345678_000000.mp4"
        self.assertLessEqual(len(filename.encode("utf-8")), 255)
        self.assertTrue(record.NAMED_PART_NAME.fullmatch(filename))
        self.assertEqual(record.part_index(filename), 0)
        for char in "/\\%?*":
            self.assertNotIn(char, filename)


class DatedScheduleTests(unittest.TestCase):
    def setUp(self):
        self.dates = (date(2026, 10, 15), date(2026, 10, 17))
        self.schedules = (record.Schedule(6, 0, timedelta(hours=5)),)

    def test_selected_dates_and_start_end_boundaries(self):
        for day in (14, 16, 18):
            self.assertIsNone(record.active_window(kst(day, 7), self.schedules, self.dates))
        for day in (15, 17):
            self.assertIsNone(record.active_window(kst(day, 5, 59), self.schedules, self.dates))
            self.assertEqual(record.active_window(kst(day, 6), self.schedules, self.dates),
                             (kst(day, 6), kst(day, 11)))
            self.assertIsNone(record.active_window(kst(day, 11), self.schedules, self.dates))

    def test_empty_dates_retain_daily_behavior(self):
        self.assertEqual(record.active_window(kst(16, 7), self.schedules, ()), (kst(16, 6), kst(16, 11)))

    def test_dates_are_korean_calendar_days_even_when_now_is_utc(self):
        now = datetime(2026, 10, 14, 21, tzinfo=timezone.utc)
        self.assertEqual(record.active_window(now, self.schedules, self.dates), (kst(15, 6), kst(15, 11)))

    def test_overnight_restart_uses_selected_start_day_and_stops_at_end(self):
        schedules = (record.Schedule(23, 0, timedelta(hours=2)),)
        dates = (date(2026, 10, 15),)
        self.assertIsNone(record.active_window(kst(15, 0, 30), schedules, dates))
        self.assertEqual(record.active_window(kst(16, 0, 30), schedules, dates), (kst(15, 23), kst(16, 1)))
        self.assertIsNone(record.active_window(kst(16, 1), schedules, dates))
        self.assertIsNone(record.active_window(kst(16, 23), schedules, dates))

    def test_consecutive_all_day_dates_merge_to_real_deadline_and_keep_ffmpeg_limit(self):
        dates = tuple(date(2026, 10, day) for day in range(14, 21))
        schedules = (record.Schedule(0, 0, timedelta(hours=24)),)
        window = record.active_window(kst(14, 12), schedules, dates)
        self.assertEqual(window, (kst(14), kst(21)))
        self.assertIsNone(record.active_window(kst(21), schedules, dates))
        settings = record.Settings(Path("unused"), (record.Camera("cam1", "rtsp://192.0.2.1/live"),),
                                   schedules, recording_dates=dates)
        remaining = (window[1] - kst(14, 12)).total_seconds()
        command = record.build_command(settings, settings.cameras[0], "part_%06d.mp4", remaining, "ffmpeg", "-timeout")
        self.assertEqual(command[command.index("-t") + 1], "561600.000")

    def test_runtime_waits_outside_dates_stops_at_deadline_and_does_not_repeat_next_day(self):
        settings = record.Settings(Path("unused"), (record.Camera("cam1", "rtsp://192.0.2.1/live"),),
                                   self.schedules, recording_dates=self.dates)
        storage = mock.Mock(spec=record.Storage)
        storage.has_space.return_value = True
        storage.create_run.return_value = Path("run")
        job = mock.Mock(last_progress=100)
        job.poll.return_value = None
        factory = mock.Mock(return_value=job)
        recorder = record.Recorder(settings, "ffmpeg", "-timeout", storage, factory)
        with mock.patch.object(record, "stop_recordings") as stop, mock.patch.object(record, "LOG"):
            recorder.tick(kst(14, 7), monotonic=99)
            factory.assert_not_called()
            recorder.tick(kst(15, 10, 59), monotonic=100)
            command = factory.call_args.args[0]
            self.assertEqual(command[command.index("-t") + 1], "60.000")
            recorder.tick(kst(15, 11), monotonic=101)
            stop.assert_called_with([job])
            self.assertFalse(recorder.jobs)
            recorder.tick(kst(16, 7), monotonic=102)
            self.assertEqual(factory.call_count, 1)
            recorder.tick(kst(17, 7), monotonic=200)
            self.assertEqual(factory.call_count, 2)


if __name__ == "__main__":
    unittest.main()
