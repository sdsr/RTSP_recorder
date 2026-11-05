"""Offline IP camera editing regressions; never connect to a camera or change the network."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("camera_connection_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


class CameraConnectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix=".test-camera-connection-", dir=SOURCE.parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.setup = self.root / "setup.ini"
        self.setup.write_text("[CAM1]\nINUSE=True\nURL=rtsp://192.0.2.1/live\n", encoding="utf-8")
        self.data = {
            "setup_ini": str(self.setup), "output_dir": str(self.root / "captures"),
            "time_ranges": ["06:00-07:00"], "reconnect_seconds": 17,
            "cameras": [
                {"name": "existing_12", "url": "rtsp://admin:PRIVATE%21%40@169.254.130.12/profile2/media.smp",
                 "intersection_name": "교차로", "weather": "비"},
                {"name": "existing_13", "url": "rtsp://admin:PRIVATE%21%40@169.254.130.13/profile2/media.smp"},
            ],
        }
        self.config = self.root / "recording.json"
        self.write()
        self.controller = record.RecorderUiController(self.config)

    def write(self):
        self.config.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    def form(self, **updates):
        form = {"mode": "direct", "ips": "169.254.130.12\n169.254.130.13", "username": "admin",
                "password": "", "port": 554, "path": "/profile2/media.smp"}
        form.update(updates)
        return form

    def save(self, form, **updates):
        patch = dict(updates, camera_connection=form)
        # Editing must not discover INI cameras, create output folders, or invoke FFmpeg.
        with mock.patch.object(record, "find_setup_ini", side_effect=AssertionError("unexpected discovery")), \
                mock.patch.object(record, "check_ffmpeg", side_effect=AssertionError("unexpected FFmpeg")), \
                mock.patch.object(record, "check_output_access", side_effect=AssertionError("unexpected output write")):
            self.controller.save(patch, self.controller.configuration()["revision"])
        return json.loads(self.config.read_text(encoding="utf-8"))

    def test_direct_view_is_editable_and_does_not_expose_encoded_or_plain_password(self):
        result = self.controller.configuration()
        self.assertEqual(result["config"]["camera_connection"], dict(self.form(), password_saved=True))
        serialized = json.dumps(result)
        for secret in ("PRIVATE", "rtsp://", "%21%40"):
            self.assertNotIn(secret, serialized)

    def test_setup_auto_view_and_explicit_setup_switch_use_original_schema(self):
        self.data["cameras"] = "auto"
        self.write()
        self.assertEqual(self.controller.configuration()["config"]["camera_connection"]["mode"], "setup")
        self.assertEqual(self.save({"mode": "setup"})["cameras"], "auto")

    def test_nonrepresentable_urls_and_mixed_sources_are_custom_and_preserved(self):
        alternatives = [
            [{"name": "ini", "section": "CAM1"}, self.data["cameras"][0]],
            [{"name": "host", "url": "rtsp://admin:PRIVATE@camera.example/live"}],
            [{"name": "ipv6", "url": "rtsp://admin:PRIVATE@[2001:db8::1]/live"}],
            [self.data["cameras"][0], {"name": "other", "url": "rtsp://admin:DIFFERENT@169.254.130.13/live"}],
            [{"name": "one", "url": "rtsp://192.0.2.1/a"}, {"name": "two", "url": "rtsp://192.0.2.1/b"}],
        ]
        for cameras in alternatives:
            with self.subTest(cameras=len(cameras)):
                self.data["cameras"] = cameras
                self.write()
                form = self.controller.configuration()["config"]["camera_connection"]
                self.assertEqual(form["mode"], "custom")
                self.assertFalse(form["password_saved"])
                self.assertEqual(self.save({"mode": "custom"}, intersection_name="변경")["cameras"], cameras)

    def test_unchanged_direct_roundtrip_keeps_urls_names_labels_and_other_settings(self):
        saved = self.save(self.form())
        self.assertEqual(saved, self.data)
        self.assertNotIn("camera_connection", saved)
        self.assertFalse((self.root / "captures").exists())

    def test_ip_list_deduplicates_separators_and_reuses_common_saved_password(self):
        saved = self.save(self.form(ips="169.254.130.12, 169.254.130.14\n169.254.130.12\t169.254.130.15"))
        cameras = saved["cameras"]
        self.assertEqual([urlsplit(item["url"]).hostname for item in cameras],
                         ["169.254.130.12", "169.254.130.14", "169.254.130.15"])
        self.assertEqual(cameras[0], self.data["cameras"][0])
        self.assertEqual([unquote(urlsplit(item["url"]).password) for item in cameras], ["PRIVATE!@"] * 3)
        self.assertEqual([item["name"] for item in cameras], ["existing_12", "camera_14", "camera_15"])

    def test_credentials_special_characters_are_encoded_once(self):
        secret = "unit@!:/$%?# password"
        saved = self.save(self.form(username="user:name@", password=secret, port="8554"))
        url = saved["cameras"][0]["url"]
        parsed = urlsplit(url)
        self.assertEqual((unquote(parsed.username), unquote(parsed.password)), ("user:name@", secret))
        self.assertEqual((parsed.hostname, parsed.port), ("169.254.130.12", 8554))
        self.assertEqual(parsed.query, "")
        self.assertNotIn(secret, url)
        self.assertEqual(self.controller.configuration()["config"]["camera_connection"]["path"],
                         "/profile2/media.smp")
        self.assertNotIn(secret, json.dumps(self.controller.configuration()))

    def test_existing_query_urls_are_not_reflected_and_custom_save_preserves_them(self):
        for suffix in ("?user=admin&password=UNIT_SECRET", "?token=UNIT_SECRET", "?profile=2", "?"):
            with self.subTest(query_kind=suffix.split("=", 1)[0]):
                self.data["cameras"] = [{"name": "custom_query", "url": "rtsp://192.0.2.1/live" + suffix}]
                self.write()
                result = self.controller.configuration()
                self.assertEqual(result["config"]["camera_connection"]["mode"], "custom")
                self.assertNotIn("UNIT_SECRET", json.dumps(result))
                self.assertNotIn(suffix, json.dumps(result["config"]))
                self.assertEqual(self.save({"mode": "custom"}, intersection_name="변경")["cameras"],
                                 self.data["cameras"])

    def test_direct_query_path_is_rejected_without_exposing_its_secret(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "기존 개별 설정") as caught:
            self.save(self.form(path="/live?password=UNIT_SECRET"))
        self.assertNotIn("UNIT_SECRET", str(caught.exception))
        self.assertNotIn("UNIT_SECRET", record.friendly_ui_error(caught.exception))
        self.assertEqual(self.config.read_bytes(), before)

    def test_new_ip_requires_password_when_no_matching_common_secret_exists(self):
        self.data["cameras"] = [{"name": "old", "url": "rtsp://another:PRIVATE@192.0.2.1/live"}]
        self.write()
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "비밀번호"):
            self.save(self.form())
        self.assertEqual(self.config.read_bytes(), before)

    def test_new_ip_does_not_pick_a_secret_from_inconsistent_accounts(self):
        self.data["cameras"][1]["url"] = "rtsp://admin:DIFFERENT@169.254.130.13/profile2/media.smp"
        self.write()
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "비밀번호"):
            self.save(self.form(ips="169.254.130.14"))
        self.assertEqual(self.config.read_bytes(), before)
        # Explicit IP matches can still retain their own credentials without leaking either one.
        saved = self.save(self.form(port=8554))
        self.assertEqual([unquote(urlsplit(item["url"]).password) for item in saved["cameras"]],
                         ["PRIVATE!@", "DIFFERENT"])

    def test_username_change_cannot_silently_reuse_another_account_password(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "비밀번호"):
            self.save(self.form(username="other"))
        self.assertEqual(self.config.read_bytes(), before)

    def test_generated_names_are_unique_across_subnets_and_preserve_retained_names(self):
        self.data["cameras"][0]["name"] = "camera_12"
        self.write()
        saved = self.save(self.form(ips="192.0.2.12 169.254.130.12 198.51.100.12"))
        names = [item["name"] for item in saved["cameras"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[1], "camera_12")
        self.assertTrue(all(record.NAME.fullmatch(name) for name in names))

    def test_unauthenticated_target_can_be_explicitly_configured(self):
        saved = self.save(self.form(ips="192.0.2.5", username=""))
        self.assertEqual(saved["cameras"][0]["url"], "rtsp://192.0.2.5:554/profile2/media.smp")
        self.assertFalse(self.controller.configuration()["config"]["camera_connection"]["password_saved"])

    def test_invalid_connection_values_never_reach_disk_or_echo_credentials(self):
        invalid = [
            {"ips": ""}, {"ips": "169.254.130.12-14"}, {"ips": "camera.example"},
            {"ips": "169.254.130.256"}, {"ips": "2001:db8::1"},
            {"ips": "rtsp://user:PRIVATE_CREDENTIAL@169.254.130.12/live"},
            {"port": 0}, {"port": 65536}, {"port": True}, {"port": 554.5}, {"port": "554.0"},
            {"path": "profile2/media.smp"}, {"path": "//host/live"}, {"path": "/live#fragment"},
            {"path": "/live\nPRIVATE_CREDENTIAL"}, {"path": "/live?token=%zz"},
            {"password": "PRIVATE_CREDENTIAL\n"}, {"username": "user\x00PRIVATE_CREDENTIAL"},
            {"username": "", "password": "PRIVATE_CREDENTIAL"}, {"mode": "invalid"},
        ]
        before = self.config.read_bytes()
        for update in invalid:
            with self.subTest(field=next(iter(update))):
                with self.assertRaises(ValueError) as caught:
                    self.save(self.form(**update))
                self.assertNotIn("PRIVATE_CREDENTIAL", str(caught.exception))
                self.assertNotIn("rtsp://user:", str(caught.exception))
                self.assertEqual(self.config.read_bytes(), before)

    def test_direct_save_reports_explicit_output_folder_requirement(self):
        before = self.config.read_bytes()
        with self.assertRaises(ValueError) as caught:
            self.save(self.form(), output_dir="auto")
        self.assertIn("저장 폴더", record.friendly_ui_error(caught.exception))
        self.assertEqual(self.config.read_bytes(), before)

    def test_conflicting_revision_cannot_overwrite_camera_credentials(self):
        revision = self.controller.configuration()["revision"]
        self.data["cameras"][0]["url"] = "rtsp://admin:CHANGED@169.254.130.12/live"
        self.write()
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "다른 곳에서 변경"):
            self.controller.save({"camera_connection": self.form(password="STALE")}, revision)
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
