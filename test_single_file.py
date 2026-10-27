"""Offline checks for deploying only record.py beside an existing project.

Temporary project copies contain no installer or JSON unless a test explicitly
requests legacy JSON mode. No camera, FFmpeg process, or service is contacted.
"""

from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("record.py").resolve()
DUMMY_INI = """[CAM1]
INUSE=1
CAMADDR=192.0.2.99
URL=rtsp://user:PRIVATE_ONE@192.0.2.1/live
[CAM2]
INUSE=0
URL=rtsp://user:PRIVATE_TWO@192.0.2.2/live
[CAM3]
INUSE=true
URL=rtsp://user:PRIVATE_THREE@192.0.2.3/live
[CAM4]
URL=rtsp://user:PRIVATE_FOUR@192.0.2.4/live
"""


class SingleFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-single-file-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.search_root = self.root / "home" / "dis"
        self.search_root.mkdir(parents=True)
        self.counter = 0

    def import_copy(self, script):
        module_name = "single_file_recorder_{}_{}".format(id(self), self.counter)
        spec = importlib.util.spec_from_file_location(module_name, script)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self.addCleanup(sys.modules.pop, module_name, None)
        spec.loader.exec_module(module)
        # Every test search is isolated; never inspect the host's /home/dis.
        module.DEFAULT_SETUP_SEARCH_ROOT = self.search_root
        return module

    def project(self, name="2025goomi", nested=False, ini_text=DUMMY_INI):
        self.counter += 1
        project = self.root / (str(self.counter) + "-layout") / name
        folder = project / "tools"
        if nested:
            folder = folder / "scheduled_recording"
        folder.mkdir(parents=True)
        script = folder / "record.py"
        shutil.copyfile(SOURCE, script)
        setup = project / "config" / "setup.ini"
        if ini_text is not None:
            setup.parent.mkdir()
            setup.write_text(ini_text, encoding="utf-8")
        module = self.import_copy(script)
        return project, setup, script, module

    def desktop_script(self):
        self.counter += 1
        script = self.root / "home" / "operator" / "Desktop" / "record.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE, script)
        return script, self.import_copy(script)

    def search_candidate(self, relative, text=DUMMY_INI):
        path = self.search_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path.resolve()

    def check(self, module, argv=()):
        with mock.patch.object(module.logging, "basicConfig"), \
                mock.patch.object(module, "check_ffmpeg", return_value=("ffmpeg", "-timeout")) as ffmpeg, \
                mock.patch.object(module.subprocess, "run") as run, \
                mock.patch.object(module.subprocess, "Popen") as popen, \
                mock.patch.object(module, "Storage") as storage, \
                mock.patch.object(module, "standalone_settings", wraps=module.standalone_settings) as standalone, \
                self.assertLogs(module.LOG, level="INFO") as messages:
            status = module.main(list(argv) + ["--check"])
        logs = "\n".join(messages.output)
        self.assertEqual(status, 0, logs)
        ffmpeg.assert_called_once()
        run.assert_not_called()
        popen.assert_not_called()
        storage.assert_not_called()
        self.assertNotIn("PRIVATE", logs)
        settings = ffmpeg.call_args.args[0]
        args = standalone.call_args.args[0] if standalone.called else None
        return settings, logs, args

    def test_single_copied_script_works_in_both_root_names_and_tool_layouts(self):
        for root_name in ("2025goomi", "2026goomi"):
            for nested in (False, True):
                with self.subTest(root_name=root_name, nested=nested):
                    project, setup, script, module = self.project(root_name, nested)
                    self.assertEqual(module.find_setup_ini(), setup.resolve())
                    self.assertEqual(module.find_setup_ini(script), setup.resolve())
                    value, _, _ = self.check(module)
                    self.assertEqual([camera.name for camera in value.cameras], ["cam1", "cam3"])
                    self.assertEqual([(s.hour, s.minute, s.duration) for s in value.schedules],
                                     [(6, 0, timedelta(hours=5)), (12, 0, timedelta(hours=5)),
                                      (18, 0, timedelta(hours=4))])
                    self.assertEqual(value.segment_seconds, 1800)
                    self.assertEqual(value.output_dir, (project / "runtime" / "scheduled_recordings").resolve())
                    self.assertFalse(value.output_dir.exists())
                    self.assertFalse(script.with_name("recording.json").exists())
                    self.assertFalse(script.with_name("install.py").exists())

    def test_default_time_ranges_have_exact_start_end_boundaries_and_gaps(self):
        _, _, _, module = self.project()
        value, _, _ = self.check(module)
        for hour, minute, second, active in (
            (0, 0, 0, False), (5, 59, 59, False), (6, 0, 0, True),
            (10, 59, 59, True), (11, 0, 0, False), (11, 59, 59, False),
            (12, 0, 0, True), (16, 59, 59, True), (17, 0, 0, False),
            (17, 59, 59, False), (18, 0, 0, True), (21, 59, 59, True),
            (22, 0, 0, False), (23, 59, 59, False),
        ):
            with self.subTest(hour=hour, minute=minute, second=second):
                now = datetime(2026, 10, 13, hour, minute, second, tzinfo=module.KST)
                self.assertEqual(module.active_window(now, value.schedules) is not None, active)

    def test_service_default_ranges_preserve_unequal_five_five_four_hour_windows(self):
        _, setup, _, module = self.project()
        value, _, args = self.check(module)
        runtime = module.service_arguments(args, value, setup.resolve())
        index = runtime.index("--time-ranges")
        self.assertEqual(runtime[index + 1:index + 4],
                         ["06:00-11:00", "12:00-17:00", "18:00-22:00"])
        self.assertNotIn("--start-times", runtime)
        self.assertNotIn("--duration-hours", runtime)
        restored, _, _ = self.check(module, runtime)
        self.assertEqual([s.duration for s in restored.schedules],
                         [timedelta(hours=5), timedelta(hours=5), timedelta(hours=4)])
        self.assertEqual(value, restored)

    def test_custom_ranges_support_different_durations_and_overnight_service_round_trip(self):
        _, setup, _, module = self.project()
        ranges = ["08:15-10:45", "23:30-01:15"]
        value, _, args = self.check(module, ["--time-ranges"] + ranges)
        self.assertEqual([(s.hour, s.minute, s.duration) for s in value.schedules],
                         [(8, 15, timedelta(hours=2.5)), (23, 30, timedelta(hours=1.75))])
        before_end = datetime(2026, 10, 14, 1, 14, 59, tzinfo=module.KST)
        at_end = datetime(2026, 10, 14, 1, 15, tzinfo=module.KST)
        self.assertEqual(module.active_window(before_end, value.schedules),
                         (datetime(2026, 10, 13, 23, 30, tzinfo=module.KST), at_end))
        self.assertIsNone(module.active_window(at_end, value.schedules))
        runtime = module.service_arguments(args, value, setup.resolve())
        index = runtime.index("--time-ranges")
        self.assertEqual(runtime[index + 1:index + 3], ranges)
        restored, _, _ = self.check(module, runtime)
        self.assertEqual(value, restored)

    def test_invalid_or_equal_time_ranges_fail_before_ffmpeg_access(self):
        _, _, _, module = self.project()
        for time_range in ("6:00-11:00", "06:00-24:00", "25:00-01:00", "06:60-11:00",
                           "06:00/11:00", "06:00-", "06:00-06:00", ""):
            with self.subTest(time_range=time_range), mock.patch.object(module.logging, "basicConfig"), \
                    mock.patch.object(module, "check_ffmpeg") as ffmpeg, \
                    self.assertLogs(module.LOG, level="ERROR") as messages:
                self.assertEqual(module.main(["--time-ranges", time_range, "--check"]), 1)
                ffmpeg.assert_not_called()
                self.assertNotIn("PRIVATE", "\n".join(messages.output))

    def test_time_ranges_cannot_mix_with_shared_start_or_duration_options(self):
        _, _, _, module = self.project()
        for flags in (["--start-times", "07:00"], ["--duration-hours", "2"],
                      ["--start-times", "07:00", "--duration-hours", "2"]):
            with self.subTest(flags=flags), mock.patch.object(module.logging, "basicConfig"), \
                    mock.patch.object(module, "check_ffmpeg") as ffmpeg, \
                    self.assertLogs(module.LOG, level="ERROR") as messages:
                self.assertEqual(module.main(["--time-ranges", "06:00-11:00"] + flags + ["--check"]), 1)
                self.assertIn("--time-ranges", "\n".join(messages.output))
                ffmpeg.assert_not_called()

    def test_custom_start_times_keep_legacy_two_hour_default(self):
        _, setup, _, module = self.project()
        value, _, args = self.check(module, ["--start-times", "06:45", "19:45"])
        self.assertEqual([(s.hour, s.minute, s.duration) for s in value.schedules],
                         [(6, 45, timedelta(hours=2)), (19, 45, timedelta(hours=2))])
        runtime = module.service_arguments(args, value, setup.resolve())
        self.assertIn("--start-times", runtime)
        self.assertNotIn("--time-ranges", runtime)
        restored, _, _ = self.check(module, runtime)
        self.assertEqual(value, restored)

    def test_duration_only_applies_common_duration_to_new_default_start_times(self):
        _, setup, _, module = self.project()
        value, _, args = self.check(module, ["--duration-hours", "3"])
        self.assertEqual([(s.hour, s.minute, s.duration) for s in value.schedules],
                         [(6, 0, timedelta(hours=3)), (12, 0, timedelta(hours=3)), (18, 0, timedelta(hours=3))])
        restored, _, _ = self.check(module, module.service_arguments(args, value, setup.resolve()))
        self.assertEqual(value, restored)

    def test_legacy_twenty_four_hour_or_subminute_schedule_survives_service_round_trip(self):
        _, setup, _, module = self.project()
        for hours in (24, 1 / 3600):
            with self.subTest(hours=hours):
                value, _, args = self.check(module, ["--start-times", "00:00", "--duration-hours", str(hours)])
                self.assertEqual(value.schedules[0].duration, timedelta(hours=hours))
                runtime = module.service_arguments(args, value, setup.resolve())
                self.assertNotIn("--time-ranges", runtime)
                restored, _, _ = self.check(module, runtime)
                self.assertEqual(value, restored)

    def test_desktop_script_discovers_project_under_home_dis(self):
        script, module = self.desktop_script()
        setup = self.search_candidate("2025goomi/config/setup.ini")
        self.assertEqual(module.find_setup_ini(), setup)
        self.assertEqual(module.find_setup_ini(script, self.search_root), setup)
        value, _, _ = self.check(module)
        self.assertEqual([camera.name for camera in value.cameras], ["cam1", "cam3"])
        self.assertEqual(value.output_dir, setup.parent.parent / "runtime" / "scheduled_recordings")
        self.assertFalse(value.output_dir.exists())
        self.assertFalse(script.with_name("recording.json").exists())
        self.assertFalse(script.with_name("install.py").exists())

    def test_fallback_accepts_setup_ini_in_arbitrary_directory(self):
        _, module = self.desktop_script()
        setup = self.search_candidate("camera-installation/settings/setup.ini")
        self.assertEqual(module.find_setup_ini(), setup)
        value, _, _ = self.check(module)
        self.assertEqual(value.output_dir, setup.parent / "runtime" / "scheduled_recordings")

    def test_multiple_fallback_candidates_report_sorted_paths_without_accessing_camera(self):
        _, module = self.desktop_script()
        later = self.search_candidate("2026goomi/config/setup.ini")
        earlier = self.search_candidate("2025goomi/config/setup.ini")
        with self.assertRaises(ValueError) as error:
            module.find_setup_ini()
        message = str(error.exception)
        self.assertIn("--setup-ini", message)
        self.assertLess(message.index(str(earlier)), message.index(str(later)))
        with mock.patch.object(module.logging, "basicConfig"), \
                mock.patch.object(module, "check_ffmpeg") as ffmpeg, \
                mock.patch.object(module.subprocess, "Popen") as popen, \
                self.assertLogs(module.LOG, level="ERROR") as messages:
            self.assertEqual(module.main(["--check"]), 1)
        ffmpeg.assert_not_called()
        popen.assert_not_called()
        self.assertNotIn("PRIVATE", "\n".join(messages.output))

    def test_fallback_skips_hidden_and_dependency_directories(self):
        _, module = self.desktop_script()
        valid = self.search_candidate("actual-project/config/setup.ini")
        for folder in (".cache", "node_modules", "venv", "env", "__pycache__"):
            self.search_candidate(folder + "/dependency/config/setup.ini")
        self.assertEqual(module.find_setup_ini(), valid)

    def test_fallback_does_not_follow_symlink_directories_or_files(self):
        _, module = self.desktop_script()
        valid = self.search_candidate("actual-project/config/setup.ini")
        outside = self.root / "outside"
        outside.mkdir()
        external = outside / "setup.ini"
        external.write_text(DUMMY_INI, encoding="utf-8")
        try:
            (self.search_root / "linked-project").symlink_to(outside, target_is_directory=True)
            (self.search_root / "setup.ini").symlink_to(external)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("Symlinks unavailable on this host: {}".format(type(exc).__name__))
        self.assertEqual(module.find_setup_ini(), valid)

    def test_explicit_setup_override_wins_over_multiple_discovery_candidates(self):
        _, module = self.desktop_script()
        chosen = self.search_candidate("2025goomi/config/setup.ini")
        self.search_candidate("2026goomi/config/setup.ini")
        with mock.patch.object(module, "find_setup_ini", side_effect=AssertionError("Discovery must not run")):
            value, _, _ = self.check(module, ["--setup-ini", str(chosen), "--cameras", "CAM2"])
        self.assertEqual([camera.name for camera in value.cameras], ["cam2"])
        self.assertEqual(value.output_dir, chosen.parent.parent / "runtime" / "scheduled_recordings")

    def test_nearby_project_takes_priority_over_fallback_candidates(self):
        _, nearby, _, module = self.project()
        self.search_candidate("fallback-one/setup.ini")
        self.search_candidate("fallback-two/setup.ini")
        self.assertEqual(module.find_setup_ini(), nearby.resolve())
        value, _, _ = self.check(module)
        self.assertEqual(value.output_dir, nearby.parent.parent / "runtime" / "scheduled_recordings")

    def test_incomplete_permission_denied_search_does_not_claim_unique_candidate(self):
        _, module = self.desktop_script()
        self.search_candidate("readable/config/setup.ini")
        blocked = self.search_root / "blocked"
        blocked.mkdir()
        scandir = module.os.scandir

        def selective_scandir(path):
            if Path(path).resolve() == blocked.resolve():
                raise PermissionError("simulated unreadable directory")
            return scandir(path)

        with mock.patch.object(module.os, "scandir", side_effect=selective_scandir):
            with self.assertRaisesRegex(ValueError, "--setup-ini"):
                module.find_setup_ini()

    def test_service_arguments_pin_discovered_setup_path_for_future_starts(self):
        _, module = self.desktop_script()
        setup = self.search_candidate("2025goomi/config/setup.ini")
        value, _, args = self.check(module)
        runtime = module.service_arguments(args, value, setup)
        self.assertEqual(runtime[runtime.index("--setup-ini") + 1], str(setup))
        self.search_candidate("2026goomi/config/setup.ini")
        with mock.patch.object(module, "find_setup_ini", side_effect=AssertionError("Do not rescan on service start")):
            restarted, _, _ = self.check(module, runtime)
        self.assertEqual(value, restarted)

    def test_default_selection_reads_url_and_excludes_disabled_or_missing_inuse(self):
        _, _, _, module = self.project()
        value, _, _ = self.check(module)
        self.assertEqual([camera.url for camera in value.cameras],
                         ["rtsp://user:PRIVATE_ONE@192.0.2.1/live", "rtsp://user:PRIVATE_THREE@192.0.2.3/live"])
        self.assertTrue(all("192.0.2.99" not in camera.url for camera in value.cameras))

    def test_explicit_camera_and_runtime_options_override_standalone_defaults(self):
        _, _, _, module = self.project()
        output = self.root / "custom-captures"
        value, logs, _ = self.check(module, [
            "--cameras", "CAM2", "CAM4", "--start-times", "06:30", "23:00",
            "--duration-hours", "3", "--segment-minutes", "60", "--output-dir", str(output),
            "--retention-days", "14", "--min-free-gb", "2.5", "--ffmpeg", "custom-ffmpeg",
        ])
        self.assertEqual([camera.name for camera in value.cameras], ["cam2", "cam4"])
        self.assertEqual([(s.hour, s.minute, s.duration) for s in value.schedules],
                         [(6, 30, timedelta(hours=3)), (23, 0, timedelta(hours=3))])
        self.assertEqual(value.segment_seconds, 3600)
        self.assertEqual(value.output_dir, output.resolve())
        self.assertEqual(value.retention_days, 14)
        self.assertEqual(value.min_free_gb, 2.5)
        self.assertEqual(value.ffmpeg, "custom-ffmpeg")
        self.assertIn("segment_minutes=60", logs)
        self.assertFalse(output.exists())

    def test_explicit_setup_ini_works_without_ancestor_config(self):
        _, _, _, module = self.project(ini_text=None)
        other = self.root / "external-setup.ini"
        other.write_text(DUMMY_INI, encoding="utf-8")
        value, _, _ = self.check(module, ["--setup-ini", str(other)])
        self.assertEqual(value.output_dir, (other.parent / "runtime" / "scheduled_recordings").resolve())
        self.assertEqual(len(value.cameras), 2)

    def test_relative_output_option_is_based_on_invocation_directory(self):
        _, _, _, module = self.project()
        relative = Path("tools") / "scheduled_recording" / self.root.name / "relative-captures"
        value, _, _ = self.check(module, ["--output-dir", str(relative)])
        self.assertEqual(value.output_dir, (Path.cwd() / relative).resolve())
        self.assertFalse(value.output_dir.exists())

    def test_nearest_ancestor_setup_wins_without_reading_other_project(self):
        project, setup, script, module = self.project(nested=True)
        sibling = project.parent / "2026goomi" / "config" / "setup.ini"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("[CAM1]\nINUSE=1\nURL=rtsp://192.0.2.200/wrong-project\n", encoding="utf-8")
        self.assertEqual(module.find_setup_ini(script), setup.resolve())
        value, _, _ = self.check(module)
        self.assertTrue(all("wrong-project" not in camera.url for camera in value.cameras))
        nearest = script.parent / "config" / "setup.ini"
        nearest.parent.mkdir()
        nearest.write_text("[CAM8]\nINUSE=1\nURL=rtsp://192.0.2.8/nearest\n", encoding="utf-8")
        self.assertEqual(module.find_setup_ini(script), nearest.resolve())

    def test_missing_setup_does_not_fall_back_to_neighboring_project(self):
        project, _, _, module = self.project(ini_text=None)
        sibling = project.parent / "2026goomi" / "config" / "setup.ini"
        sibling.parent.mkdir(parents=True)
        sibling.write_text(DUMMY_INI, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "setup.ini"):
            module.find_setup_ini()
        with mock.patch.object(module.logging, "basicConfig"), \
                mock.patch.object(module, "check_ffmpeg") as ffmpeg, \
                self.assertLogs(module.LOG, level="ERROR") as messages:
            status = module.main(["--check"])
        self.assertEqual(status, 1)
        self.assertIn("--setup-ini", "\n".join(messages.output))
        ffmpeg.assert_not_called()

    def test_disabled_only_project_requires_explicit_selection(self):
        _, _, _, module = self.project(ini_text="[CAM1]\nINUSE=0\nURL=rtsp://192.0.2.1/live\n")
        with mock.patch.object(module.logging, "basicConfig"), \
                mock.patch.object(module, "check_ffmpeg") as ffmpeg, \
                self.assertLogs(module.LOG, level="ERROR"):
            self.assertEqual(module.main(["--check"]), 1)
        ffmpeg.assert_not_called()
        value, _, _ = self.check(module, ["--cameras", "CAM1"])
        self.assertEqual([camera.name for camera in value.cameras], ["cam1"])

    def test_unrequested_adjacent_json_is_ignored(self):
        _, _, script, module = self.project()
        script.with_name("recording.json").write_text("This is not valid JSON", encoding="utf-8")
        value, _, _ = self.check(module)
        self.assertEqual([camera.name for camera in value.cameras], ["cam1", "cam3"])

    def test_explicit_legacy_json_and_segment_override_keep_working(self):
        project, setup, _, module = self.project()
        config = project / "legacy.json"
        config.write_text(json.dumps({
            "setup_ini": str(setup), "output_dir": str(project / "legacy-captures"),
            "cameras": [{"name": "legacy_cam", "section": "CAM2"}],
            "schedules": [{"start": "09:15", "duration_hours": 1.5}], "segment_seconds": 600,
        }), encoding="utf-8")
        value, _, args = self.check(module, ["--config", str(config), "--segment-minutes", "60"])
        self.assertIsNone(args)
        self.assertEqual([camera.name for camera in value.cameras], ["legacy_cam"])
        self.assertEqual(value.cameras[0].url, "rtsp://user:PRIVATE_TWO@192.0.2.2/live")
        self.assertEqual(value.schedules[0].duration, timedelta(hours=1.5))
        self.assertEqual((value.schedules[0].hour, value.schedules[0].minute), (9, 15))
        self.assertEqual(value.segment_seconds, 3600)

    def test_json_mode_rejects_standalone_flags_before_loading_config(self):
        _, _, script, module = self.project()
        config = script.with_name("legacy.json")
        config.write_text("{}", encoding="utf-8")
        for flag in (["--cameras", "CAM1"], ["--setup-ini", "elsewhere.ini"],
                     ["--start-times", "06:00"], ["--duration-hours", "3"],
                     ["--time-ranges", "06:00-11:00"],
                     ["--output-dir", "captures"], ["--retention-days", "7"],
                     ["--min-free-gb", "3"], ["--ffmpeg", "ffmpeg-custom"]):
            with self.subTest(flag=flag), mock.patch.object(module.logging, "basicConfig"), \
                    mock.patch.object(module, "check_ffmpeg") as ffmpeg, \
                    self.assertLogs(module.LOG, level="ERROR") as messages:
                self.assertEqual(module.main(["--config", str(config)] + flag + ["--check"]), 1)
                self.assertIn("--config", "\n".join(messages.output))
                ffmpeg.assert_not_called()

    def test_service_arguments_round_trip_without_url_credentials_or_extra_files(self):
        for name in ("2025goomi", "2026goomi"):
            with self.subTest(name=name):
                project, setup, script, module = self.project(name)
                value, _, args = self.check(module, [
                    "--cameras", "CAM2", "--start-times", "08:45", "23:15", "--duration-hours", "1.5",
                    "--segment-minutes", "60", "--output-dir", str(project / "capture-files"),
                    "--retention-days", "10", "--min-free-gb", "1", "--ffmpeg", "ffmpeg-test",
                ])
                runtime = module.service_arguments(args, value, setup.resolve())
                self.assertEqual(runtime[runtime.index("--setup-ini") + 1], str(setup.resolve()))
                serialized = " ".join(runtime)
                self.assertNotIn("rtsp://", serialized)
                self.assertNotIn("PRIVATE", serialized)
                self.assertNotIn("recording.json", serialized)
                self.assertNotIn("install.py", serialized)
                for operation in ("--check", "--install", "--print-unit", "--uninstall"):
                    self.assertNotIn(operation, runtime)
                repeated, _, _ = self.check(module, runtime)
                self.assertEqual(value, repeated)
                self.assertFalse(script.with_name("recording.json").exists())
                self.assertFalse(script.with_name("install.py").exists())

    def test_service_default_camera_selection_tracks_inuse_on_next_start(self):
        _, setup, _, module = self.project()
        value, _, args = self.check(module)
        runtime = module.service_arguments(args, value, setup.resolve())
        self.assertNotIn("--cameras", runtime)
        setup.write_text("[CAM2]\nINUSE=1\nURL=rtsp://192.0.2.2/live\n", encoding="utf-8")
        restarted, _, _ = self.check(module, runtime)
        self.assertEqual([camera.name for camera in restarted.cameras], ["cam2"])


if __name__ == "__main__":
    unittest.main()
