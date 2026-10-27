"""Offline weather gating and localhost UI tests; no real API key or camera is used."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("weather_ui_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def now(hour=6):
    return datetime(2026, 10, 15, hour, 0, tzinfo=record.KST)


def items(issue, forecasts):
    result = []
    for hour, pty, sky in forecasts:
        stamp = now(hour)
        for category, value in (("PTY", pty), ("SKY", sky), ("TMP", 20), ("POP", 60)):
            result.append({"baseDate": issue.strftime("%Y%m%d"), "baseTime": issue.strftime("%H%M"),
                           "fcstDate": stamp.strftime("%Y%m%d"), "fcstTime": stamp.strftime("%H%M"),
                           "category": category, "fcstValue": str(value), "nx": 99, "ny": 75})
    return result


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-weather-ui-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.weather = record.default_weather_config()
        self.weather["enabled"] = True
        self.weather["provider"] = "data_go_kr"
        self.condition = record.parse_weather_condition(self.weather, self.root)
        self.data = {"output_dir": str(self.root / "captures"), "time_ranges": list(record.DEFAULT_TIME_RANGES),
                     "cameras": [{"name": "cam1", "url": "rtsp://user:PRIVATE_CAMERA@192.0.2.1/live"}],
                     "weather_condition": self.weather}
        self.config = self.root / "recording.json"
        self.config.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(record.logging, "basicConfig").start()
        mock.patch.dict(os.environ, {"KMA_SERVICE_KEY": "UNIT_TEST_SECRET"}).start()


class WeatherConfigurationTests(Fixture):
    def test_embedded_assets_work_when_only_record_py_is_copied(self):
        copied = self.root / "record_only.py"
        copied.write_bytes(SOURCE.read_bytes())
        spec = importlib.util.spec_from_file_location("copied_weather_recorder", copied)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        self.assertEqual(len(module.region_data()["rows"]), 3837)
        self.assertIn("날씨 예보로 녹화하는 날 선택", module.embedded_text(module.EMBEDDED_UI))

    def test_official_city_and_optional_town_resolve_from_embedded_data(self):
        self.assertEqual((self.condition.nx, self.condition.ny), (99, 75))
        self.assertEqual(len(record.region_data()["rows"]), 3837)
        self.weather["region"] = {"sido": "경상북도", "sigungu": "구미시", "eupmyeondong": ""}
        result = record.parse_weather_condition(self.weather, self.root)
        self.assertEqual((result.nx, result.ny), (84, 96))
        town = next(row for row in record.region_data()["rows"] if row[0] == "부산광역시" and row[1] == "해운대구" and row[2])
        self.weather["region"] = dict(zip(("sido", "sigungu", "eupmyeondong"), town[:3]))
        self.assertEqual(record.parse_weather_condition(self.weather, self.root).region, tuple(town[:3]))

    def test_invalid_regions_conditions_and_provider_are_rejected(self):
        for update in ({"enabled": "yes"}, {"conditions": []}, {"conditions": ["rain", "rain"]},
                       {"conditions": ["fog"]}, {"provider": "https://other.invalid"}, {"poll_minutes": 1},
                       {"region": {"sido": "부산", "sigungu": "없는구"}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                record.parse_weather_condition(dict(self.weather, **update), self.root)

    def test_latest_base_obeys_korean_release_time_and_ten_minute_delay(self):
        before = datetime(2026, 10, 15, 2, 9, tzinfo=record.KST)
        self.assertEqual(record.latest_forecast_base(before), datetime(2026, 10, 14, 23, tzinfo=record.KST))
        after = before + timedelta(minutes=1)
        self.assertEqual(record.latest_forecast_base(after.astimezone(timezone.utc)), after.replace(minute=0))

    def test_weather_codes_use_precipitation_before_sky(self):
        self.assertTrue(record.forecast_matches({"PTY": 1, "SKY": 1}, "rain"))
        self.assertFalse(record.forecast_matches({"PTY": 1, "SKY": 1}, "clear"))
        for condition in ("rain", "snow"):
            self.assertTrue(record.forecast_matches({"PTY": 2, "SKY": 4}, condition))
        self.assertTrue(record.forecast_matches({"PTY": 4}, "rain"))
        self.assertFalse(record.forecast_matches({"POP": 100, "SKY": 4}, "rain"))
        self.assertFalse(record.forecast_matches({"SKY": 1}, "clear"))

    def test_encoded_key_is_decoded_once_and_never_in_settings_repr(self):
        with mock.patch.dict(os.environ, {"KMA_SERVICE_KEY": "abc%2Bdef%3D"}):
            self.assertEqual(record.weather_api_key(self.condition), "abc+def=")
        self.assertNotIn("UNIT_TEST_SECRET", repr(record.load_settings(self.config)))
        with mock.patch.dict(os.environ, {"KMA_SERVICE_KEY": "abc%0Adef"}), self.assertRaises(ValueError):
            record.weather_api_key(self.condition)

    def test_missing_key_and_bad_utf8_do_not_expose_raw_key_content(self):
        with mock.patch.dict(os.environ, {"KMA_SERVICE_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "인증키"):
                record.weather_api_key(self.condition)
            self.condition.key_file.write_bytes(b"PRIVATE\xff")
            with self.assertRaises(ValueError) as error:
                record.weather_api_key(self.condition)
            self.assertNotIn("PRIVATE", str(error.exception))


class ForecastFetchTests(Fixture):
    def payload(self, values, total=None):
        return {"response": {"header": {"resultCode": "00"}, "body": {
            "totalCount": len(values) if total is None else total, "items": {"item": values}}}}

    def opener(self, payloads):
        return mock.Mock(open=mock.Mock(side_effect=[io.BytesIO(json.dumps(value).encode("utf-8")) for value in payloads]))

    def test_request_uses_https_expected_grid_and_safe_key_encoding(self):
        issue = now(5)
        values = items(issue, [(6, 1, 4)])
        opener = self.opener([self.payload(values)])
        self.assertEqual(record.fetch_kma_forecast(self.condition, issue, "abc+def=", opener), values)
        request = opener.open.call_args.args[0]
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "apis.data.go.kr")
        self.assertEqual(query["serviceKey"], ["abc+def="])
        self.assertEqual((query["nx"], query["ny"]), (["99"], ["75"]))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)

    def test_pagination_keeps_late_rain_forecast_instead_of_truncating_first_page(self):
        issue = now(5)
        first, second = items(issue, [(6, 0, 1)]), items(issue, [(21, 1, 4)])
        opener = self.opener([self.payload(first, 8), self.payload(second, 8)])
        result = record.fetch_kma_forecast(self.condition, issue, "key", opener)
        self.assertEqual(len(result), 8)
        self.assertEqual(parse_qs(urlsplit(opener.open.call_args.args[0].full_url).query)["pageNo"], ["2"])
        self.assertTrue(any(record.forecast_matches(point, "rain") for point in record.forecast_points(result, issue).values()))

    def test_api_hub_uses_its_own_endpoint_and_auth_parameter(self):
        issue = now(5)
        opener = self.opener([self.payload(items(issue, [(6, 1, 4)]))])
        record.fetch_kma_forecast(replace(self.condition, provider="kma_api_hub"), issue, "key", opener)
        url = urlsplit(opener.open.call_args.args[0].full_url)
        self.assertEqual(url.hostname, "apihub.kma.go.kr")
        self.assertEqual(parse_qs(url.query)["authKey"], ["key"])
        self.assertNotIn("serviceKey", parse_qs(url.query))

    def test_errors_and_mismatched_region_never_expose_key_or_response(self):
        issue = now(5)
        values = items(issue, [(6, 1, 4)])
        values[0]["nx"] = "UNIT_TEST_SECRET"
        cases = [self.payload(values), {"response": {"header": {"resultCode": "30", "resultMsg": "UNIT_TEST_SECRET"}}}]
        for value in cases:
            with self.assertRaises(ValueError) as error:
                record.fetch_kma_forecast(self.condition, issue, "UNIT_TEST_SECRET", self.opener([value]))
            self.assertNotIn("UNIT_TEST_SECRET", str(error.exception))
        opener = mock.Mock(open=mock.Mock(side_effect=URLError("https://test?serviceKey=UNIT_TEST_SECRET")))
        with self.assertRaises(ValueError) as error:
            record.fetch_kma_forecast(self.condition, issue, "UNIT_TEST_SECRET", opener)
        self.assertNotIn("UNIT_TEST_SECRET", str(error.exception))

    def test_redirects_are_rejected_before_forwarding_credentials(self):
        with self.assertRaises(ValueError):
            record.NoWeatherRedirect().redirect_request(None, None, 302, "redirect", {}, "http://other.invalid")


class DailyGateTests(Fixture):
    def gate(self, forecasts):
        fetch = mock.Mock(side_effect=lambda condition, issue, key: items(issue, forecasts))
        return record.WeatherGate(self.condition, self.root, fetch), fetch

    def test_evening_rain_enables_morning_midday_and_evening_schedules(self):
        gate, fetch = self.gate([(0, 0, 1), (6, 0, 1), (15, 0, 1), (21, 1, 4)])
        gate.refresh(now(6))
        self.assertTrue(gate.allows_date(now().date(), now(6)))
        settings = record.load_settings(self.config)
        for hour in (6, 13, 20):
            self.assertIsNotNone(record.settings_window(now(hour), settings,
                lambda day: gate.allows_date(day, now(hour))))
        self.assertEqual(gate.label_for_day(now().date()), "비예보일")
        self.assertIsNone(record.settings_window(now(11), settings, lambda day: True))
        self.assertEqual(fetch.call_count, 2)

    def test_positive_day_is_retained_after_forecast_changes_and_process_restart(self):
        gate, _ = self.gate([(0, 0, 1), (21, 1, 4)])
        gate.refresh(now(6))
        gate.fetcher = lambda condition, issue, key: items(issue, [(0, 0, 1), (21, 0, 1)])
        gate.refresh(now(12))
        restarted = record.WeatherGate(self.condition, self.root)
        self.assertTrue(restarted.allows_date(now().date(), now(20)))
        self.assertFalse(restarted.allows_date((now() + timedelta(days=1)).date(), now() + timedelta(days=1)))

    def test_no_rain_and_unknown_forecast_do_not_start_recording(self):
        gate, _ = self.gate([(0, 0, 1), (21, 0, 1)])
        gate.refresh(now())
        self.assertFalse(gate.allows_date(now().date(), now()))
        gate.checked_at = (now() - timedelta(hours=7)).isoformat()
        gate.points = {now().isoformat(): {"time": now().isoformat(), "PTY": 1, "SKY": 4}}
        self.assertFalse(gate.allows_date(now().date(), now()))

    def test_changing_region_or_conditions_does_not_reuse_old_approval(self):
        gate, _ = self.gate([(0, 0, 1), (21, 1, 4)])
        gate.refresh(now())
        changed = replace(self.condition, conditions=("snow",))
        self.assertFalse(record.WeatherGate(changed, self.root).allows_date(now().date(), now()))

    def test_network_failure_keeps_only_already_approved_day(self):
        gate, _ = self.gate([(0, 0, 1), (21, 1, 4)])
        gate.refresh(now())
        gate.fetcher = mock.Mock(side_effect=ValueError("조회 실패"))
        with mock.patch.object(record, "LOG"):
            gate.refresh(now(12))
        self.assertTrue(gate.allows_date(now().date(), now(12)))
        other = record.WeatherGate(self.condition, self.root / "other", gate.fetcher, persist=False)
        with mock.patch.object(record, "LOG"):
            other.refresh(now(12))
        self.assertFalse(other.allows_date(now().date(), now(12)))

    def test_preview_does_not_write_cache_or_require_existing_output_folder(self):
        root = self.root / "not-created"
        gate = record.WeatherGate(self.condition, root,
                                  lambda condition, issue, key: items(issue, [(21, 1, 4)]), persist=False)
        gate.refresh(now())
        self.assertTrue(gate.snapshot(now())["allowed"])
        self.assertFalse(root.exists())

    def test_one_time_recording_bypasses_weather_and_normal_window_uses_start_day(self):
        settings = record.load_settings(self.config)
        once = replace(settings, once_window=(now(), now() + timedelta(minutes=5)))
        self.assertEqual(record.settings_window(now(), once, lambda day: False), once.once_window)
        overnight = replace(settings, schedules=(record.Schedule(23, 0, timedelta(hours=2)),))
        following = now().replace(day=16, hour=0)
        self.assertEqual(record.settings_window(following, overnight, lambda day: day == now().date()),
                         (now(23), now().replace(day=16, hour=1)))


class UiControllerTests(Fixture):
    def test_unsupported_inline_api_key_is_not_reflected_in_ui_config(self):
        self.data["weather_condition"]["api_key"] = "DO_NOT_REFLECT"
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        controller = record.RecorderUiController(self.config)
        self.assertNotIn("DO_NOT_REFLECT", json.dumps(controller.configuration()))

    def test_configuration_and_status_hide_camera_passwords_and_api_keys(self):
        controller = record.RecorderUiController(self.config)
        text = json.dumps(controller.configuration(), ensure_ascii=False) + json.dumps(controller.state(), ensure_ascii=False)
        self.assertNotIn("PRIVATE_CAMERA", text)
        self.assertNotIn("UNIT_TEST_SECRET", text)
        self.assertIn("해운대구", text)

    def test_save_keeps_camera_source_and_rejects_conflicting_file_revision(self):
        controller = record.RecorderUiController(self.config)
        before = controller.configuration()
        result = controller.save({"intersection_name": "새 이름", "segment_minutes": 60}, before["revision"])
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["cameras"], self.data["cameras"])
        self.assertEqual(saved["intersection_name"], "새 이름")
        self.assertNotEqual(result["revision"], before["revision"])
        with self.assertRaisesRegex(ValueError, "다른 곳에서 변경"):
            controller.save({"intersection_name": "잘못된 덮어쓰기"}, before["revision"])
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["intersection_name"], "새 이름")

    def test_invalid_save_and_arbitrary_executable_field_leave_original_untouched(self):
        controller = record.RecorderUiController(self.config)
        before = self.config.read_bytes()
        revision = controller.configuration()["revision"]
        for patch in ({"time_ranges": []}, {"segment_minutes": -1}, {"ffmpeg": "other-program"}):
            with self.assertRaises(ValueError):
                controller.save(patch, revision)
        self.assertEqual(self.config.read_bytes(), before)

    def test_live_save_checks_credentials_before_interrupting_current_recording(self):
        controller = record.RecorderUiController(self.config)
        controller.process = mock.Mock()
        controller.process.poll.return_value = None
        controller.mode = "scheduled"
        before = self.config.read_bytes()
        revision = controller.configuration()["revision"]
        with mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")), \
                mock.patch.object(record, "weather_api_key", side_effect=ValueError("인증키가 없습니다")), \
                mock.patch.object(controller, "_stop") as stop:
            with self.assertRaisesRegex(ValueError, "인증키"):
                controller.save({"intersection_name": "바꾼 이름"}, revision)
        stop.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)

    def test_save_does_not_interrupt_a_one_time_test(self):
        controller = record.RecorderUiController(self.config)
        controller.process = mock.Mock()
        controller.process.poll.return_value = None
        controller.mode = "once"
        original = self.config.read_bytes()
        with mock.patch.object(controller, "_stop") as stop, self.assertRaisesRegex(ValueError, "시험 녹화"):
            controller.save({"segment_minutes": 60}, controller.configuration()["revision"])
        stop.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)

    def test_key_storage_is_private_and_not_returned_to_browser(self):
        # /mnt/d uses Windows ACLs in WSL; check the actual Linux permissions on
        # its native temporary filesystem instead of silently skipping the check.
        with tempfile.TemporaryDirectory(dir=None if sys.platform == "linux" else self.root) as private_dir:
            key_file = Path(private_dir) / "kma_service.key"
            self.data["weather_condition"]["api_key_file"] = str(key_file)
            self.config.write_text(json.dumps(self.data), encoding="utf-8")
            controller = record.RecorderUiController(self.config)
            result = controller.save_key("SAVED_TEST_KEY")
            self.assertEqual(key_file.read_text(encoding="utf-8"), "SAVED_TEST_KEY\n")
            self.assertNotIn("SAVED_TEST_KEY", json.dumps(result))
            if sys.platform == "linux":
                self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

    def test_key_is_not_written_when_filesystem_cannot_enforce_private_permissions(self):
        controller = record.RecorderUiController(self.config)
        with mock.patch.object(record.sys, "platform", "linux"), \
                mock.patch.object(record.os, "fstat", return_value=mock.Mock(st_mode=0o100777)):
            with self.assertRaisesRegex(ValueError, "Linux 파일 권한"):
                controller.save_key("MUST_NOT_BE_WRITTEN")
        self.assertFalse(self.condition.key_file.exists())
        self.assertFalse(list(self.root.glob(".recorder-save-*")))

    def test_start_uses_own_script_and_explicit_arguments_without_a_shell(self):
        controller = record.RecorderUiController(self.config)
        process = mock.Mock(stdout=io.StringIO("ordinary log\nrtsp://PRIVATE_CAMERA\n"))
        process.poll.return_value = None
        with mock.patch.object(record.sys, "platform", "linux"), \
                mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")), \
                mock.patch.object(record.subprocess, "Popen", return_value=process) as popen:
            controller.start("once", 5)
            controller.reader.join(timeout=1)
        command = popen.call_args.args[0]
        self.assertEqual(command[:4], [sys.executable, str(SOURCE.resolve()), "--config", str(self.config)])
        self.assertEqual(command[-2:], ["--record-minutes", "5"])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        self.assertNotIn("PRIVATE_CAMERA", "\n".join(controller.logs))
        with self.assertRaisesRegex(ValueError, "이미 녹화"):
            controller.start("scheduled")

    def test_ui_install_check_is_read_only_and_does_not_open_server(self):
        with mock.patch.object(record, "run_recorder_ui") as ui, \
                mock.patch.object(record, "check_ffmpeg", return_value=("ffmpeg", "-timeout")), \
                mock.patch.object(record, "Storage") as storage, mock.patch.object(record, "LOG"):
            result = record.main(["--ui", "--ui-autostart", "--config", str(self.config), "--check"])
        self.assertEqual(result, 0)
        ui.assert_not_called()
        storage.assert_not_called()

    def test_ui_service_install_keeps_config_and_ui_flags_in_the_unit(self):
        with mock.patch.object(record.sys, "platform", "linux"), \
                mock.patch.object(record.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(record, "resolve_service_user", return_value="dis"), \
                mock.patch.object(record, "install_service") as install, mock.patch.object(record, "LOG"):
            self.assertEqual(record.main(["--ui", "--config", str(self.config), "--install"]), 0)
        self.assertEqual(install.call_args.args, (["--config", str(self.config), "--ui", "--ui-port", "8765", "--ui-autostart"], "dis"))


class UiHttpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.controller = record.RecorderUiController(self.config)
        self.server = record.ThreadingHTTPServer(("127.0.0.1", 0), record.BaseHTTPRequestHandler)
        self.port = self.server.server_port
        self.server.RequestHandlerClass = record.make_ui_handler(self.controller, "test-session-token", self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.finish_server)

    def finish_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def get(self, path, headers=None, data=None):
        request = Request("http://127.0.0.1:{}{}".format(self.port, path),
                          headers=headers or {}, data=None if data is None else json.dumps(data).encode("utf-8"))
        return urlopen(request, timeout=5)

    def test_page_works_with_embedded_assets_and_has_restrictive_browser_headers(self):
        with self.get("/") as response:
            page = response.read().decode("utf-8")
            self.assertIn("test-session-token", page)
            self.assertIn("카메라 녹화", page)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
        self.assertNotIn("PRIVATE_CAMERA", page)

    def test_missing_token_cross_origin_and_dns_rebinding_host_are_rejected(self):
        for headers in ({}, {"X-Recorder-Token": "wrong"},
                        {"X-Recorder-Token": "test-session-token", "Origin": "http://other.invalid"},
                        {"X-Recorder-Token": "test-session-token", "Host": "other.invalid"}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                self.get("/api/config", headers)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

    def test_authenticated_config_never_returns_secrets(self):
        with self.get("/api/config", {"X-Recorder-Token": "test-session-token"}) as response:
            value = response.read().decode("utf-8")
        self.assertNotIn("PRIVATE_CAMERA", value)
        self.assertNotIn("UNIT_TEST_SECRET", value)

    def test_large_ui_responses_use_gzip_when_browser_supports_it(self):
        with self.get("/api/config", {"X-Recorder-Token": "test-session-token", "Accept-Encoding": "gzip"}) as response:
            raw = response.read()
            self.assertEqual(response.headers["Content-Encoding"], "gzip")
            self.assertEqual(int(response.headers["Content-Length"]), len(raw))
        decoded = gzip.decompress(raw)
        self.assertGreater(len(decoded), len(raw))
        self.assertNotIn(b"PRIVATE_CAMERA", decoded)
        self.assertEqual(len(json.loads(decoded)["regions"]["rows"]), 3837)

    def test_post_validates_json_body_and_requires_token_before_mutating(self):
        original = self.config.read_bytes()
        with self.assertRaises(HTTPError) as error:
            self.get("/api/save", {"Content-Type": "application/json"}, {"config": {"intersection_name": "x"}})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.get("/api/save", {"X-Recorder-Token": "test-session-token", "Content-Type": "text/plain"}, {})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.config.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
