"""Read-only installer tests; never invoke systemd or mutate system paths."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location("scheduled_recording_install", Path(__file__).with_name("install.py"))
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_unit_escapes_paths_without_shell_interpretation(self):
        config = Path('/opt/camera data/100%/$CAM/"config".json')
        content = installer.render_unit(config, "camera", Path("/usr/bin/python3"), Path("/opt/tools/record.py"))
        self.assertIn('WorkingDirectory=' + str(config.parent).replace("%", "%%") + '\n', content)
        self.assertIn('$$CAM', content.split("ExecStart=", 1)[1])
        self.assertIn('\\"config\\".json', content)
        self.assertIn("KillMode=mixed\nTimeoutStopSec=30", content)
        self.assertNotIn("/bin/sh", content)

    def test_working_directory_preserves_literal_path_characters(self):
        self.assertEqual(installer.unit_directory('/opt/camera space/100%/$CAM/"dir"'),
                         '/opt/camera space/100%%/$CAM/"dir"')
        self.assertEqual(installer.unit_directory('/opt/trailing '), '/opt/trailing /.')
        self.assertEqual(installer.unit_directory('/opt/trailing\\'), '/opt/trailing\\/.')

    def test_rejects_control_characters(self):
        for value in ("a\nb", "a\rb", "a\tb", "a\x00b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                installer.unit_quote(value)

    def test_preflight_failure_cannot_write_or_change_service(self):
        with patch.object(installer, "preflight", side_effect=subprocess.CalledProcessError(1, "check")), \
                patch.object(installer, "ensure_no_foreign_unit") as inspect, \
                patch.object(installer, "write_unit") as write, \
                patch.object(installer, "systemctl") as systemctl:
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(Path("/tmp/config.json"), "camera")
            inspect.assert_not_called()
            write.assert_not_called()
            systemctl.assert_not_called()

    def test_foreign_unit_is_not_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / installer.SERVICE_NAME
            path.write_text("[Unit]\nDescription=Someone else's service\n", encoding="utf-8")
            with patch.object(installer, "systemctl") as systemctl:
                with self.assertRaises(ValueError):
                    installer.uninstall(path)
                systemctl.assert_not_called()
            self.assertTrue(path.exists())

    def test_foreign_fragment_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / installer.SERVICE_NAME
            result = Mock(stdout="FragmentPath=/usr/lib/systemd/system/other.service\nDropInPaths=\n")
            with patch.object(installer, "systemctl", return_value=result), self.assertRaises(ValueError):
                installer.ensure_no_foreign_unit(path)

    def test_uninstall_only_removes_managed_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / installer.SERVICE_NAME
            path.write_text(installer.MANAGED_MARKER + "\n[Unit]\n", encoding="utf-8")
            recording = Path(directory) / "keep.mkv"
            recording.write_bytes(b"recording")
            result = Mock(stdout="FragmentPath=" + str(path) + "\nDropInPaths=\n")
            with patch.object(installer, "systemctl", return_value=result) as systemctl:
                installer.uninstall(path)
                self.assertIn(unittest.mock.call("disable", "--now", installer.SERVICE_NAME), systemctl.call_args_list)
            self.assertFalse(path.exists())
            self.assertEqual(recording.read_bytes(), b"recording")

    def test_check_runs_as_service_user_and_with_service_paths(self):
        with patch.object(installer.shutil, "which", side_effect=lambda tool: "/usr/bin/" + tool), \
                patch.object(installer.subprocess, "run") as run:
            config = Path("/opt/config/recording.json")
            installer.preflight(config, "camera", Path("/usr/bin/python3"))
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[:5], ["/usr/bin/runuser", "-u", "camera", "--", "/usr/bin/env"])
            self.assertIn("PATH=" + installer.SERVICE_PATH, arguments)
            self.assertEqual(arguments[-3:], ["--config", str(config), "--check"])
            self.assertEqual(run.call_args.kwargs["cwd"], str(config.parent))


if __name__ == "__main__":
    unittest.main()
