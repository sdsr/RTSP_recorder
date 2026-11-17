"""Snapshot regressions using synthetic PNGs and inert subprocesses, never cameras."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib


SOURCE = Path(__file__).with_name('record.py')
SPEC = importlib.util.spec_from_file_location('camera_snapshots_under_test', SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def png_chunk(kind, content):
    return struct.pack('>I', len(content)) + kind + content + struct.pack('>I', zlib.crc32(kind + content) & 0xffffffff)


def png(width=2, height=1):
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    pixels = b'\x00' + b'\x10\x20\x30' * max(1, width)
    return (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', header) +
            png_chunk(b'IDAT', zlib.compress(pixels * max(1, height))) + png_chunk(b'IEND', b''))


class CaptureInput(io.BytesIO):
    def close(self):
        if not self.closed:
            self.captured = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, data=b'result', code=0):
        self.stdout = io.BytesIO(data)
        self.stdin = CaptureInput()
        self.code = code
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def kill(self):
        self.killed = True
        self.code = 0


class BlockingOutput:
    def __init__(self):
        self.started = threading.Event()
        self.released = threading.Event()
        self.closed = False

    def read(self, maximum):
        self.started.set()
        self.released.wait(3)
        return b''

    def close(self):
        self.closed = True
        self.released.set()


class BlockingProcess(FakeProcess):
    def __init__(self):
        super().__init__(code=None)
        self.stdout = BlockingOutput()

    def terminate(self):
        super().terminate()
        self.stdout.released.set()


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='snapshot-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'recording.json'
        self.config.write_bytes(b'{"keep_existing_configuration":true}\n')
        self.before = self.config.read_bytes()
        self.camera = record.Camera('camera_12', 'rtsp://admin:PRIVATE_PASSWORD@192.0.2.12:554/profile2/media.smp')
        self.settings = SimpleNamespace(cameras=(self.camera,), ffmpeg='/unused/recorder', min_free_gb=0)
        self.provider = mock.Mock(return_value=self.settings)
        self.manager = record.CameraSnapshotManager(self.provider, self.config)
        self.addCleanup(self.manager.close)
        record._SNAP_FFMPEG_CACHE.clear()
        self.addCleanup(record._SNAP_FFMPEG_CACHE.clear)

    def capture(self, profile='current', data=None):
        data = png() if data is None else data
        with mock.patch.object(record, 'check_ffmpeg', return_value=('/bundled/recorder', '-timeout')), \
                mock.patch.object(record, '_snapshot_choose_ffmpeg', return_value='/system/decoder'), \
                mock.patch.object(self.manager, '_capture_bytes', side_effect=[b'one-frame-mp4', data]) as capture:
            metadata = self.manager.capture('camera_12', profile)
        return metadata, capture


class SnapshotParserTests(unittest.TestCase):
    def test_png_dimensions_and_crc_are_checked(self):
        image = png(3, 2)
        self.assertEqual(record._snapshot_png_info(image), (3, 2))
        damaged = bytearray(image)
        damaged[28] ^= 1
        with self.assertRaises(record.CameraSnapshotError):
            record._snapshot_png_info(bytes(damaged))

    def test_incomplete_or_oversized_images_are_rejected(self):
        for invalid in (b'', b'not a png', png()[:-1], png() + b'extra', png(0, 1), png(16385, 1)):
            with self.subTest(length=len(invalid)), self.assertRaises(record.CameraSnapshotError):
                record._snapshot_png_info(invalid)
        with mock.patch.object(record, '_SNAP_MAX_BYTES', len(png()) - 1), self.assertRaises(record.CameraSnapshotError):
            record._snapshot_png_info(png())


class SnapshotCapabilityTests(SnapshotFixture):
    def capabilities(self, args, **kwargs):
        good = args[0].endswith('decoder')
        if args[-1] == '-decoders':
            text = ' VFS..D h264 H.264\n VFS..D hevc HEVC\n' if good else ''
        else:
            text = ' VF.... png PNG image\n' if good else ''
        return SimpleNamespace(returncode=0, stdout=text)

    def test_remux_binary_is_kept_for_recording_and_decoder_selected_locally(self):
        bundled, decoder = self.root / 'remux', self.root / 'decoder'
        bundled.write_bytes(b'remux'); decoder.write_bytes(b'decoder')
        self.settings.ffmpeg = str(bundled)
        with mock.patch.object(record._snap_shutil, 'which', side_effect=[str(bundled), str(decoder), str(decoder)]), \
                mock.patch.object(record._snap_subprocess, 'run', side_effect=self.capabilities) as run:
            selected = record._snapshot_choose_ffmpeg(self.settings)
        self.assertEqual(selected, str(decoder.resolve()))
        self.assertEqual(self.settings.ffmpeg, str(bundled))
        self.assertEqual(len(run.call_args_list), 4)
        self.assertTrue(all(call.args[0][-1] in ('-decoders', '-encoders') for call in run.call_args_list))
        self.assertNotIn('rtsp://', repr(run.call_args_list))
        with mock.patch.object(record._snap_shutil, 'which', return_value=str(decoder)), \
                mock.patch.object(record._snap_subprocess, 'run') as run:
            self.assertEqual(record._snapshot_choose_ffmpeg(self.settings), str(decoder.resolve()))
        run.assert_not_called()

    def test_missing_decoder_fails_before_camera_capture_or_storage(self):
        with mock.patch.object(record._snap_shutil, 'which', return_value=None), \
                mock.patch.object(record._snap_subprocess, 'run') as run, self.assertRaisesRegex(record.CameraSnapshotError, '일반 FFmpeg'):
            record._snapshot_choose_ffmpeg(self.settings)
        run.assert_not_called()
        self.assertFalse((self.root / 'snapshots').exists())

    def test_failed_capability_command_does_not_leak_output(self):
        binary = self.root / 'decoder'; binary.write_bytes(b'test')
        with mock.patch.object(record._snap_shutil, 'which', return_value=str(binary)), \
                mock.patch.object(record._snap_subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='PRIVATE_PASSWORD')), \
                self.assertRaises(record.CameraSnapshotError) as error:
            record._snapshot_choose_ffmpeg(self.settings)
        self.assertNotIn('PRIVATE_PASSWORD', str(error.exception))


class SnapshotProcessTests(SnapshotFixture):
    def test_capture_bytes_delivers_input_and_closes_pipes(self):
        process = FakeProcess(b'converted')
        with mock.patch.object(record._snap_subprocess, 'Popen', return_value=process) as popen:
            result = self.manager._capture_bytes(['decoder', '-i', 'pipe:0'], b'input-container')
        self.assertEqual(result, b'converted')
        self.assertEqual(process.stdin.captured, b'input-container')
        self.assertTrue(process.stdout.closed)
        self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertIsNone(self.manager._process)

    def test_nonzero_exit_and_spawn_errors_are_safe(self):
        for side_effect, value in ((None, FakeProcess(b'PRIVATE_PASSWORD', code=1)), (OSError('PRIVATE_PASSWORD'), None)):
            with mock.patch.object(record._snap_subprocess, 'Popen', side_effect=side_effect, return_value=value), \
                    self.assertRaises(record.CameraSnapshotError) as error:
                self.manager._capture_bytes(['unused', 'rtsp://admin:PRIVATE_PASSWORD@192.0.2.12/live'])
            self.assertNotIn('PRIVATE_PASSWORD', str(error.exception))
            self.assertNotIn('rtsp://', str(error.exception))

    def test_oversized_stream_is_stopped_without_returning_bytes(self):
        process = FakeProcess(b'a' * 65, code=None)
        with mock.patch.object(record, '_SNAP_MAX_BYTES', 64), \
                mock.patch.object(record._snap_subprocess, 'Popen', return_value=process), \
                self.assertRaises(record.CameraSnapshotError):
            self.manager._capture_bytes(['unused'])
        self.assertTrue(process.terminated)
        self.assertTrue(process.stdout.closed)

    def test_stalled_stdout_is_bounded_and_process_is_stopped(self):
        process = BlockingProcess()
        started = time.monotonic()
        with mock.patch.object(record._snap_subprocess, 'Popen', return_value=process), self.assertRaises(record.CameraSnapshotError):
            self.manager._capture_bytes(['unused'], deadline=time.monotonic() + .03)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(process.terminated)
        self.assertIsNone(self.manager._process)

    def test_close_cancels_a_running_capture(self):
        process = BlockingProcess(); errors = []
        def capture():
            try: self.manager._capture_bytes(['unused'])
            except Exception as exc: errors.append(exc)
        with mock.patch.object(record._snap_subprocess, 'Popen', return_value=process):
            thread = threading.Thread(target=capture)
            thread.start()
            self.assertTrue(process.stdout.started.wait(1))
            self.manager.close(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], record.CameraSnapshotError)
        self.assertTrue(process.terminated)

    def test_expired_deadline_or_bad_input_does_not_spawn(self):
        with mock.patch.object(record._snap_subprocess, 'Popen') as popen:
            with self.assertRaises(record.CameraSnapshotError):
                self.manager._capture_bytes(['unused'], deadline=time.monotonic() - 1)
            with self.assertRaises(record.CameraSnapshotError):
                self.manager._capture_bytes(['unused'], input_data='not bytes')
        popen.assert_not_called()

    def test_real_inert_child_output_is_collected(self):
        image = png()
        command = [sys.executable, '-c', 'import base64,sys;sys.stdout.buffer.write(base64.b64decode(sys.argv[1]))',
                   base64.b64encode(image).decode('ascii')]
        self.assertEqual(self.manager._capture_png(command), image)
        self.assertIsNone(self.manager._process)


class SnapshotPlatformTests(SnapshotFixture):
    def test_non_linux_preview_constructs_without_io_but_capture_fails_before_camera_work(self):
        with mock.patch.object(record._snap_sys, 'platform', 'win32'), \
                mock.patch.object(record, 'check_ffmpeg') as ffmpeg:
            manager = record.CameraSnapshotManager(self.provider, self.config)
            with self.assertRaisesRegex(record.CameraSnapshotError, 'Linux'):
                manager.capture('camera_12')
            with self.assertRaisesRegex(record.CameraSnapshotError, 'Linux'):
                manager.list()
            manager.close()
        self.provider.assert_not_called(); ffmpeg.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.before)


@unittest.skipUnless(sys.platform == 'linux', 'Snapshot directory descriptors require Linux')
class SnapshotStorageTests(SnapshotFixture):
    def test_capture_separates_network_auth_from_decoder_and_preserves_config(self):
        metadata, calls = self.capture(profile='4')
        first, second = calls.call_args_list
        network, decode = first.args[0], second.args[0]
        self.assertEqual(network[0], '/bundled/recorder')
        self.assertIn('rtsp://admin:PRIVATE_PASSWORD@192.0.2.12:554/profile4/media.smp', network)
        self.assertEqual(network[network.index('-c:v') + 1], 'copy')
        self.assertEqual(decode[0], '/system/decoder')
        self.assertEqual(decode[decode.index('-i') + 1], 'pipe:0')
        self.assertEqual(second.args[1], b'one-frame-mp4')
        self.assertEqual(first.kwargs['deadline'], second.args[2])
        self.assertNotIn('PRIVATE_PASSWORD', json.dumps(metadata))
        self.assertNotIn('rtsp://', repr(decode))
        self.assertEqual((metadata['width'], metadata['height'], metadata['profile']), (2, 1, '4'))
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertEqual(self.camera.url, 'rtsp://admin:PRIVATE_PASSWORD@192.0.2.12:554/profile2/media.smp')
        images = list((self.root / 'snapshots').rglob('*.png'))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].read_bytes(), png())
        self.assertEqual(images[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'snapshots').stat().st_mode & 0o777, 0o700)

    def test_list_and_media_survive_a_new_manager(self):
        metadata, _ = self.capture()
        manager = record.CameraSnapshotManager(self.provider, self.config)
        self.addCleanup(manager.close)
        self.assertEqual(manager.list(), [metadata])
        self.assertEqual(manager.media(metadata['id']), (metadata, png()))
        for invalid in ('../escape', 'g' * 32, '', None):
            with self.subTest(value=invalid), self.assertRaises(record.CameraSnapshotError):
                manager.media(invalid)

    def test_unregistered_camera_busy_or_closed_rejects_before_process_start(self):
        with mock.patch.object(record._snap_subprocess, 'Popen') as popen:
            with self.assertRaises(record.CameraSnapshotError):
                self.manager.capture('not_saved')
            self.manager._capture_lock.acquire()
            try:
                with self.assertRaises(record.CameraSnapshotError):
                    self.manager.capture('camera_12')
            finally: self.manager._capture_lock.release()
            self.manager.close()
            with self.assertRaises(record.CameraSnapshotError):
                self.manager.capture('camera_12')
        popen.assert_not_called()

    def test_decoder_missing_and_low_space_prevent_network_capture(self):
        with mock.patch.object(record, 'check_ffmpeg', return_value=('recorder', '-timeout')), \
                mock.patch.object(record, '_snapshot_choose_ffmpeg', side_effect=record.CameraSnapshotError('decoder required')), \
                mock.patch.object(self.manager, '_capture_bytes') as capture, self.assertRaises(record.CameraSnapshotError):
            self.manager.capture('camera_12')
        capture.assert_not_called()
        self.assertFalse((self.root / 'snapshots').exists())
        with mock.patch.object(record, 'check_ffmpeg', return_value=('recorder', '-timeout')), \
                mock.patch.object(record, '_snapshot_choose_ffmpeg', return_value='decoder'), \
                mock.patch.object(record._snap_os, 'fstatvfs', return_value=SimpleNamespace(f_bavail=1, f_frsize=1)), \
                mock.patch.object(self.manager, '_capture_bytes') as capture, self.assertRaises(record.CameraSnapshotError):
            self.manager.capture('camera_12')
        capture.assert_not_called()

    def test_incomplete_png_is_never_published(self):
        with self.assertRaises(record.CameraSnapshotError):
            self.capture(data=png()[:-4])
        self.assertEqual(list((self.root / 'snapshots').rglob('*.png')), [])

    def test_existing_file_is_not_overwritten_and_temporary_files_are_cleaned(self):
        directory = self.root / 'files'; directory.mkdir()
        path = directory / 'already.png'; path.write_bytes(b'keep original')
        with record._snapshot_dir(str(directory)) as fd, self.assertRaises(FileExistsError):
            record._snapshot_write(fd, 'already.png', png())
        self.assertEqual(path.read_bytes(), b'keep original')
        self.assertEqual(list(directory.glob('*.partial')), [])

    def test_symlink_root_and_image_are_not_followed(self):
        other = self.root / 'other'; other.mkdir()
        (self.root / 'snapshots').symlink_to(other, target_is_directory=True)
        with self.assertRaises(record.CameraSnapshotError): self.capture()
        self.assertEqual(list(other.iterdir()), [])
        (self.root / 'snapshots').unlink()
        metadata, _ = self.capture()
        image = next((self.root / 'snapshots').rglob('*.png'))
        victim = other / 'private.png'; victim.write_bytes(png())
        image.unlink(); image.symlink_to(victim)
        with self.assertRaises(record.CameraSnapshotError): self.manager.media(metadata['id'])
        self.assertEqual(victim.read_bytes(), png())
        self.assertEqual(self.manager.list(), [])

    def test_tampered_snapshot_bytes_are_not_served(self):
        metadata, _ = self.capture()
        next((self.root / 'snapshots').rglob('*.png')).write_bytes(png()[:-1] + b'x')
        with self.assertRaises(record.CameraSnapshotError): self.manager.media(metadata['id'])

    def test_metadata_failure_keeps_complete_png_for_manual_recovery(self):
        write = record._snapshot_write
        def fail_index(fd, name, data):
            if name.endswith('.json'): raise OSError('PRIVATE_ERROR')
            return write(fd, name, data)
        with mock.patch.object(record, '_snapshot_write', side_effect=fail_index), \
                self.assertRaisesRegex(record.CameraSnapshotError, '사진은 저장') as error:
            self.capture()
        images = list((self.root / 'snapshots').rglob('*.png'))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].read_bytes(), png())
        self.assertEqual(self.manager.list(), [])
        self.assertNotIn('PRIVATE_ERROR', str(error.exception))

    def test_metadata_failure_never_deletes_a_replacement_at_same_path(self):
        write = record._snapshot_write
        def replace_then_fail(fd, name, data):
            if name.endswith('.json'):
                image = next((self.root / 'snapshots').rglob('*.png'))
                replacement = image.with_suffix('.replacement'); replacement.write_bytes(b'concurrent user file')
                replacement.replace(image)
                raise OSError('simulated index failure')
            return write(fd, name, data)
        with mock.patch.object(record, '_snapshot_write', side_effect=replace_then_fail), \
                self.assertRaises(record.CameraSnapshotError):
            self.capture()
        self.assertEqual(next((self.root / 'snapshots').rglob('*.png')).read_bytes(), b'concurrent user file')


if __name__ == '__main__':
    unittest.main()
