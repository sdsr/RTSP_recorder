"""Offline INI creation, reload, and service wiring tests for copied record.py."""

import configparser
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import importlib.util
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("record.py").resolve()
CAMERAS = """[CAM1]
INUSE=1
URL=rtsp://camera-user:PRIVATE_ONE@192.0.2.1/live
[CAM2]
INUSE=0
URL=rtsp://camera-user:PRIVATE_TWO@192.0.2.2/live
"""


class IniFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-ini-flow-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "2025goomi"
        self.script = self.project / "tools" / "record.py"
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(SOURCE, self.script)
        self.setup = self.project / "config" / "setup.ini"
        self.setup.parent.mkdir()
        self.setup.write_text(CAMERAS, encoding="utf-8")
        self.config = self.script.with_name("recording.ini")
        name = "ini_flow_recorder_{}".format(id(self))
        spec = importlib.util.spec_from_file_location(name, self.script)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(self.module)
        self.module.DEFAULT_SETUP_SEARCH_ROOT = self.root / "isolated-search"
        setup_logging = mock.patch.object(self.module.logging, "basicConfig")
        setup_logging.start()
        self.addCleanup(setup_logging.stop)

    def invoke(self, argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return self.module.main(argv)

    def initialize(self, argv=()):
        with mock.patch.object(self.module, "find_setup_ini", side_effect=AssertionError("Init must not discover cameras")), \
                mock.patch.object(self.module, "check_ffmpeg", side_effect=AssertionError("Init must not query FFmpeg")), \
                mock.patch.object(self.module.subprocess, "run") as run, \
                mock.patch.object(self.module.subprocess, "Popen") as popen, \
                mock.patch.object(self.module, "Storage") as storage:
            status = self.invoke(["--init-config"] + list(argv))
        self.assertEqual(status, 0)
        run.assert_not_called()
        popen.assert_not_called()
        storage.assert_not_called()

    def read_ini(self, path=None):
        parser = configparser.ConfigParser(interpolation=None)
        with (path or self.config).open(encoding="utf-8-sig") as handle:
            parser.read_file(handle)
        return parser

    def save_ini(self, parser, path=None):
        with (path or self.config).open("w", encoding="utf-8") as handle:
            parser.write(handle)

    def check(self, argv=()):
        with mock.patch.object(self.module, "check_ffmpeg", return_value=("ffmpeg", "-timeout")) as ffmpeg, \
                mock.patch.object(self.module.subprocess, "run") as run, \
                mock.patch.object(self.module.subprocess, "Popen") as popen, \
                mock.patch.object(self.module, "Storage") as storage, \
                self.assertLogs(self.module.LOG, level="INFO") as messages:
            status = self.invoke(list(argv) + ["--check"])
        self.assertEqual(status, 0, "\n".join(messages.output))
        ffmpeg.assert_called_once()
        run.assert_not_called()
        popen.assert_not_called()
        storage.assert_not_called()
        self.assertNotIn("PRIVATE", "\n".join(messages.output))
        return ffmpeg.call_args.args[0]

    def linux_service_mocks(self):
        """Install/uninstall control flow only; never call systemd or change owner."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(mock.patch.object(self.module.sys, "platform", "linux"))
        stack.enter_context(mock.patch.object(self.module.os, "geteuid", return_value=0, create=True))
        stack.enter_context(mock.patch.object(self.module, "resolve_service_user", return_value="camera-user"))
        return stack

    def test_init_creates_commented_defaults_without_setup_or_external_queries(self):
        self.setup.unlink()
        self.initialize()
        self.assertTrue(self.config.is_file())
        text = self.config.read_text(encoding="utf-8-sig")
        self.assertTrue(any(line.startswith(("#", ";")) for line in text.splitlines()))
        self.assertNotIn("rtsp://", text)
        self.assertNotIn("PRIVATE", text)
        parser = self.read_ini()
        self.assertEqual(parser.get("CAMERAS", "setup_ini"), "auto")
        self.assertEqual(parser.get("CAMERAS", "cameras"), "auto")
        self.assertEqual(parser.get("RECORDING", "time_ranges").replace(" ", ""),
                         "06:30-07:30,10:00-11:00,12:00-13:00,17:00-18:00,21:00-22:00")
        self.assertEqual(parser.getfloat("RECORDING", "segment_minutes"), 30)
        self.assertEqual(parser.get("RECORDING", "output_dir"), "auto")
        self.assertFalse((self.project / "runtime").exists())
        self.assertFalse(self.script.with_name("install.py").exists())

    def test_init_can_choose_explicit_destination_and_preserves_existing_contents(self):
        destination = self.root / "custom.ini"
        self.initialize(["--config", str(destination), "--segment-minutes", "60"])
        self.assertFalse(self.config.exists())
        self.assertEqual(self.read_ini(destination).getfloat("RECORDING", "segment_minutes"), 60)
        before = destination.read_bytes()
        self.initialize(["--config", str(destination), "--segment-minutes", "30"])
        self.assertEqual(destination.read_bytes(), before)

    def test_init_rejects_symlink_destination_without_touching_target(self):
        target = self.root / "keep.ini"
        target.write_text("keep this exact content", encoding="utf-8")
        try:
            self.config.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("Symlinks unavailable on this host: {}".format(type(exc).__name__))
        before = target.read_bytes()
        with self.assertLogs(self.module.LOG, level="ERROR"):
            self.assertEqual(self.invoke(["--init-config"]), 1)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(self.config.is_symlink())

    def test_check_without_ini_keeps_cli_defaults_and_creates_no_ini(self):
        value = self.check()
        self.assertFalse(self.config.exists())
        self.assertFalse(value.output_dir.exists())
        self.assertEqual([s.duration for s in value.schedules],
                         [timedelta(hours=1)] * 5)

    def test_adjacent_ini_changes_are_loaded_on_next_run(self):
        self.initialize()
        initial = self.check()
        self.assertEqual([camera.name for camera in initial.cameras], ["cam1"])
        parser = self.read_ini()
        parser["CAMERAS"]["cameras"] = "CAM2"
        parser["RECORDING"]["time_ranges"] = "08:30-10:00, 23:00-01:00"
        parser["RECORDING"]["segment_minutes"] = "60"
        parser["RECORDING"]["output_dir"] = "../new-captures"
        self.save_ini(parser)
        before = self.config.read_bytes()
        restarted = self.check()
        self.assertEqual([camera.name for camera in restarted.cameras], ["cam2"])
        self.assertEqual([(s.hour, s.minute, s.duration) for s in restarted.schedules],
                         [(8, 30, timedelta(hours=1.5)), (23, 0, timedelta(hours=2))])
        self.assertEqual(restarted.segment_seconds, 3600)
        self.assertEqual(restarted.output_dir, (self.project / "new-captures").resolve())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(restarted.output_dir.exists())

    def test_explicit_ini_relative_paths_are_based_on_ini_location(self):
        folder = self.root / "external-config"
        folder.mkdir()
        config = folder / "recorder.ini"
        self.initialize(["--config", str(config)])
        parser = self.read_ini(config)
        parser["CAMERAS"]["setup_ini"] = "../2025goomi/config/setup.ini"
        parser["RECORDING"]["output_dir"] = "recordings"
        self.save_ini(parser, config)
        value = self.check(["--config", str(config)])
        self.assertEqual(value.output_dir, (folder / "recordings").resolve())
        self.assertEqual(value.cameras[0].url, "rtsp://camera-user:PRIVATE_ONE@192.0.2.1/live")
        self.assertFalse(self.config.exists())

    def test_runtime_cli_overrides_ini_without_persisting_hidden_changes(self):
        self.initialize()
        before = self.config.read_bytes()
        output = self.root / "one-run-captures"
        value = self.check(["--cameras", "CAM2", "--time-ranges", "06:30-07:00",
                            "--segment-minutes", "60", "--output-dir", str(output)])
        self.assertEqual([camera.name for camera in value.cameras], ["cam2"])
        self.assertEqual([(s.hour, s.minute, s.duration) for s in value.schedules],
                         [(6, 30, timedelta(minutes=30))])
        self.assertEqual(value.segment_seconds, 3600)
        self.assertEqual(value.output_dir, output.resolve())
        self.assertEqual(self.config.read_bytes(), before)
        default_next_run = self.check()
        self.assertEqual(default_next_run.segment_seconds, 1800)
        self.assertEqual([camera.name for camera in default_next_run.cameras], ["cam1"])

    def test_invalid_ini_is_preserved_and_fails_before_external_access(self):
        invalid = "[RECORDING]\ntime_ranges=06:00-06:00\nsegment_minutes=30\n[CAMERAS]\nsetup_ini=auto\ncameras=auto\n"
        self.config.write_text(invalid, encoding="utf-8")
        before = self.config.read_bytes()
        with mock.patch.object(self.module, "check_ffmpeg") as ffmpeg, \
                mock.patch.object(self.module.subprocess, "Popen") as popen, \
                self.assertLogs(self.module.LOG, level="ERROR"):
            self.assertEqual(self.invoke(["--check"]), 1)
        ffmpeg.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_initial_install_creates_ini_from_cli_and_forwards_owner_and_config_only(self):
        real_create = self.module.create_ini_config

        def create_without_changing_host_owner(path=None, overrides=None, owner=None):
            return real_create(path, overrides, owner=None)

        with self.linux_service_mocks(), \
                mock.patch.object(self.module, "create_ini_config", side_effect=create_without_changing_host_owner) as create, \
                mock.patch.object(self.module, "install_service") as install:
            status = self.invoke(["--install", "--segment-minutes", "60", "--cameras", "CAM2",
                                  "--time-ranges", "08:00-09:00", "--setup-ini", str(self.setup)])
        self.assertEqual(status, 0)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs.get("owner"), "camera-user")
        install.assert_called_once_with(["--config", str(self.config.resolve())], "camera-user")
        parser = self.read_ini()
        self.assertEqual(parser.getfloat("RECORDING", "segment_minutes"), 60)
        self.assertEqual(parser.get("CAMERAS", "cameras"), "CAM2")
        self.assertEqual(parser.get("RECORDING", "time_ranges").replace(" ", ""), "08:00-09:00")
        self.assertNotIn("rtsp://", self.config.read_text(encoding="utf-8-sig"))
        self.assertNotIn("PRIVATE", self.config.read_text(encoding="utf-8-sig"))
        self.assertFalse((self.project / "runtime").exists())

    def test_existing_ini_install_references_file_without_frozen_options(self):
        self.initialize(["--segment-minutes", "60"])
        before = self.config.read_bytes()
        with self.linux_service_mocks(), \
                mock.patch.object(self.module, "create_ini_config", wraps=self.module.create_ini_config) as create, \
                mock.patch.object(self.module, "install_service") as install:
            self.assertEqual(self.invoke(["--install"]), 0)
        create.assert_called_once()
        install.assert_called_once_with(["--config", str(self.config.resolve())], "camera-user")
        self.assertEqual(self.config.read_bytes(), before)

    def test_install_with_existing_ini_rejects_runtime_overrides_and_preserves_file(self):
        self.initialize()
        before = self.config.read_bytes()
        for prefix in ([], ["--config", str(self.config)]):
            for options in (["--segment-minutes", "60"], ["--cameras", "CAM2"],
                            ["--time-ranges", "08:00-09:00"], ["--output-dir", str(self.root / "other")]):
                with self.subTest(prefix=prefix, options=options), self.linux_service_mocks(), \
                        mock.patch.object(self.module, "install_service") as install, \
                        self.assertLogs(self.module.LOG, level="ERROR") as messages:
                    self.assertEqual(self.invoke(prefix + ["--install"] + options), 1)
                install.assert_not_called()
                self.assertEqual(self.config.read_bytes(), before)
                self.assertIn("INI", "\n".join(messages.output).upper())

    def test_uninstall_does_not_require_ini_or_camera_setup(self):
        self.setup.unlink()
        with self.linux_service_mocks(), \
                mock.patch.object(self.module, "uninstall_service") as uninstall, \
                mock.patch.object(self.module, "find_setup_ini", side_effect=AssertionError("No discovery on uninstall")), \
                mock.patch.object(self.module, "check_ffmpeg", side_effect=AssertionError("No FFmpeg on uninstall")):
            self.assertEqual(self.invoke(["--uninstall"]), 0)
        uninstall.assert_called_once_with()
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
