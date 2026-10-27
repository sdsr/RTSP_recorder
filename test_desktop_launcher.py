"""Desktop integration checks; no user application menu or real camera is changed."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("record.py").resolve()
SPEC = importlib.util.spec_from_file_location("desktop_launcher_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-desktop-launcher-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "recording.json"
        self.xdg = self.root / "data home"
        self.target = self.xdg / "applications" / "ed-camera-recorder.desktop"
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.xdg)}).start()
        mock.patch.object(record.logging, "basicConfig").start()
        mock.patch.object(record, "show_desktop_error", create=True).start()

    def linux(self, uid=1000):
        mock.patch.object(record.sys, "platform", "linux").start()
        mock.patch.object(record.os, "geteuid", return_value=uid, create=True).start()
        mock.patch.object(record, "require_desktop_tk", return_value=None).start()

    def test_install_is_user_local_and_does_not_start_or_change_config(self):
        self.linux()
        original = b'{"intersection_name":"keep"}\n'
        self.config.write_bytes(original)
        with mock.patch.object(record.subprocess, "Popen") as popen, \
                mock.patch.object(record, "install_service") as service:
            result = record.install_desktop_shortcut(self.config)
        self.assertEqual(Path(result["path"]), self.target)
        entry = self.target.read_text(encoding="utf-8")
        self.assertIn("Terminal=false", entry)
        self.assertIn("--gui", entry)
        self.assertNotIn("--ui-autostart", entry)
        self.assertNotIn("--install", entry)
        self.assertEqual(self.config.read_bytes(), original)
        popen.assert_not_called()
        service.assert_not_called()

    def test_reinstall_updates_managed_shortcut_only(self):
        self.linux()
        record.install_desktop_shortcut(self.config)
        second = self.root / "second.json"
        record.install_desktop_shortcut(second)
        entry = self.target.read_text(encoding="utf-8")
        self.assertIn("second.json", entry)
        self.assertNotIn("recording.json", entry)
        self.assertFalse(second.exists())

    def test_foreign_shortcut_remains_unchanged(self):
        self.linux()
        self.target.parent.mkdir(parents=True)
        original = "[Desktop Entry]\nType=Application\nExec=another-app\n"
        self.target.write_text(original, encoding="utf-8")
        with self.assertRaises(ValueError):
            record.install_desktop_shortcut(self.config)
        self.assertEqual(self.target.read_text(encoding="utf-8"), original)

    def test_root_and_relative_data_home_cannot_publish_shortcut(self):
        self.linux(uid=0)
        with self.assertRaisesRegex(ValueError, "sudo"):
            record.install_desktop_shortcut(self.config)
        self.assertFalse(self.xdg.exists())
        with mock.patch.object(record.os, "geteuid", return_value=1000), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": "relative-data"}):
            with self.assertRaises(ValueError):
                record.install_desktop_shortcut(self.config)
        self.assertFalse(self.xdg.exists())

    def test_nonexistent_working_directory_does_not_publish_broken_icon(self):
        self.linux()
        with self.assertRaises((ValueError, OSError)):
            record.install_desktop_shortcut(self.root / "missing" / "recording.json")
        self.assertFalse(self.target.exists())

    def test_missing_tk_does_not_publish_an_unlaunchable_shortcut(self):
        self.linux()
        with mock.patch.object(record, "require_desktop_tk", side_effect=RuntimeError("install python3-tk")):
            with self.assertRaisesRegex(RuntimeError, "python3-tk"):
                record.install_desktop_shortcut(self.config)
        self.assertFalse(self.target.exists())

    def test_control_characters_cannot_inject_desktop_keys(self):
        for text in ("/tmp/bad\nTerminal=true", "/tmp/bad\rExec=other", "/tmp/bad\x00name"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                record.desktop_exec_arg(text)
        with self.assertRaises(ValueError):
            record.render_desktop_entry(self.config, python_path=self.root / "py=thon")

    @unittest.skipUnless(sys.platform == "linux", "symlink behavior is verified on Linux")
    def test_symlink_shortcut_is_never_replaced(self):
        self.linux()
        victim = self.root / "other.desktop"
        victim.write_text("keep", encoding="utf-8")
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(victim)
        with self.assertRaises(ValueError):
            record.install_desktop_shortcut(self.config)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_gui_creates_defaults_without_connecting_or_recording(self):
        with mock.patch.object(record, "run_recorder_gui") as gui, \
                mock.patch.object(record, "check_ffmpeg") as ffmpeg, \
                mock.patch.object(record.subprocess, "Popen") as popen:
            result = record.main(["--gui", "--config", str(self.config)])
        self.assertEqual(result, 0)
        gui.assert_called_once_with(self.config)
        ffmpeg.assert_not_called()
        popen.assert_not_called()
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["time_ranges"], ["06:00-11:00", "12:00-17:00", "18:00-22:00"])
        self.assertEqual(data["segment_minutes"], 30)
        self.assertFalse(data["weather_condition"]["enabled"])

    def test_existing_json_is_not_rewritten_on_gui_launch(self):
        original = b'{"custom_flag":true,"weather_condition":null}\n'
        self.config.write_bytes(original)
        with mock.patch.object(record, "run_recorder_gui") as gui:
            self.assertEqual(record.main(["--gui", "--config", str(self.config)]), 0)
        gui.assert_called_once()
        self.assertEqual(self.config.read_bytes(), original)

    def test_gui_rejects_ini_and_recording_or_service_overrides(self):
        combinations = (["--config", str(self.root / "recording.ini")], ["--record-minutes", "5"],
                        ["--install"], ["--ui"], ["--check"], ["--time-ranges", "08:00-09:00"])
        for argv in combinations:
            with self.subTest(argv=argv), mock.patch.object(record, "run_recorder_gui") as gui, \
                    mock.patch.object(record, "install_service") as service, self.assertLogs(record.LOG, "ERROR"):
                self.assertEqual(record.main(["--gui"] + list(argv)), 1)
            gui.assert_not_called()
            service.assert_not_called()

    def test_desktop_install_cli_does_not_open_gui(self):
        with mock.patch.object(record, "install_desktop_shortcut", return_value={"message": "registered"}) as install, \
                mock.patch.object(record, "run_recorder_gui") as gui, mock.patch("builtins.print"):
            self.assertEqual(record.main(["--desktop-install", "--config", str(self.config)]), 0)
        install.assert_called_once_with(self.config)
        gui.assert_not_called()
        self.assertFalse(self.config.exists())

    def test_single_copied_file_imports_without_tk_or_sidecar_components(self):
        copied = self.root / "single.py"
        shutil.copyfile(SOURCE, copied)
        code = """import importlib.util, sys
