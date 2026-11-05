"""Desktop workflow regressions without requiring a display or physical cameras."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("desktop_gui_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


class DesktopFormTests(unittest.TestCase):
    def test_dates_accept_readable_list_and_reject_invalid_or_duplicate_days(self):
        self.assertEqual(record.desktop_dates("2026-10-27, 2026-10-28\n2026-10-29"),
                         ["2026-10-27", "2026-10-28", "2026-10-29"])
        self.assertEqual(record.desktop_dates(" \n"), [])
        for invalid in ("2026-02-30", "2026-10-27,2026-10-27", "10/27"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                record.desktop_dates(invalid)

    def test_numeric_fields_reject_non_finite_and_fractional_retention(self):
        self.assertEqual(record.desktop_number(" 30 ", "분할"), 30)
        self.assertEqual(record.desktop_number("0", "보관", True), 0)
        for text in ("nan", "inf", "-inf", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                record.desktop_number(text, "길이")
        with self.assertRaises(ValueError):
            record.desktop_number("1.5", "보관", True)

    def test_patch_preserves_legacy_schedule_and_unexposed_camera_settings(self):
        temp = tempfile.TemporaryDirectory(prefix=".test-desktop-gui-", dir=SOURCE.parent)
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        path = base / "recording.json"
        data = {"output_dir": str(base / "videos"), "intersection_name": "기존 이름",
                "cameras": [{"name": "cam1", "url": "rtsp://private:secret@192.0.2.1/live"}],
                "schedules": [{"start": "00:00", "duration_hours": 24}], "segment_seconds": 90,
                "reconnect_seconds": 17}
        path.write_text(json.dumps(data), encoding="utf-8")
        controller = record.RecorderUiController(path)
        configuration = controller.configuration()
        self.assertTrue(configuration["config"]["legacy_schedule"])
        baseline = {"intersection_name": "기존 이름", "time_ranges": [], "segment_minutes": 1.5}
        changed = dict(baseline, intersection_name="새 교차로")
        patch = record.desktop_config_patch(baseline, changed)
        self.assertEqual(patch, {"intersection_name": "새 교차로"})
        controller.save(patch, configuration["revision"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        for key in ("cameras", "schedules", "segment_seconds", "reconnect_seconds"):
            self.assertEqual(saved[key], data[key])
        self.assertEqual(saved["intersection_name"], "새 교차로")

    def test_removed_schedule_is_an_intentional_patch(self):
        self.assertEqual(record.desktop_config_patch({"time_ranges": ["06:00-11:00"]}, {"time_ranges": []}),
                         {"time_ranges": []})

    def test_forecast_distinguishes_unknown_negative_and_positive(self):
        self.assertIn("아직 유효한", record.desktop_forecast_text({"enabled": True}))
        self.assertIn("선택한 날씨가 없습니다", record.desktop_forecast_text({"enabled": True, "known": True}))
        self.assertIn("예약 시간표", record.desktop_forecast_text({"enabled": True, "allowed": True}))
        self.assertIn("응답 본문", record.desktop_forecast_text({"enabled": True, "allowed": True,
            "network": {"body_bytes_today": 711, "requests_today": 1, "cached": True}}))

    def test_manual_preview_keeps_weather_rule_off_and_shows_source(self):
        app = record.RecorderDesktopApp.__new__(record.RecorderDesktopApp)
        app.baseline = {"weather_condition": {"enabled": False}}
        for field in ("state_text", "summary_text", "camera_tree", "weather_text", "log_box", "_update_buttons"):
            setattr(app, field, mock.Mock())
        app.camera_tree.get_children.return_value = []
        app.last_logs = ""
        app._show_state({"running": False, "weather": {"enabled": True, "allowed": True, "source": "Open-Meteo"}})
        text = app.weather_text.set.call_args.args[0]
        self.assertIn("날씨 조건이 꺼져", text)
        self.assertIn("Open-Meteo", text)
        self.assertNotIn("오늘은 날씨 조건에 맞습니다", text)


class DesktopActionTests(unittest.TestCase):
    def app(self):
        app = record.RecorderDesktopApp.__new__(record.RecorderDesktopApp)
        app.pending_action = app.closing = False
        app.controller = mock.Mock()
        app.root = mock.Mock()
        app.messagebox = mock.Mock()
        app.revision = "old-revision"
        app.action = mock.Mock()
        app._patch = mock.Mock(return_value={"intersection_name": "수정 이름"})
        app._load = mock.Mock()
        app._error = mock.Mock()
        app._request_state = mock.Mock()
        app.notice = mock.Mock()
        app.dirty_text = mock.Mock()
        app.controller.save.return_value = {"message": "saved"}
        app.controller.configuration.return_value = {"revision": "new-revision"}
        return app

    def test_cancel_dirty_save_does_not_start_or_write(self):
        app = self.app()
        app.messagebox.askyesno.return_value = False
        operation = mock.Mock()
        app.with_settings(operation)
        app.action.assert_not_called()
        app.controller.save.assert_not_called()
        operation.assert_not_called()

    def test_save_precedes_start_and_updates_revision_when_start_fails(self):
        app = self.app()
        operation = mock.Mock(side_effect=ValueError("FFmpeg not found"))
        app.with_settings(operation)
        work, callback = app.action.call_args.args[:2]
        result = work()
        callback(result)
        app.controller.save.assert_called_once_with({"intersection_name": "수정 이름"}, "old-revision")
        operation.assert_called_once()
        app._load.assert_called_once_with({"revision": "new-revision"})
        app._error.assert_called_once()

    def test_revision_conflict_never_starts_with_unsaved_settings(self):
        app = self.app()
        app.controller.save.side_effect = ValueError("revision conflict")
        operation = mock.Mock()
        app.with_settings(operation)
        work = app.action.call_args.args[0]
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            work()
        operation.assert_not_called()

    def test_unchanged_action_does_not_rewrite_configuration(self):
        app = self.app()
        app._patch.return_value = {}
        operation = mock.Mock(return_value={"message": "checked"})
        app.with_settings(operation)
        work, callback = app.action.call_args.args[:2]
        callback(work())
        app.controller.save.assert_not_called()
        app.controller.configuration.assert_not_called()
        operation.assert_called_once()

    def test_worker_serializes_operations_and_returns_exceptions_without_running_callbacks(self):
        app = record.RecorderDesktopApp.__new__(record.RecorderDesktopApp)
        app.jobs, app.results = queue.Queue(), queue.Queue()
        history = []
        callback = mock.Mock()
        def first():
            history.append("start")
            return "saved"
        def second():
            self.assertEqual(history, ["start"])
            history.append("stop")
            raise ValueError("failure")
        app.jobs.put(("action", first, callback))
        app.jobs.put(("action", second, callback))
        app.jobs.put(None)
        thread = threading.Thread(target=app._worker)
        thread.start()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(app.results.get(timeout=1)[2], "saved")
        self.assertIsInstance(app.results.get(timeout=1)[3], ValueError)
        callback.assert_not_called()

    def test_close_cancel_keeps_owned_recording(self):
        app = self.app()
        app.dirty = False
        app.last_state = {"running": True}
        app.messagebox.askyesno.return_value = False
        app.jobs = queue.Queue()
        app.request_close()
        self.assertTrue(app.jobs.empty())
        app.controller.close.assert_not_called()

    def test_close_is_queued_and_only_calls_controller_own_close(self):
        app = self.app()
        app.dirty = False
        app.last_state = {"running": True}
        app._set_busy = mock.Mock()
        app.jobs = queue.Queue()
        app.request_close()
        kind, work, _callback = app.jobs.get_nowait()
        self.assertEqual(kind, "close")
        app.controller.close.assert_not_called()
        work()
        app.controller.close.assert_called_once()

    def test_termination_handler_only_sets_event_then_tk_poll_closes_after_busy_work(self):
        app, root = mock.Mock(), mock.Mock()
        app.destroyed, app.pending_action = False, True
        sentinel = object()
        with mock.patch.object(record.signal, "getsignal", return_value=sentinel), mock.patch.object(record.signal, "signal") as register:
            restore = record.desktop_signal_watch(root, app)
            registrations = list(register.call_args_list)
            self.assertGreaterEqual(len(registrations), 2)
            handler = registrations[0].args[1]
            handler(signal.SIGTERM, None)
            app.request_close.assert_not_called()
            poll = root.after.call_args.args[1]
            poll()
            app.request_close.assert_not_called()
            app.pending_action = False
            poll()
            app.request_close.assert_called_once_with(force=True)
            restore()
            for call in registrations:
                self.assertIn(mock.call(call.args[0], sentinel), register.call_args_list)


@unittest.skipUnless(os.environ.get("ED_TEST_DESKTOP_DISPLAY") == "1", "Set ED_TEST_DESKTOP_DISPLAY=1 for real Tk integration tests")
class DesktopTkIntegrationTests(unittest.TestCase):
    """Run under a real desktop or Xvfb; dialogs are injected to stay nonmodal."""

    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        self.temp = tempfile.TemporaryDirectory(prefix=".test-desktop-tk-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.path = self.folder / "recording.json"
        self.data = {
            "output_dir": str(self.folder / "videos"),
            "intersection_name": "초기 교차로", "time_ranges": list(record.DEFAULT_TIME_RANGES),
            "cameras": [{"name": "cam1", "url": "rtsp://private:secret@192.0.2.1/live"}],
            "weather_condition": record.default_weather_config(),
            "segment_minutes": 30, "retention_days": 0, "min_free_gb": 5,
            "reconnect_seconds": 17, "stall_seconds": 43,
        }
        self.write_config(self.data)
        self.tk_root = tk.Tk()
        self.tk_root.withdraw()
        self.timers_at_destroy = []
        actual_destroy = self.tk_root.destroy
        def checked_destroy():
            self.timers_at_destroy.append(self.tk_root.tk.call("after", "info"))
            actual_destroy()
        self.tk_root.destroy = checked_destroy
        self.callback_errors = []
        self.tk_root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.dialogs = mock.Mock()
        self.dialogs.askyesno.return_value = True
        self.controller = record.RecorderUiController(self.path)
        self.app = record.RecorderDesktopApp(self.tk_root, tk, ttk, (mock.Mock(), self.dialogs), self.controller)
        self.addCleanup(self.cleanup_window)
        self.pump_until(lambda: self.app.loaded and not self.app.pending_action)
        self.assertEqual(self.callback_errors, [])

    def write_config(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def pump_until(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for Tk state; notice={!r}, callback errors={!r}".format(
                    self.app.notice.get(), self.callback_errors))
            self.tk_root.update()
            time.sleep(0.005)
        self.assertEqual(self.callback_errors, [])

    def cleanup_window(self):
        if not self.app.destroyed:
            try:
                self.controller.close()
            finally:
                self.app.jobs.put(None)
                self.app.destroyed = True
                # A failed assertion bypasses the app's normal close callback.
                # Remove its Tcl timers before the next test creates a root.
                for identifier in self.tk_root.tk.call("after", "info"):
                    self.tk_root.after_cancel(identifier)
                self.tk_root.destroy()
        self.app.worker.join(timeout=2)

    def close_window(self):
        self.app.request_close()
        self.pump_until(lambda: self.app.destroyed)
        self.app.worker.join(timeout=2)
        self.assertFalse(self.app.worker.is_alive())
        self.assertEqual(self.callback_errors, [])
        self.assertFalse(self.timers_at_destroy[-1])

    def test_load_edit_save_real_controls_preserve_hidden_settings_then_close_without_tcl_error(self):
        app = self.app
        self.assertEqual(len(app.notebook.tabs()), 3)
        self.assertEqual(app.form["intersection_name"].get(), "초기 교차로")
        shown_ranges = [start.get() + "-" + end.get() for _, start, end in app.rows]
        self.assertEqual(shown_ranges, self.data["time_ranges"])
        app.form["intersection_name"].set("구미 시험 교차로")
        app.rows[0][1].set("06:45")
        app.form["recording_dates"].set("2026-10-28, 2026-10-30")
        app.form["sido"].set("경상북도")
        app._region_changed(0)
        app.form["sigungu"].set("구미시")
        app._region_changed(1)
        app.form["weather_enabled"].set(True)
        self.assertTrue(app.dirty)
        app.save()
        self.pump_until(lambda: not app.pending_action and not app.dirty)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["intersection_name"], "구미 시험 교차로")
        expected_ranges = list(self.data["time_ranges"])
        expected_ranges[0] = "06:45-" + self.data["time_ranges"][0].split("-", 1)[1]
        self.assertEqual(saved["time_ranges"], expected_ranges)
        self.assertEqual(saved["recording_dates"], ["2026-10-28", "2026-10-30"])
        self.assertEqual(saved["weather_condition"]["region"], {"sido": "경상북도", "sigungu": "구미시", "eupmyeondong": ""})
        for name in ("cameras", "reconnect_seconds", "stall_seconds"):
            self.assertEqual(saved[name], self.data[name])
        self.assertFalse(self.dialogs.showerror.called)
        app._show_state({"running": True, "mode": "scheduled", "active_cameras": ["cam1"],
            "cameras": [{"name": "cam1", "client_no": "29", "intersection": "구미 시험 교차로", "direction": "West"}],
            "disk": {"free_gb": 120.5}, "weather": {"enabled": True, "allowed": True, "source": "Open-Meteo"},
            "logs": ["Recorder started"]})
        self.assertIn("녹화 중", app.state_text.get())
        self.assertEqual(app.camera_tree.item(app.camera_tree.get_children()[0], "values")[0], "29")
        self.assertIn("오늘은 날씨 조건에 맞습니다", app.weather_text.get())
        self.assertIn("Recorder started", app.log_box.get("1.0", "end"))
        self.close_window()

    def test_fallback_cleanup_cancels_callbacks_when_normal_close_was_bypassed(self):
        callback = mock.Mock()
        self.tk_root.after(100, callback)
        self.cleanup_window()
        self.assertTrue(self.app.destroyed)
        self.assertFalse(self.app.worker.is_alive())
        self.assertFalse(self.timers_at_destroy[-1])
        callback.assert_not_called()

    def test_close_failure_leaves_live_controls_and_can_retry(self):
        actual_close = self.controller.close
        def fail_close():
            self.controller.closing = True
            raise ValueError("종료 지연 시험")
        with mock.patch.object(self.controller, "close", side_effect=fail_close):
            self.app.request_close()
            self.pump_until(lambda: self.dialogs.showerror.called and not self.app.closing)
        self.assertFalse(self.app.destroyed)
        self.assertFalse(self.controller.closing)
        self.assertTrue(self.app.start_button.instate(["!disabled"]))
        self.assertTrue(self.app.worker.is_alive())
        self.assertEqual(self.callback_errors, [])
        self.assertEqual(self.controller.close, actual_close)
        self.close_window()

    def test_existing_invalid_numeric_setting_can_be_corrected_in_form(self):
        invalid = dict(self.data, retention_days="잘못된 숫자")
        self.write_config(invalid)
        self.app.reload()
        self.pump_until(lambda: not self.app.pending_action)
        self.assertTrue(self.app.loaded)
        self.assertEqual(self.app.form["retention_days"].get(), "잘못된 숫자")
        self.app.form["retention_days"].set("0")
        self.app.save()
        self.pump_until(lambda: not self.app.pending_action and not self.app.dirty)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["retention_days"], 0)
        self.assertEqual(saved["cameras"], self.data["cameras"])
        self.assertFalse(self.dialogs.showerror.called)
        self.close_window()

    @unittest.skipUnless(sys.platform == "linux", "Owned process finalization uses Linux signals")
    def test_sigterm_finalizes_owned_child_and_closes_window(self):
        # A local sleeper stands in for the recorder; no RTSP connection is made.
        child = subprocess.Popen([sys.executable, "-c",
            "import signal,sys,time; signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); print('READY',flush=True); time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True)
        def cleanup_child():
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
            child.stdout.close()
        self.addCleanup(cleanup_child)
        self.assertEqual(child.stdout.readline().strip(), "READY")
        self.controller.process, self.controller.mode = child, "once"
        self.app.last_state = {"running": True, "mode": "once"}
        restore = record.desktop_signal_watch(self.tk_root, self.app)
        self.addCleanup(restore)
        os.kill(os.getpid(), signal.SIGTERM)
        self.pump_until(lambda: self.app.destroyed)
        self.assertEqual(child.poll(), 0)
        self.assertFalse(self.timers_at_destroy[-1])
        self.dialogs.askyesno.assert_not_called()
        self.app.worker.join(timeout=2)
        self.assertFalse(self.app.worker.is_alive())


if __name__ == "__main__":
    unittest.main()
