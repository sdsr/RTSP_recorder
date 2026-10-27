"""Keyless forecast providers, Korean day boundaries, and bandwidth-saving HTTP caching."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
import gzip
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("public_weather_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def now():
    return datetime(2026, 10, 19, 6, 0, tzinfo=record.KST)


def headers(values=None):
    result = Message()
    for key, value in (values or {}).items():
        result[key] = value
    return result


def open_body():
    return {"timezone": "Asia/Seoul", "utc_offset_seconds": 32400,
            "hourly": {"time": ["2026-10-19T00:00", "2026-10-19T18:00", "2026-10-20T00:00"],
                       "weather_code": [0, 61, 0], "temperature_2m": [15, 20, 15]}}


def met_body(symbol="rain", period=1, time="2026-10-19T09:00:00Z"):
    return {"properties": {"meta": {"updated_at": "2026-10-18T18:00:00Z"}, "timeseries": [
        {"time": time, "data": {"instant": {"details": {"air_temperature": 20}},
                                  "next_{}_hours".format(period): {"summary": {"symbol_code": symbol}}}}]}}


class Response(io.BytesIO):
    def __init__(self, body, response_headers=None, status=200, compressed=False):
        raw = json.dumps(body).encode("utf-8")
        if compressed:
            raw = gzip.compress(raw)
        super().__init__(raw)
        self.headers = headers(response_headers)
        if compressed:
            self.headers["Content-Encoding"] = "gzip"
        self.status = status
        self.wire_size = len(raw)

    def getcode(self):
        return self.status


class PublicForecastTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-public-weather-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        data = record.default_weather_config()
        data["enabled"] = True
        self.condition = record.parse_weather_condition(data, self.root)

    def client(self, responses, condition=None):
        opener = mock.Mock(open=mock.Mock(side_effect=responses))
        return record.PublicWeatherClient(condition or self.condition, self.root, opener), opener

    def test_default_is_keyless_open_meteo_with_real_region_coordinates(self):
        self.assertEqual(self.condition.provider, "open_meteo")
        self.assertEqual((self.condition.latitude, self.condition.longitude), (35.16, 129.1658))
        with mock.patch.object(record, "weather_api_key", side_effect=AssertionError("must not require a key")):
            self.assertIsNone(record.validate_weather_access(self.condition))

    def test_open_meteo_maps_rain_snow_clear_cloud_and_missing_values(self):
        for code, pty in ((0, 0), (1, 0), (2, 0), (3, 0), (45, 0), (51, 1), (61, 1), (67, 1),
                          (71, 3), (77, 3), (85, 3), (80, 4), (95, 1)):
            with self.subTest(code=code):
                self.assertEqual(record.wmo_weather(code)[0], pty)
        for code in (None, True, -1, 1000, "61"):
            self.assertIsNone(record.wmo_weather(code))
        points, _ = record.parse_open_meteo(open_body(), now())
        self.assertEqual(points["2026-10-19T18:00:00+09:00"]["PTY"], 1)
        bad = open_body()
        bad["utc_offset_seconds"] = 0
        with self.assertRaises(ValueError):
            record.parse_open_meteo(bad, now())

    def test_request_is_small_gzipped_and_second_process_reuses_persistent_cache(self):
        response = Response(open_body(), compressed=True)
        size = response.wire_size
        client, opener = self.client([response])
        points, _, checked = client.fetch(now())
        request = opener.open.call_args.args[0]
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["hourly"], ["weather_code,temperature_2m"])
        self.assertEqual(query["timezone"], ["Asia/Seoul"])
        self.assertEqual(query["latitude"], ["35.1600"])
        self.assertNotIn("apikey", query)
        self.assertEqual(request.get_header("Accept-encoding"), "gzip, deflate")
        second = record.PublicWeatherClient(self.condition, self.root, mock.Mock(open=mock.Mock(side_effect=AssertionError("cache must prevent network"))))
        second_points, _, _ = second.fetch(now() + timedelta(minutes=1))
        self.assertEqual(second_points, points)
        self.assertEqual(checked, now())
        self.assertEqual(second.stats(now())["body_bytes_today"], size)
        self.assertEqual(second.stats(now())["requests_today"], 1)
        self.assertTrue(second.stats(now())["cached"])

    def test_expires_is_respected_beyond_configured_poll_interval(self):
        expires = format_datetime((now() + timedelta(hours=2)).astimezone(timezone.utc), usegmt=True)
        client, opener = self.client([Response(open_body(), {"Expires": expires})])
        client.fetch(now())
        client.fetch(now() + timedelta(minutes=45))
        self.assertEqual(opener.open.call_count, 1)

    def test_conditional_request_304_reuses_body_and_updates_confirmation_time(self):
        last_modified = "Sun, 18 Oct 2026 21:00:00 GMT"
        first = Response(open_body(), {"Last-Modified": last_modified, "ETag": '"forecast-1"'})
        unchanged = HTTPError("https://api.open-meteo.com", 304, "Not Modified", headers({"Cache-Control": "max-age=3600"}), io.BytesIO())
        client, opener = self.client([first, unchanged])
        client.fetch(now())
        points, _, checked = client.fetch(now() + timedelta(minutes=31))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("If-modified-since"), last_modified)
        self.assertEqual(request.get_header("If-none-match"), '"forecast-1"')
        self.assertEqual(checked, now() + timedelta(minutes=31))
        self.assertEqual(client.stats(now())["requests_today"], 2)
        self.assertEqual(client.stats(now())["body_bytes_today"], first.wire_size)
        self.assertTrue(points)

    def test_retry_after_is_persisted_and_repeated_clicks_do_not_retry_early(self):
        error = HTTPError("https://api.open-meteo.com", 429, "Limited", headers({"Retry-After": "7200"}), io.BytesIO())
        client, opener = self.client([error])
        with self.assertRaisesRegex(ValueError, "호출 한도"):
            client.fetch(now())
        with self.assertRaisesRegex(ValueError, "호출 한도"):
            record.PublicWeatherClient(self.condition, self.root, opener).fetch(now() + timedelta(hours=1))
        self.assertEqual(opener.open.call_count, 1)

    def test_met_requires_real_contact_syntax_before_any_network_request(self):
        condition = replace(self.condition, provider="met_no")
        client, opener = self.client([], condition)
        with self.assertRaisesRegex(ValueError, "연락용"):
            client.fetch(now())
        opener.open.assert_not_called()
        for contact in ("", "plain-name", "mail\n@example.org", "https://user:password@example.org"):
            with self.subTest(contact=contact), self.assertRaises(ValueError):
                record.met_user_agent(replace(condition, met_contact=contact))
        self.assertIn("person@example.org", record.met_user_agent(replace(condition, met_contact="person@example.org")))

    def test_met_request_uses_user_agent_and_at_most_four_coordinate_decimals(self):
        condition = replace(self.condition, provider="met_no", met_contact="person@example.org")
        client, opener = self.client([Response(met_body())], condition)
        points, issue, _ = client.fetch(now())
        request = opener.open.call_args.args[0]
        self.assertIn("person@example.org", request.get_header("User-agent"))
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)["lon"], ["129.1658"])
        self.assertEqual(issue.hour, 3)
        self.assertEqual(next(iter(points.values()))["time"], "2026-10-19T18:00:00+09:00")

    def test_met_period_intersection_handles_midnight_without_inventing_an_exact_rain_hour(self):
        points, _ = record.parse_met_forecast(met_body("sleet", 6, "2026-10-19T12:00:00Z"), now())
        point = next(iter(points.values()))
        self.assertEqual(point["period_hours"], 6)
        self.assertEqual(point["PTY"], 2)
        self.assertTrue(record.point_covers_day(point, now().date()))
        self.assertTrue(record.point_covers_day(point, (now() + timedelta(days=1)).date()))
        points, _ = record.parse_met_forecast(met_body("rain", 1, "2026-10-19T14:00:00Z"), now())
        self.assertFalse(record.point_covers_day(next(iter(points.values())), (now() + timedelta(days=1)).date()))

    def test_met_symbol_variants_and_unknown_values(self):
        self.assertEqual(record.met_symbol_weather("lightrainshowersandthunder_day")[0], 4)
        self.assertEqual(record.met_symbol_weather("heavysnow_night")[0], 3)
        self.assertEqual(record.met_symbol_weather("fair_polartwilight"), (0, 1, "맑음"))
        self.assertIsNone(record.met_symbol_weather("notrain"))

    def test_evening_open_meteo_rain_qualifies_whole_day_without_api_key(self):
        gate = record.WeatherGate(self.condition, self.root)
        gate.public_client, opener = self.client([Response(open_body())])
        with mock.patch.object(record, "weather_api_key", side_effect=AssertionError("No key should be requested")):
            gate.refresh(now())
        self.assertTrue(gate.allows_date(now().date(), now()))
        self.assertEqual(gate.snapshot(now())["source"], "Open-Meteo")
        self.assertEqual(gate.snapshot(now())["network"]["requests_today"], 1)
        self.assertEqual(gate.label_for_day(now().date()), "비예보일")

    def test_bad_json_and_network_error_are_safe_and_backed_off(self):
        response = Response(open_body())
        response = io.BytesIO(b"not json with arbitrary contents")
        response.headers = headers()
        response.getcode = lambda: 200
        client, opener = self.client([response])
        with self.assertRaises(ValueError) as error:
            client.fetch(now())
        self.assertNotIn("arbitrary", str(error.exception))
        with self.assertRaises(ValueError):
            client.fetch(now() + timedelta(minutes=1))
        self.assertEqual(opener.open.call_count, 1)

    def test_response_expansion_is_bounded(self):
        client, _ = self.client([])
        huge = gzip.compress(b"a" * (record.PUBLIC_WEATHER_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "응답이 너무"):
            client._decompress(huge, "gzip")

    def test_data_counter_resets_on_korean_date_change(self):
        client, opener = self.client([Response(open_body()), Response(open_body())])
        client.fetch(now())
        client.fetch(now() + timedelta(days=1))
        self.assertEqual(client.stats(now() + timedelta(days=1))["requests_today"], 1)


if __name__ == "__main__":
    unittest.main()