spec = importlib.util.spec_from_file_location('single', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert callable(module.run_recorder_gui)
assert callable(module.install_desktop_shortcut)
assert 'tkinter' not in sys.modules
print('single-file-ok')
"""
        result = subprocess.run([sys.executable, "-I", "-c", code, str(copied)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("single-file-ok", result.stdout)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("gio"), "requires Linux gio launcher")
    def test_real_gio_launch_round_trips_special_paths_and_working_directory(self):
        # This executes a harmless argument collector through the actual desktop
        # parser, so it also covers the two independent backslash escape layers.
        folder = self.root / '교차로 spaces %f $cash `tick` "quote" \\slash'
        folder.mkdir()
        script = folder / "capture script.py"
        result_path = self.root / "captured.json"
        script.write_text("import json, os, sys\nfrom pathlib import Path\n"
                          "Path(os.environ['ED_DESKTOP_TEST_RESULT']).write_text("
                          "json.dumps({'argv': sys.argv, 'cwd': os.getcwd()}), encoding='utf-8')\n",
                          encoding="utf-8")
        config = folder / "recording %U $name.json"
        entry = self.root / "test.desktop"
        entry.write_text(record.render_desktop_entry(config, script_path=script,
                                                    python_path=sys.executable), encoding="utf-8")
        env = dict(os.environ, ED_DESKTOP_TEST_RESULT=str(result_path))
        result = subprocess.run(["gio", "launch", str(entry)], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 5
        captured = None
        while time.monotonic() < deadline:
            try:
                captured = json.loads(result_path.read_text(encoding="utf-8"))
                break
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.02)
        self.assertIsNotNone(captured, result.stderr)
        self.assertEqual(captured["argv"], [str(script), "--config", str(config), "--gui"])
        self.assertEqual(captured["cwd"], str(folder))


if __name__ == "__main__":
    unittest.main()
