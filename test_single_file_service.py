"""Embedded installer checks; service writes/commands are always mocked or temporary."""

import importlib.util
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch


SPEC = importlib.util.spec_from_file_location("single_file_service_recorder", Path(__file__).with_name("record.py"))
recorder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recorder
SPEC.loader.exec_module(recorder)


class SingleFileServiceTests(unittest.TestCase):
    def test_unit_runs_copied_script_with_literal_runtime_arguments(self):
        script = PurePosixPath('/opt/project space/100%/$CAM/"dir"/record.py')
        args = ["--setup-ini", '/opt/project space/100%/$CAM/"setup".ini',
                "--output-dir", "/mnt/recordings", "--start-times", "07:00", "--duration-hours", "2"]
        unit = recorder.render_service_unit(args, "camera", script_path=script,
                                             python_path=PurePosixPath("/usr/bin/python3"))
        self.assertIn('WorkingDirectory=/opt/project space/100%%/$CAM/"dir"\n', unit)
        self.assertIn('ExecStart="/usr/bin/python3" "/opt/project space/100%%/$$CAM/\\"dir\\"/record.py"', unit)
        self.assertIn('"--start-times" "07:00" "--duration-hours" "2"', unit)
        self.assertNotIn("install.py", unit)
        self.assertNotIn("--check", unit)
        self.assertNotIn("/bin/sh", unit)

    def test_working_directory_is_raw_and_preserves_trailing_characters(self):
        self.assertEqual(recorder.service_working_directory('/opt/100%/"quoted"/$name'),
                         '/opt/100%%/"quoted"/$name')
        self.assertEqual(recorder.service_working_directory('/opt/trailing '), '/opt/trailing /.')
        self.assertEqual(recorder.service_working_directory('/opt/trailing\\'), '/opt/trailing\\/.')
        for value in ("a\nb", "a\rb", "a\x00b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                recorder.service_working_directory(value)

    def test_render_rejects_user_directive_injection(self):
        with self.assertRaises(ValueError):
            recorder.render_service_unit([], "camera\nExecStart=/bad")

    def test_render_has_no_preflight_or_service_side_effect(self):
        with patch.object(recorder, "service_preflight") as preflight, \
                patch.object(recorder, "service_systemctl") as systemctl, \
                patch.object(recorder, "service_write_unit") as write:
            unit = recorder.render_service_unit([], "camera")
            self.assertIn(recorder.SERVICE_MARKER, unit)
            preflight.assert_not_called()
            systemctl.assert_not_called()
            write.assert_not_called()

    def test_preflight_uses_selected_account_and_exact_copied_script(self):
        script = PurePosixPath("/opt/copied-project/tools/record.py")
        args = ["--setup-ini", "/opt/copied-project/config/setup.ini", "--segment-minutes", "30"]
        with patch.object(recorder.shutil, "which", side_effect=lambda tool: "/usr/bin/" + tool), \
                patch.object(recorder.subprocess, "run") as run:
            recorder.service_preflight(args, "camera", script, PurePosixPath("/usr/bin/python3"))
        actual = run.call_args.args[0]
        self.assertEqual(actual[:5], ["/usr/bin/runuser", "-u", "camera", "--", "/usr/bin/env"])
        self.assertIn("PATH=" + recorder.SERVICE_PATH, actual)
        self.assertEqual(actual[-len(args)-2:], [str(script), *args, "--check"])
        self.assertEqual(run.call_args.kwargs["cwd"], str(script.parent))
        self.assertGreater(run.call_args.kwargs["timeout"], 0)

    def test_failed_check_prevents_all_service_mutation(self):
        with patch.object(recorder.os, "geteuid", return_value=0, create=True), \
                patch.object(recorder, "service_preflight", side_effect=subprocess.CalledProcessError(1, "check")), \
                patch.object(recorder, "service_ensure_no_foreign_unit") as inspect, \
                patch.object(recorder, "service_write_unit") as write, \
                patch.object(recorder, "service_systemctl") as systemctl:
            with self.assertRaises(subprocess.CalledProcessError):
                recorder.install_service(["--start-times", "07:00"], "camera")
            inspect.assert_not_called()
            write.assert_not_called()
            systemctl.assert_not_called()

    def test_install_uses_own_location_and_restarts_after_check(self):
        script = Path(__file__).resolve().parent / "copied-recorder.py"
        timeline = Mock()
        with patch.object(recorder.os, "geteuid", return_value=0, create=True), \
                patch.object(recorder, "__file__", str(script)), \
                patch.object(recorder, "service_preflight", timeline.check), \
                patch.object(recorder, "service_ensure_no_foreign_unit", timeline.inspect), \
                patch.object(recorder, "service_write_unit", timeline.write), \
                patch.object(recorder, "service_systemctl", timeline.systemctl):
            recorder.install_service(["--segment-minutes", "30"], "camera")
        self.assertEqual(timeline.mock_calls[0].args[2], script)
        self.assertEqual([item[0] for item in timeline.mock_calls],
                         ["check", "inspect", "write", "systemctl", "systemctl", "systemctl"])
        self.assertEqual(timeline.mock_calls[-1], call.systemctl("restart", recorder.SERVICE_NAME))

    def test_previous_installer_marker_is_upgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / recorder.SERVICE_NAME
            unit.write_text("# Managed by tools/scheduled_recording/install.py\n[Unit]\n", encoding="utf-8")
            replacement = recorder.SERVICE_MARKER + "\n[Unit]\nDescription=Upgraded\n"
            recorder.service_write_unit(replacement, unit)
            self.assertEqual(unit.read_text(encoding="utf-8"), replacement)

    def test_uninstall_accepts_both_managed_markers_and_keeps_recordings(self):
        for marker in recorder.SERVICE_MARKERS:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                unit = Path(directory) / recorder.SERVICE_NAME
                unit.write_text(marker + "\n[Unit]\n", encoding="utf-8")
                recording = Path(directory) / "keep.mp4"
                recording.write_bytes(b"recording")
                result = Mock(stdout="FragmentPath=" + str(unit) + "\nDropInPaths=\n")
                with patch.object(recorder.os, "geteuid", return_value=0, create=True), \
                        patch.object(recorder, "UNIT_PATH", unit), \
                        patch.object(recorder, "service_systemctl", return_value=result) as systemctl:
                    recorder.uninstall_service()
                self.assertFalse(unit.exists())
                self.assertEqual(recording.read_bytes(), b"recording")
                self.assertIn(call("disable", "--now", recorder.SERVICE_NAME), systemctl.call_args_list)

    def test_foreign_unit_and_dropins_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / recorder.SERVICE_NAME
            unit.write_text("[Unit]\nDescription=Unmanaged\n", encoding="utf-8")
            with patch.object(recorder, "service_systemctl") as systemctl, self.assertRaises(ValueError):
                recorder.service_ensure_no_foreign_unit(unit)
            systemctl.assert_not_called()
            unit.write_text(recorder.SERVICE_MARKER + "\n[Unit]\n", encoding="utf-8")
            result = Mock(stdout="FragmentPath=" + str(unit) + "\nDropInPaths=/etc/override.conf\n")
            with patch.object(recorder, "service_systemctl", return_value=result), self.assertRaises(ValueError):
                recorder.service_ensure_no_foreign_unit(unit)
            self.assertTrue(unit.exists())


if __name__ == "__main__":
    unittest.main()
