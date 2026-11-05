"""Run the Linux launchers against an inert recorder; no cameras or services."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
LAUNCHERS = ("실행.sh", "브라우저실행.sh", "실행_GUI.sh")


@unittest.skipUnless(sys.platform == "linux", "Linux shell and file permission checks")
class LaunchScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rtsp-launch-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "녹화 도구 % folder"
        self.folder.mkdir()
        for name in LAUNCHERS:
            shutil.copyfile(HERE / name, self.folder / name)
        self.config = self.folder / "recording.json"
        self.template = self.folder / "recording.portable.example.json"
        self.template_data = b'{"segment_minutes":5,"ffmpeg":"./bin/ffmpeg","label":"TEST_ONLY"}\n'
        self.capture = self.root / "capture.json"
        self.env = dict(os.environ, RTSP_LAUNCH_TEST_CAPTURE=str(self.capture))
        (self.folder / "record.py").write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "config = Path(sys.argv[sys.argv.index('--config') + 1])\n"
            "result = {'argv':sys.argv[1:], 'config_exists':config.exists(),"
            "'config_hex':config.read_bytes().hex() if config.exists() else None}\n"
            "Path(os.environ['RTSP_LAUNCH_TEST_CAPTURE']).write_text(json.dumps(result))\n",
            encoding="utf-8")

    def bundle(self):
        self.template.write_bytes(self.template_data)
        (self.folder / "bin").mkdir()
        binary = self.folder / "bin/ffmpeg"
        binary.write_text("unused test binary", encoding="utf-8")
        binary.chmod(0o644)
        return binary

    def launch(self, name="실행.sh", args=(), env=None):
        return subprocess.run(["/bin/sh", str(self.folder / name), *args], cwd=self.root,
                              env=env or self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=10)

    def captured(self):
        return json.loads(self.capture.read_text(encoding="utf-8"))

    def test_missing_config_uses_portable_template_privately_before_launch(self):
        binary = self.bundle()
        result = self.launch(args=("--ui-port", "8766"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), self.template_data)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertTrue(binary.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(self.captured()["config_hex"], self.template_data.hex())
        self.assertEqual(self.captured()["argv"],
                         ["--config", str(self.config), "--ui", "--ui-port", "8766"])
        self.assertEqual(list(self.folder.glob(".recorder-init-*")), [])

    def test_existing_settings_are_byte_preserved_and_shared_by_all_launchers(self):
        self.bundle()
        original = b'{ "custom_camera": "USER_SETTING", "segment_minutes": 60 }\n'
        self.config.write_bytes(original)
        self.config.chmod(0o644)
        for name, mode in (("실행.sh", "--ui"), ("브라우저실행.sh", "--ui"), ("실행_GUI.sh", "--gui")):
            with self.subTest(launcher=name):
                result = self.launch(name, ("--ui-port", "8767"))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
                self.assertEqual(self.captured()["argv"],
                                 ["--config", str(self.config), mode, "--ui-port", "8767"])

    def test_gui_and_browser_aliases_initialize_missing_config_and_ffmpeg(self):
        binary = self.bundle()
        for name, mode in (("브라우저실행.sh", "--ui"), ("실행_GUI.sh", "--gui")):
            with self.subTest(launcher=name):
                if self.config.exists():
                    self.config.unlink()
                binary.chmod(0o644)
                result = self.launch(name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.config.read_bytes(), self.template_data)
                self.assertTrue(binary.stat().st_mode & stat.S_IXUSR)
                self.assertIn(mode, self.captured()["argv"])

    def test_source_only_launch_leaves_default_creation_to_the_recorder(self):
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                result = self.launch(name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.captured()["config_exists"])
                self.assertFalse(self.config.exists())

    def test_config_symlink_is_rejected_without_touching_target(self):
        self.bundle()
        victim = self.root / "private-settings.json"
        victim.write_bytes(b'{"keep":"UNCHANGED"}\n')
        victim.chmod(0o640)
        self.config.symlink_to(victim)
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("심볼릭 링크", result.stderr)
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(victim.read_bytes(), b'{"keep":"UNCHANGED"}\n')
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o640)
        self.assertFalse(self.capture.exists())

    def test_dangling_config_symlink_is_not_replaced(self):
        self.bundle()
        victim = self.root / "does-not-exist.json"
        self.config.symlink_to(victim)
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.config.is_symlink())
        self.assertFalse(victim.exists())

    def test_nonregular_config_is_rejected_without_blocking(self):
        self.bundle()
        os.mkfifo(self.config)
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(stat.S_ISFIFO(self.config.stat().st_mode))
        self.assertFalse(self.capture.exists())

    def test_invalid_template_does_not_publish_partial_settings(self):
        self.bundle()
        self.template.write_bytes(b'{"not_finished":')
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("JSON", result.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.capture.exists())
        self.assertEqual(list(self.folder.glob(".recorder-init-*")), [])

    def test_parallel_first_launches_publish_complete_settings_once(self):
        self.bundle()
        processes = []
        for index in range(4):
            env = dict(self.env, RTSP_LAUNCH_TEST_CAPTURE=str(self.root / ("capture%d.json" % index)))
            processes.append(subprocess.Popen(["/bin/sh", str(self.folder / "실행.sh")], cwd=self.root,
                                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        try:
            for process in processes:
                _, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, errors)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        self.assertEqual(self.config.read_bytes(), self.template_data)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        for index in range(4):
            result = json.loads((self.root / ("capture%d.json" % index)).read_text())
            self.assertEqual(result["config_hex"], self.template_data.hex())
        self.assertEqual(list(self.folder.glob(".recorder-init-*")), [])

    def test_missing_python_has_actionable_error(self):
        empty_path = self.root / "empty-path"
        empty_path.mkdir()
        result = self.launch(env=dict(self.env, PATH=str(empty_path)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3", result.stderr)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
