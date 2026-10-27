"""INI parsing, override precedence and exclusive creation; no camera/service I/O."""

import argparse
import importlib.util
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location("ini_settings_recorder", Path(__file__).with_name("record.py"))
recorder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recorder
SPEC.loader.exec_module(recorder)


class IniSettingsTests(unittest.TestCase):
    def write_ini(self, directory, text=None):
        path = Path(directory) / "recording.ini"
        path.write_text(recorder.render_ini_config() if text is None else text, encoding="utf-8-sig")
        return path

    def test_default_location_follows_copied_script(self):
        script = Path(__file__).resolve().parent / "copied" / "record.py"
        with patch.object(recorder, "__file__", str(script)):
            self.assertEqual(recorder.default_ini_path(), script.with_name("recording.ini"))

    def test_template_and_example_need_no_setup_file(self):
        with patch.object(recorder, "find_setup_ini", side_effect=AssertionError("must not search")), \
                patch.object(recorder, "standalone_settings", side_effect=AssertionError("must not read setup")):
            text = recorder.render_ini_config()
        self.assertIn("time_ranges = 06:00-11:00, 12:00-17:00, 18:00-22:00", text)
        self.assertIn("setup_ini = auto", text)
        self.assertIn("segment_minutes = 30", text)
        self.assertNotIn("rtsp://", text)
        example = Path(__file__).with_name("recording.example.ini").read_text(encoding="utf-8")
        self.assertEqual(example, text)

    def test_bom_and_literal_percent_paths_resolve_from_ini_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            text = recorder.render_ini_config().replace("setup_ini = auto", "setup_ini = config/setup.ini")
            text = text.replace("output_dir = auto", "output_dir = recordings/100%")
            text = text.replace("ffmpeg = ffmpeg", "ffmpeg = bin/100%/ffmpeg")
            text = text.replace("cameras = auto", "cameras = cam2, CAM1")
            options = recorder.ini_options(self.write_ini(directory, text))
            self.assertEqual(options.setup_ini, (Path(directory) / "config/setup.ini").resolve())
            self.assertEqual(options.output_dir, (Path(directory) / "recordings/100%").resolve())
            self.assertEqual(options.ffmpeg, str((Path(directory) / "bin/100%/ffmpeg").resolve()))
            self.assertEqual(options.cameras, ["CAM2", "CAM1"])

    def test_unknown_options_sections_and_default_injection_rejected(self):
        base = recorder.render_ini_config()
        cases = [base + "\n[OTHER]\nkey=value\n",
                 base.replace("cameras = auto", "cameras = auto\nurl = hidden"),
                 base.replace("segment_minutes = 30", "segment_minute = 30"),
                 "[DEFAULT]\nretention_days=1\n" + base]
        with tempfile.TemporaryDirectory() as directory:
            for text in cases:
                with self.subTest(text=text[-80:]), self.assertRaises(ValueError):
                    recorder.ini_options(self.write_ini(directory, text))

    def test_ini_errors_never_echo_malformed_credentials(self):
        secret = "rtsp://user:private-password@camera/stream"
        cases = [(secret + "\n", "Invalid recording.ini structure"),
                 (recorder.render_ini_config().replace("segment_minutes = 30", "segment_minutes = " + secret),
                  "Invalid recording.ini numeric option: segment_minutes")]
        with tempfile.TemporaryDirectory() as directory:
            for text, message in cases:
                with self.subTest(message=message), self.assertRaises(ValueError) as failure:
                    recorder.ini_options(self.write_ini(directory, text))
                self.assertEqual(str(failure.exception), message)
                self.assertNotIn(secret, str(failure.exception))

    def test_ranges_cannot_mix_with_shared_duration_and_invalid_numbers_fail(self):
        base = recorder.render_ini_config()
        cases = [base.replace("segment_minutes = 30", "segment_minutes = 0"),
                 base.replace("min_free_gb = 5", "min_free_gb = nan"),
                 base.replace("retention_days = 0", "retention_days = 1.5"),
                 base.replace("stall_seconds = 30", "stall_seconds = 1"),
                 base + "start_times = 07:00\nduration_hours = 2\n"]
        with tempfile.TemporaryDirectory() as directory:
            for text in cases:
                with self.subTest(text=text[-80:]), self.assertRaises(ValueError):
                    recorder.ini_options(self.write_ini(directory, text))

    def test_shared_duration_roundtrips_one_second_and_full_day(self):
        with tempfile.TemporaryDirectory() as directory:
            for duration in (1 / 3600, 24):
                text = recorder.render_ini_config(argparse.Namespace(start_times=["23:59"], duration_hours=duration))
                options = recorder.ini_options(self.write_ini(directory, text))
                self.assertIsNone(options.time_ranges)
                self.assertEqual(options.start_times, ["23:59"])
                self.assertEqual(options.duration_hours, duration)

    def test_cli_overrides_use_cwd_and_replace_schedule_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            ini = self.write_ini(directory)
            initial = recorder.ini_options(ini)
            cwd = Path(directory) / "caller"
            overrides = argparse.Namespace(setup_ini="other/setup.ini", output_dir="output", ffmpeg="bin/ffmpeg",
                                           duration_hours=3, cameras=["CAM3"], segment_minutes=60)
            with patch.object(recorder.Path, "cwd", return_value=cwd):
                options = recorder.merge_ini_overrides(initial, overrides)
            self.assertEqual(options.setup_ini, (cwd / "other/setup.ini").resolve())
            self.assertEqual(options.output_dir, (cwd / "output").resolve())
            self.assertEqual(options.ffmpeg, str((cwd / "bin/ffmpeg").resolve()))
            self.assertIsNone(options.time_ranges)
            self.assertEqual(options.start_times, ["06:00", "12:00", "18:00"])
            self.assertEqual(options.duration_hours, 3)
            self.assertEqual(options.segment_minutes, 60)
            changed = recorder.merge_ini_overrides(options, argparse.Namespace(time_ranges=["21:00-02:00"]))
            self.assertEqual(changed.time_ranges, ["21:00-02:00"])
            self.assertIsNone(changed.start_times)
            self.assertIsNone(changed.duration_hours)

    def test_load_hands_validated_settings_to_existing_camera_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            text = recorder.render_ini_config().replace("reconnect_seconds = 10", "reconnect_seconds = 12")
            text = text.replace("stall_seconds = 30", "stall_seconds = 45")
            path = self.write_ini(directory, text)
            with patch.object(recorder, "standalone_settings", return_value=("settings", "setup")) as standalone:
                self.assertEqual(recorder.load_ini_settings(path), ("settings", "setup"))
            options = standalone.call_args.args[0]
            self.assertEqual(options.reconnect_seconds, 12)
            self.assertEqual(options.stall_seconds, 45)
            self.assertIsNone(options.cameras)
            self.assertIsNone(options.setup_ini)

    def test_creation_is_private_and_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.ini"
            self.assertTrue(recorder.create_ini_config(path, argparse.Namespace(segment_minutes=60)))
            original = path.read_bytes()
            self.assertIn(b"segment_minutes = 60", original)
            self.assertFalse(recorder.create_ini_config(path, argparse.Namespace(segment_minutes=-1)))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            if sys.platform == "linux":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_creation_rejects_directory_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.ini"
            path.mkdir()
            with self.assertRaises(ValueError):
                recorder.create_ini_config(path)
            path.rmdir()
            with patch.object(recorder.Path, "is_symlink", return_value=True), self.assertRaises(ValueError):
                recorder.create_ini_config(path)

    def test_failed_atomic_publication_leaves_no_partial_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.ini"
            with patch.object(recorder, "ini_publish_file", side_effect=OSError("simulated failure")), self.assertRaises(OSError):
                recorder.create_ini_config(path)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_native_publication_refuses_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "new.tmp", Path(directory) / "recording.ini"
            source.write_bytes(b"new")
            destination.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                recorder.ini_publish_file(source, destination)
            self.assertEqual(destination.read_bytes(), b"existing")
            self.assertEqual(source.read_bytes(), b"new")

    def test_file_created_by_concurrent_writer_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.ini"

            def competing_link(*args):
                path.write_bytes(b"existing user settings")
                raise FileExistsError("already created")

            with patch.object(recorder, "ini_publish_file", side_effect=competing_link):
                self.assertFalse(recorder.create_ini_config(path))
            self.assertEqual(path.read_bytes(), b"existing user settings")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_root_creation_assigns_requested_service_owner(self):
        account = Mock(pw_uid=1234, pw_gid=2345)
        password_module = Mock(getpwnam=Mock(return_value=account))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.ini"
            with patch.object(recorder.os, "geteuid", return_value=0, create=True), \
                    patch.object(recorder.os, "fchown", create=True) as chown, \
                    patch.dict(sys.modules, {"pwd": password_module}):
                self.assertTrue(recorder.create_ini_config(path, owner="camera"))
            password_module.getpwnam.assert_called_once_with("camera")
            self.assertEqual(chown.call_args.args[1:], (1234, 2345))


if __name__ == "__main__":
    unittest.main()
