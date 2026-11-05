"""Recording-library regressions using temporary files and a loopback HTTP server."""
import base64
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("recording_library_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


class LibraryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".test-recording-library-", dir=SOURCE.parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "videos"
        self.output.mkdir()
        self.config = self.root / "recording.json"
        self.data = {"output_dir": str(self.output), "time_ranges": ["06:00-07:00"],
                     "cameras": [{"name": "camera_12", "url": "rtsp://192.0.2.12/live"}]}
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        self.controller = record.RecorderUiController(self.config)
        self.original_writers = record.recording_writer_inodes
        self.writers = mock.patch.object(record, "recording_writer_inodes", return_value=(set(), False)).start()
        self.addCleanup(mock.patch.stopall)
        self.relative = "camera_12/2026-11-05/run_test/영상.mp4"
        self.video = self.make(self.relative, b"0123456789abcdefghijklmnopqrstuvwxyz")
        self.identifier = self.controller.recordings()["items"][0]["id"]

    def scoped(self, relative, info=None, trash=False):
        return record.recording_scoped_id(relative, self.output.stat(), info or self.video.stat(), trash)

    def trash_path(self, identifier):
        directory, _ = record.recording_scope_from_id(identifier, self.output.stat(), trash=True)
        return self.output / record.RECORDING_TRASH / directory

    def make(self, relative, content=b"video"):
        path = self.output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def status(self, cameras, age=0):
        value = {"running": True, "active_cameras": cameras,
                 "updated_at": (datetime.now(record.KST) - timedelta(seconds=age)).isoformat()}
        (self.output / record.STATUS_NAME).write_text(json.dumps(value), encoding="utf-8")


class RecordingLibraryTests(LibraryFixture):
    def test_list_filters_paginates_and_has_no_camera_credentials(self):
        self.make("camera_13/2026-11-04/run_other/part.mp4")
        self.make("camera_12/2026-11-04/run_other/part.mp4")
        result = self.controller.recordings(limit=1)
        self.assertEqual((result["total"], len(result["items"]), result["limit"]), (3, 1, 1))
        self.assertEqual(result["cameras"], ["camera_12", "camera_13"])
        self.assertEqual(result["dates"], ["2026-11-05", "2026-11-04"])
        matching = self.controller.recordings(camera="camera_12", day="2026-11-05")
        item = matching["items"][0]
        self.assertEqual((matching["total"], item["id"], item["relative_path"], item["size"]),
                         (1, self.identifier, self.relative, self.video.stat().st_size))
        self.assertFalse(item["active"])
        self.assertNotIn("rtsp://", json.dumps(result))
        self.assertEqual(self.controller.recordings(offset=5)["items"], [])

    def test_listing_excludes_non_video_hidden_files_and_all_symlinks(self):
        self.make("camera_12/config.json")
        self.make(".hidden/hidden.mp4")
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"OUTSIDE")
        (self.output / "link.mp4").symlink_to(outside)
        (self.output / "linked_directory").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.controller.recordings()["total"], 1)

    def test_nonexistent_output_is_empty_and_not_created(self):
        self.data["output_dir"] = str(self.root / "not-created")
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        self.assertEqual(self.controller.recordings()["total"], 0)
        self.assertFalse((self.root / "not-created").exists())

    def test_output_root_symlink_is_rejected(self):
        link = self.root / "linked-root"
        link.symlink_to(self.output, target_is_directory=True)
        self.data["output_dir"] = str(link)
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "심볼릭"):
            self.controller.recordings()

    def test_invalid_ids_cannot_escape_or_open_hidden_non_mp4_paths(self):
        for path in ("../outside.mp4", "/tmp/outside.mp4", "a/../../outside.mp4", "a//video.mp4",
                     "a/./video.mp4", "a\\video.mp4", ".recorder-trash/id/video.mp4", "config.json"):
            bad = base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.controller.recording_media(bad)
        for bad in (None, "", "..", "not-valid-===", "%2e%2e"):
            with self.subTest(identifier=bad), self.assertRaises(ValueError):
                self.controller.trash_recording(bad)

    def test_media_and_trash_reject_file_and_ancestor_symlinks(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"OUTSIDE")
        (self.output / "link.mp4").symlink_to(outside)
        folder = self.output / "alias"
        folder.symlink_to(self.root, target_is_directory=True)
        for relative in ("link.mp4", "alias/outside.mp4"):
            identifier = self.scoped(relative)
            with self.subTest(relative=relative), self.assertRaises((OSError, ValueError)):
                self.controller.recording_media(identifier)
            with self.assertRaises((OSError, ValueError)):
                self.controller.trash_recording(identifier)
        self.assertEqual(outside.read_bytes(), b"OUTSIDE")

    def test_trash_and_restore_keep_bytes_and_return_original_location(self):
        original = self.video.read_bytes()
        moved = self.controller.trash_recording(self.identifier)
        self.assertFalse(self.video.exists())
        self.assertEqual(self.controller.recordings()["total"], 0)
        trash = self.controller.recordings(trash=True)
        self.assertEqual(trash["total"], 1)
        self.assertEqual(trash["items"][0]["id"], moved["id"])
        self.assertEqual(trash["items"][0]["relative_path"], self.relative)
        meta = self.trash_path(moved["id"]) / "metadata.json"
        self.assertEqual(meta.stat().st_mode & 0o777, 0o600)
        restored = self.controller.restore_recording(moved["id"])
        self.assertEqual(restored["id"], self.identifier)
        self.assertEqual(self.video.read_bytes(), original)
        self.assertEqual(self.controller.recordings(trash=True)["total"], 0)

    def test_restore_never_overwrites_an_existing_file(self):
        moved = self.controller.trash_recording(self.identifier)
        self.video.write_bytes(b"NEWER FILE")
        with self.assertRaisesRegex(ValueError, "덮어쓰지"):
            self.controller.restore_recording(moved["id"])
        self.assertEqual(self.video.read_bytes(), b"NEWER FILE")
        self.assertEqual(self.controller.recordings(trash=True)["total"], 1)

    def test_restore_rejects_metadata_path_escape(self):
        moved = self.controller.trash_recording(self.identifier)
        metadata = self.trash_path(moved["id"]) / "metadata.json"
        metadata.write_text(json.dumps({"relative_path": "../outside.mp4"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.controller.restore_recording(moved["id"])
        self.assertFalse((self.root / "outside.mp4").exists())
        self.assertTrue(metadata.with_name("video.mp4").is_file())

    def test_restore_rejects_symlink_target_directory_and_symlink_trash_video(self):
        moved = self.controller.trash_recording(self.identifier)
        self.video.parent.rmdir()
        self.video.parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.controller.restore_recording(moved["id"])
        self.assertFalse((self.root / self.video.name).exists())
        payload = self.trash_path(moved["id"]) / "video.mp4"
        payload.unlink()
        outside = self.root / "original.mp4"
        outside.write_bytes(b"OUTSIDE")
        payload.symlink_to(outside)
        with self.assertRaises(OSError):
            self.controller.restore_recording(moved["id"])
        self.assertEqual(outside.read_bytes(), b"OUTSIDE")

    def test_trash_root_symlink_and_non_trash_restore_ids_are_rejected(self):
        (self.output / record.RECORDING_TRASH).symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.controller.trash_recording(self.identifier)
        self.assertTrue(self.video.is_file())
        for identifier in (self.identifier, "../video.mp4", "/tmp/video.mp4", None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.controller.restore_recording(identifier)

    def test_active_camera_files_are_protected_by_status(self):
        self.status(["camera_12"])
        self.assertTrue(self.controller.recordings()["items"][0]["active"])
        with self.assertRaisesRegex(ValueError, "보호"):
            self.controller.trash_recording(self.identifier)
        self.assertTrue(self.video.is_file())

    def test_writable_descriptor_and_unknown_status_protect_files(self):
        info = self.video.stat()
        for value in (({(info.st_dev, info.st_ino)}, False), (set(), True)):
            self.writers.return_value = value
            with self.subTest(protection=value), self.assertRaisesRegex(ValueError, "보호"):
                self.controller.trash_recording(self.identifier)
        self.assertTrue(self.video.is_file())

    def test_actual_open_write_descriptor_is_detected(self):
        with self.video.open("ab"):
            inodes, _ = self.original_writers([(self.relative, self.video.stat())])
        info = self.video.stat()
        self.assertIn((info.st_dev, info.st_ino), inodes)

    def test_closed_file_is_not_blocked_by_unrelated_inaccessible_processes(self):
        inodes, uncertain = self.original_writers([(self.relative, self.video.stat())])
        self.assertEqual(inodes, set())
        self.assertFalse(uncertain)

    def test_source_recreated_after_atomic_move_is_never_unlinked(self):
        original = record.recording_rename_exclusive
        old_bytes = self.video.read_bytes()

        def racing_move(source_fd, source_name, target_fd, target_name):
            original(source_fd, source_name, target_fd, target_name)
            self.video.write_bytes(b"NEW SOURCE")

        with mock.patch.object(record, "recording_rename_exclusive", side_effect=racing_move):
            moved = self.controller.trash_recording(self.identifier)
        self.assertEqual(self.video.read_bytes(), b"NEW SOURCE")
        self.assertEqual((self.trash_path(moved["id"]) / "video.mp4").read_bytes(), old_bytes)

    def test_source_replaced_before_move_is_rolled_back_without_deleting_it(self):
        original = record.recording_rename_exclusive
        saved = self.root / "saved-original.mp4"
        calls = []

        def racing_move(source_fd, source_name, target_fd, target_name):
            if not calls:
                calls.append(True)
                self.video.rename(saved)
                self.video.write_bytes(b"REPLACEMENT")
            original(source_fd, source_name, target_fd, target_name)

        with mock.patch.object(record, "recording_rename_exclusive", side_effect=racing_move):
            with self.assertRaisesRegex(ValueError, "이동 중"):
                self.controller.trash_recording(self.identifier)
        self.assertEqual(self.video.read_bytes(), b"REPLACEMENT")
        self.assertEqual(saved.read_bytes(), b"0123456789abcdefghijklmnopqrstuvwxyz")

    def test_rollback_collision_preserves_both_raced_files(self):
        original = record.recording_rename_exclusive
        saved = self.root / "saved-original.mp4"
        calls = []

        def racing_move(source_fd, source_name, target_fd, target_name):
            if not calls:
                calls.append(True)
                self.video.rename(saved)
                self.video.write_bytes(b"REPLACEMENT")
                original(source_fd, source_name, target_fd, target_name)
                self.video.write_bytes(b"THIRD FILE")
            else:
                original(source_fd, source_name, target_fd, target_name)

        with mock.patch.object(record, "recording_rename_exclusive", side_effect=racing_move):
            with self.assertRaisesRegex(ValueError, "이동 중"):
                self.controller.trash_recording(self.identifier)
        self.assertEqual(self.video.read_bytes(), b"THIRD FILE")
        payloads = list((self.output / record.RECORDING_TRASH).glob("*/video.mp4"))
        self.assertEqual([path.read_bytes() for path in payloads], [b"REPLACEMENT"])
        self.assertEqual(saved.read_bytes(), b"0123456789abcdefghijklmnopqrstuvwxyz")

    def test_raced_destination_is_never_overwritten(self):
        original = record.recording_rename_exclusive
        before = self.video.read_bytes()

        def racing_move(source_fd, source_name, target_fd, target_name):
            fd = os.open(target_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=target_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"RACED TARGET")
            original(source_fd, source_name, target_fd, target_name)

        with mock.patch.object(record, "recording_rename_exclusive", side_effect=racing_move):
            with self.assertRaisesRegex(ValueError, "덮어쓰지"):
                self.controller.trash_recording(self.identifier)
        self.assertEqual(self.video.read_bytes(), before)
        payloads = list((self.output / record.RECORDING_TRASH).glob("*/video.mp4"))
        self.assertEqual([path.read_bytes() for path in payloads], [b"RACED TARGET"])

    def test_old_list_id_cannot_target_a_different_output_root(self):
        other = self.root / "other-output"
        replacement = other / self.relative
        replacement.parent.mkdir(parents=True)
        replacement.write_bytes(b"OTHER ROOT")
        self.data["output_dir"] = str(other)
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        for operation in (self.controller.recording_media, self.controller.trash_recording):
            with self.assertRaisesRegex(ValueError, "새로고침"):
                operation(self.identifier)
        self.assertEqual(replacement.read_bytes(), b"OTHER ROOT")
        self.assertTrue(self.video.is_file())

    def test_old_trash_id_cannot_restore_from_a_different_output_root(self):
        moved = self.controller.trash_recording(self.identifier)
        payload = self.trash_path(moved["id"]) / "video.mp4"
        other = self.root / "other-output"
        other.mkdir()
        self.data["output_dir"] = str(other)
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "새로고침"):
            self.controller.restore_recording(moved["id"])
        self.assertTrue(payload.is_file())
        self.assertEqual(list(other.iterdir()), [])

    def test_old_list_id_cannot_move_a_replaced_file_at_the_same_path(self):
        self.video.rename(self.root / "old.mp4")
        self.video.write_bytes(b"NEW FILE")
        for operation in (self.controller.recording_media, self.controller.trash_recording):
            with self.assertRaisesRegex(ValueError, "변경"):
                operation(self.identifier)
        self.assertEqual(self.video.read_bytes(), b"NEW FILE")

    def test_live_recorder_lock_without_fresh_status_protects_files(self):
        import fcntl
        with (self.output / ".recorder.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "보호"):
                self.controller.trash_recording(self.identifier)
        self.assertTrue(self.video.is_file())

    def test_stale_status_does_not_claim_a_camera_is_currently_recording(self):
        self.status(["camera_12"], age=60)
        self.assertFalse(self.controller.recordings()["items"][0]["active"])

    def test_invalid_page_and_date_inputs_are_rejected(self):
        for values in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "bad"},
                       {"limit": True}, {"day": "2026-02-30"}, {"day": "20261105"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.controller.recordings(**values)


class RecordingLibraryHttpTests(LibraryFixture):
    def setUp(self):
        super().setUp()
        self.server = record.ThreadingHTTPServer(("127.0.0.1", 0), record.BaseHTTPRequestHandler)
        self.port = self.server.server_port
        self.token = "library-test-session"
        self.server.RequestHandlerClass = record.make_ui_handler(self.controller, self.token, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, headers=None, data=None, method=None):
        request = Request("http://127.0.0.1:{}{}".format(self.port, path), headers=headers or {},
                          data=json.dumps(data).encode() if data is not None else None, method=method)
        return urlopen(request, timeout=5)

    def media_path(self, **updates):
        values = {"id": self.identifier, "token": self.token}
        values.update(updates)
        return "/api/recordings/media?" + urlencode(values)

    def test_list_needs_header_token_and_media_query_token_does_not_authorize_other_apis(self):
        for path in ("/api/recordings", "/api/recordings?token=" + self.token, "/api/config?token=" + self.token):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                self.request(path)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        with self.request("/api/recordings", {"X-Recorder-Token": self.token}) as response:
            self.assertEqual(json.loads(response.read())["total"], 1)

    def test_full_media_query_auth_and_download_headers(self):
        with self.request(self.media_path(download="1"), {"Accept-Encoding": "gzip"}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.video.read_bytes())
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertIsNone(response.headers.get("Content-Encoding"))
            self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
            self.assertIn("media-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_byte_range_suffix_open_ended_and_head(self):
        content = self.video.read_bytes()
        for header, expected in (("bytes=2-5", content[2:6]), ("bytes=4-", content[4:]), ("bytes=-5", content[-5:])):
            with self.subTest(header=header), self.request(self.media_path(), {"Range": header}) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), expected)
                self.assertEqual(int(response.headers["Content-Length"]), len(expected))
                self.assertTrue(response.headers["Content-Range"].endswith("/" + str(len(content))))
        with self.request(self.media_path(), {"Range": "bytes=2-5"}, method="HEAD") as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.headers["Content-Length"], "4")

    def test_invalid_ranges_return_416_without_streaming_file(self):
        for header in ("bytes=1000-", "bytes=9-2", "bytes=0-1,4-5", "bytes=-0", "no-range"):
            with self.subTest(header=header), self.assertRaises(HTTPError) as caught:
                self.request(self.media_path(), {"Range": header})
            self.assertEqual(caught.exception.code, 416)
            self.assertEqual(caught.exception.headers["Content-Range"], "bytes */36")
            caught.exception.close()

    def test_media_wrong_token_origin_and_rebound_host_are_rejected(self):
        attempts = [(self.media_path(token="wrong"), {}),
                    (self.media_path(), {"Origin": "https://other.invalid"}),
                    (self.media_path(), {"Host": "other.invalid"})]
        for path, headers in attempts:
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as caught:
                self.request(path, headers)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()

    def test_trash_restore_post_requires_csrf_header_and_roundtrips_bytes(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/recordings/trash", {"Content-Type": "application/json"}, {"id": self.identifier})
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        self.assertTrue(self.video.exists())
        headers = {"Content-Type": "application/json", "X-Recorder-Token": self.token}
        with self.request("/api/recordings/trash", headers, {"id": self.identifier}) as response:
            moved = json.loads(response.read())
        self.assertFalse(self.video.exists())
        with self.request("/api/recordings?trash=1", {"X-Recorder-Token": self.token}) as response:
            self.assertEqual(json.loads(response.read())["items"][0]["id"], moved["id"])
        with self.request("/api/recordings/restore", headers, {"id": moved["id"]}) as response:
            self.assertEqual(json.loads(response.read())["id"], self.identifier)
        self.assertTrue(self.video.exists())


if __name__ == "__main__":
    unittest.main()
