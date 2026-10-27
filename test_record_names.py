"""Offline metadata, file publication, and retention tests for named recordings."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("record.py")
SPEC = importlib.util.spec_from_file_location("record_names_under_test", SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def now():
    return datetime(2026, 10, 13, 6, 0, 0, tzinfo=record.KST)


def camera():
    return record.Camera("cam1", "rtsp://user:PRIVATE@192.0.2.1/live", "29", "구미교차로", "North")


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-record-metadata-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.setup = self.root / "setup.ini"
        self.data = {"setup_ini": str(self.setup), "output_dir": str(self.root / "captures"),
                     "cameras": [{"name": "cam1", "section": "CAM1"}],
                     "schedules": [{"start": "06:00", "duration_hours": 5}]}
        self.write_setup("29")

    def write_setup(self, client_no):
        self.setup.write_text("[CAM1]\nINUSE=1\nCLIENTNO={}\nURL=rtsp://user:PRIVATE@192.0.2.1/live\n".format(client_no),
                              encoding="utf-8")

    def write_client(self, client_no="29", name="구미교차로", direction="1"):
        path = self.root / ("client" + client_no + ".ini")
        path.write_text("[INTERSECTION]\nNAME={}\nCAMDIR={}\n".format(name, direction), encoding="utf-8")

    def load(self):
        return record.settings_from_data(self.data, self.root).cameras[0]

    def test_clientno_selects_client29_not_camera1(self):
        self.write_client("1", "Wrong intersection", "4")
        self.write_client("29", "구미교차로", "1")
        result = self.load()
        self.assertEqual((result.client_no, result.intersection, result.direction), ("29", "구미교차로", "North"))
        self.assertEqual(record.camera_filename_prefix(result), "client29_구미교차로_North")
        self.assertNotIn("PRIVATE", repr(result))

    def test_leading_zero_client_number_resolves_normalized_metadata_filename(self):
        self.write_setup("00029")
        self.write_client("29", "구미교차로", "4")
        result = self.load()
        self.assertEqual((result.client_no, result.direction), ("29", "West"))
        self.assertTrue(record.camera_filename_prefix(result).startswith("client29_"))

    def test_all_eight_camera_directions_are_mapped(self):
        for code, expected in (("1", "North"), ("2", "East"), ("3", "South"), ("4", "West"),
                               ("5", "North-East"), ("6", "East-South"), ("7", "South-West"), ("8", "West-North")):
            with self.subTest(direction=code):
                self.write_client(direction=code)
                self.assertEqual(self.load().direction, expected)

    def test_missing_metadata_keeps_recording_camera_and_warns_once_without_credentials(self):
        with self.assertLogs(record.LOG, level="WARNING") as messages:
            result = self.load()
        self.assertEqual(result.client_no, "29")
        self.assertEqual(result.intersection, "UnknownIntersection")
        self.assertEqual(result.direction, "UnknownDirection")
        self.assertEqual(len(messages.output), 1)
        self.assertNotIn("PRIVATE", "\n".join(messages.output))
        self.assertEqual(result.url, "rtsp://user:PRIVATE@192.0.2.1/live")

    def test_invalid_clientno_never_guesses_camera_index(self):
        self.write_client("1", "Wrong intersection", "1")
        for client_no in ("", "bad", "-1", "29/../1", "1" * 17):
            with self.subTest(client_no=client_no), self.assertLogs(record.LOG, level="WARNING") as messages:
                self.write_setup(client_no)
                result = self.load()
                self.assertEqual(result.client_no, "Unknown")
                self.assertEqual(result.intersection, "UnknownIntersection")
                self.assertEqual(len(messages.output), 1)

    def test_invalid_metadata_fields_produce_one_combined_warning(self):
        self.write_client(name="", direction="99")
        with self.assertLogs(record.LOG, level="WARNING") as messages:
            result = self.load()
        self.assertEqual((result.client_no, result.intersection, result.direction),
                         ("29", "UnknownIntersection", "UnknownDirection"))
        self.assertEqual(len(messages.output), 1)

    def test_direct_url_legacy_config_uses_clear_unknown_metadata(self):
        self.data["cameras"] = [{"name": "custom_cam", "url": "rtsp://192.0.2.1/live"}]
        result = self.load()
        self.assertEqual((result.client_no, result.intersection, result.direction),
                         ("Unknown", "UnknownIntersection", "UnknownDirection"))
        self.assertEqual(record.camera_filename_prefix(result),
                         "clientUnknown_custom_cam_UnknownIntersection_UnknownDirection")


class FilenameTests(unittest.TestCase):
    def test_korean_name_is_preserved_but_unsafe_characters_are_removed(self):
        value = replace(camera(), intersection=' 구미/교차로\\이름:100%?*<>|"\n\t ')
        prefix = record.camera_filename_prefix(value)
        self.assertIn("구미", prefix)
        self.assertIn("교차로", prefix)
        self.assertTrue(prefix.startswith("client29_"))
        self.assertTrue(prefix.endswith("_North"))
        for char in '/\\:%?*<>|"\n\r\t':
            self.assertNotIn(char, prefix)
        self.assertEqual(Path(prefix).name, prefix)

    def test_long_unicode_metadata_stays_within_filename_byte_limit(self):
        prefix = record.camera_filename_prefix(replace(camera(), intersection="가나다" * 200))
        name = prefix + "_20261013_060000_KST_r12345678_999999.mp4"
        self.assertLessEqual(len(name.encode("utf-8")), 255)
        self.assertEqual(name.encode("utf-8").decode("utf-8"), name)
        self.assertTrue(record.NAMED_PART_NAME.fullmatch(name))

    def test_output_pattern_escapes_parent_percent_without_changing_frame_index(self):
        run = Path("root100%") / "run%06d"
        pattern = record.output_pattern(camera(), run)
        self.assertEqual(pattern,
                         str(run / "client29_구미교차로_North_pending_").replace("%", "%%") + "%06d.mp4")
        self.assertEqual(pattern % 7, str(run / "client29_구미교차로_North_pending_000007.mp4"))

    def test_part_index_accepts_legacy_pending_final_and_collision_names_only(self):
        for name, expected in (
            ("part_000001.mp4", 1), ("part_000002.mkv", 2),
            ("client29_구미교차로_North_pending_000003.mp4", 3),
            ("client29_구미교차로_North_20261013_060000_KST_000004.mp4", 4),
            ("client29_구미교차로_North_20261013_060000_KST_r12345678_000005.mp4", 5),
            ("holiday.mp4", None), ("client29_구미교차로_North_pending_000006.avi", None),
            ("client29_구미교차로_North_pending_6.mp4", None), ("part_000001.mp4.bak", None),
        ):
            with self.subTest(name=name):
                self.assertEqual(record.part_index(Path(name)), expected)


class SegmentNamerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-segment-names-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.prefix = "client29_구미교차로_North"
        self.namer = record.SegmentNamer(self.run, self.prefix)
        self.addCleanup(self.namer.close)

    def pending(self, index=0, content=b"recording"):
        path = self.run / (self.prefix + "_pending_{:06d}.mp4".format(index))
        path.write_bytes(content)
        return path

    def test_timestamp_uses_creation_observation_in_kst_and_preserves_contents(self):
        pending = self.pending()
        utc = datetime(2026, 10, 12, 21, 0, 0, tzinfo=timezone.utc)
        renamed = self.namer.rename_available(utc)
        self.assertEqual(renamed, [self.run / "client29_구미교차로_North_20261013_060000_KST_000000.mp4"])
        self.assertEqual(renamed[0].read_bytes(), b"recording")
        self.assertFalse(pending.exists())

    def test_same_time_and_clock_rollback_keep_distinct_sequence_numbers(self):
        self.pending(0, b"zero")
        self.pending(1, b"one")
        first = self.namer.rename_available(now())
        self.pending(2, b"two")
        second = self.namer.rename_available(now() - timedelta(hours=1))
        paths = first + second
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual([record.part_index(path) for path in paths], [0, 1, 2])
        self.assertEqual([path.read_bytes() for path in paths], [b"zero", b"one", b"two"])
        self.assertIn("_20261013_050000_KST_", second[0].name)

    def test_existing_final_filename_is_preserved_and_collision_gets_new_name(self):
        existing = self.run / (self.prefix + "_20261013_060000_KST_000000.mp4")
        existing.write_bytes(b"existing recording")
        self.pending(0, b"new recording")
        renamed = self.namer.rename_available(now())
        self.assertEqual(existing.read_bytes(), b"existing recording")
        self.assertNotEqual(renamed[0], existing)
        self.assertRegex(renamed[0].name, r"_KST_r[a-f0-9]{8}_000000\.mp4$")
        self.assertEqual(renamed[0].read_bytes(), b"new recording")

    def test_publication_error_preserves_pending_and_warns_once_while_retrying(self):
        pending = self.pending()
        with mock.patch.object(record, "ini_publish_file", side_effect=PermissionError("simulated storage denial")), \
                self.assertLogs(record.LOG, level="WARNING") as messages:
            self.namer._try_rename()
            self.namer._try_rename()
        self.assertEqual(len(messages.output), 1)
        self.assertEqual(pending.read_bytes(), b"recording")
        self.assertEqual(self.namer.next_index, 0)
        self.assertEqual(list(self.run.glob("*.mp4")), [pending])
        self.assertEqual(len(self.namer.rename_available(now())), 1)

    def test_publication_retry_keeps_first_observed_timestamp(self):
        self.pending()
        with mock.patch.object(record, "ini_publish_file", side_effect=PermissionError("temporary storage denial")):
            with self.assertRaises(PermissionError):
                self.namer.rename_available(now())
        renamed = self.namer.rename_available(now() + timedelta(hours=1))
        self.assertEqual(renamed[0].name, self.prefix + "_20261013_060000_KST_000000.mp4")

    @unittest.skipUnless(sys.platform == "linux", "POSIX hardlink publication and unlink recovery")
    def test_unlink_retry_reuses_published_target_without_duplicating_recording(self):
        pending = self.pending()
        original_unlink = Path.unlink

        def fail_pending_unlink(path, *args, **kwargs):
            if path == pending:
                raise PermissionError("temporary source unlink failure")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=fail_pending_unlink):
            with self.assertRaises(PermissionError):
                self.namer.rename_available(now())
        expected = self.run / (self.prefix + "_20261013_060000_KST_000000.mp4")
        self.assertTrue(pending.exists())
        self.assertTrue(expected.exists())
        self.assertTrue(os.path.samefile(pending, expected))
        renamed = self.namer.rename_available(now() + timedelta(hours=1))
        self.assertEqual(renamed, [expected])
        self.assertFalse(pending.exists())
        self.assertEqual(list(self.run.glob("*.mp4")), [expected])

    def test_close_performs_final_pass_when_watcher_was_not_started(self):
        pending = self.pending()
        self.namer.close()
        self.assertFalse(pending.exists())
        outputs = list(self.run.glob("*.mp4"))
        self.assertEqual(len(outputs), 1)
        self.assertEqual(record.part_index(outputs[0]), 0)

    def test_naive_timestamp_is_rejected_without_renaming(self):
        pending = self.pending()
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.namer.rename_available(datetime(2026, 10, 13, 6))
        self.assertTrue(pending.exists())

    @unittest.skipUnless(sys.platform == "linux", "Open recording descriptors are supported on Linux")
    def test_actual_open_descriptor_keeps_writing_after_name_changes(self):
        pending = self.run / (self.prefix + "_pending_000000.mp4")
        with pending.open("wb", buffering=0) as output:
            output.write(b"before-")
            renamed = self.namer.rename_available(now())
            self.assertFalse(pending.exists())
            output.write(b"after")
        self.assertEqual(renamed[0].read_bytes(), b"before-after")

    def test_recording_starts_and_finishes_naming_worker_with_its_process(self):
        process = mock.Mock(stdout=io.BytesIO(b"out_time_us=1\n"))
        namer = mock.Mock()
        command = ["ffmpeg", record.output_pattern(camera(), self.run)]
        with mock.patch.object(record.subprocess, "Popen", return_value=process), \
                mock.patch.object(record, "SegmentNamer", return_value=namer) as factory:
            recording = record.Recording(command, self.run)
            recording.finish()
        factory.assert_called_once_with(self.run, self.prefix)
        namer.start.assert_called_once_with()
        namer.close.assert_called_once_with()


class NamedRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-named-retention-", dir=SOURCE.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = record.Settings(self.root, (camera(),),
                                        (record.Schedule(6, 0, timedelta(hours=5)),), retention_days=7)
        self.storage = record.Storage(self.settings)
        self.run = self.storage.create_run(camera(), now() - timedelta(days=9))

    def part(self, name, old=True):
        path = self.run / name
        path.write_bytes(b"retain or delete")
        date = now() - timedelta(days=8 if old else 1)
        os.utime(path, (date.timestamp(), date.timestamp()))
        return path

    def test_closed_run_cleans_owned_legacy_pending_and_named_files_only(self):
        owned = [self.part(name) for name in (
            "part_000000.mkv", "part_000001.mp4",
            "client29_구미교차로_North_pending_000002.mp4",
            "client29_구미교차로_North_20261004_060000_KST_000003.mp4",
        )]
        unrelated = self.part("important-video.mp4")
        recent = self.part("client29_구미교차로_North_20261012_060000_KST_000004.mp4", old=False)
        self.assertEqual(self.storage.cleanup(now()), 4)
        self.assertTrue(all(not path.exists() for path in owned))
        self.assertTrue(unrelated.exists())
        self.assertTrue(recent.exists())

    def test_active_run_preserves_highest_index_ties_in_pending_and_final_names(self):
        closed = self.part("client29_구미교차로_North_20261004_060000_KST_000008.mp4")
        pending = self.part("client29_구미교차로_North_pending_000009.mp4")
        final = self.part("client29_구미교차로_North_20261004_070000_KST_000009.mp4")
        collision = self.part("client29_구미교차로_North_20261004_070000_KST_r12345678_000009.mp4")
        self.assertEqual(self.storage.cleanup(now(), [self.run]), 1)
        self.assertFalse(closed.exists())
        self.assertTrue(all(path.exists() for path in (pending, final, collision)))


if __name__ == "__main__":
    unittest.main()
