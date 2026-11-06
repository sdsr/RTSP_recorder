"""In-memory live-view tests; cameras and external networks are never contacted."""
from dataclasses import replace
import base64
import importlib.util
import io
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name('record.py')
SPEC = importlib.util.spec_from_file_location('live_view_under_test', SOURCE)
record = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = record
SPEC.loader.exec_module(record)


def box(kind, content=b'', extended=False):
    return (struct.pack('>I4sQ', 1, kind, len(content) + 16) if extended
            else struct.pack('>I4s', len(content) + 8, kind)) + content


def video_moov(width=3840, height=2160, kind=b'avc1', avcc=b'\x01\x64\x00\x33\xff\xe1\x00'):
    sample = bytearray(78)
    struct.pack_into('>HH', sample, 24, width, height)
    entry = box(kind, bytes(sample) + box(b'avcC', avcc))
    stsd = box(b'stsd', b'\x00' * 4 + struct.pack('>I', 1) + entry)
    return box(b'trak', box(b'mdia', box(b'minf', box(b'stbl', stsd))))


def initialization():
    return box(b'ftyp', b'isom\x00\x00\x00\x00isomiso6') + box(b'moov', video_moov())


def camera(url='rtsp://TEST_USER:TEST_SECRET@192.0.2.12:554/profile2/media.smp'):
    return record.Camera('camera_12', url)


class NoThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def join(self, timeout=None):
        pass


class ShortPipe(io.BytesIO):
    def read(self, length=-1):
        return super().read(min(length, 3) if length >= 0 else 3)


class FakeProcess:
    def __init__(self, data=b'', errors=b'', fragmented=False):
        self.stdout = ShortPipe(data) if fragmented else io.BytesIO(data)
        self.stderr = io.BytesIO(errors)
        self.returncode = None
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = 0

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def dormant_session(data=b'', errors=b'', fragmented=False):
    process = FakeProcess(data, errors, fragmented)
    with mock.patch.object(record.subprocess, 'Popen', return_value=process), \
            mock.patch.object(record.threading, 'Thread', NoThread):
        session = record._LiveViewSession(['inert-test-process'], {'name': 'camera_12', 'ip': '192.0.2.12'})
    return session, process


class LiveViewMetadataTests(unittest.TestCase):
    def test_profile_override_preserves_credentials_and_never_mutates_camera(self):
        original = camera('rtsp://TEST_USER:TEST%40SECRET@192.0.2.12:8554/profile2/media.smp')
        self.assertEqual(record.live_view_url(original, '4'),
                         'rtsp://TEST_USER:TEST%40SECRET@192.0.2.12:8554/profile4/media.smp')
        self.assertEqual(record.live_view_url(original, 'current'), original.url)
        self.assertEqual(original.url, 'rtsp://TEST_USER:TEST%40SECRET@192.0.2.12:8554/profile2/media.smp')
        info = record.live_view_camera_info(original)
        self.assertEqual(info, {'name': 'camera_12', 'ip': '192.0.2.12', 'profile': '2', 'can_select_profile': True})
        self.assertNotIn('TEST', repr(info))

    def test_custom_query_and_non_wisenet_paths_cannot_be_rewritten(self):
        for url in ('rtsp://u:p@192.0.2.1/custom',
                    'rtsp://u:p@192.0.2.1/profile2/media.smp?token=TEST_TOKEN',
                    'rtsp://u:p@192.0.2.1/profile2/media.smp#fragment'):
            with self.subTest(path=url.split('@')[-1]):
                original = camera(url)
                self.assertEqual(record.live_view_url(original, 'current'), url)
                with self.assertRaises(record.LiveViewError):
                    record.live_view_url(original, '4')

    def test_invalid_profiles_urls_and_ports_fail_without_exposing_credentials(self):
        for profile in ('1', '3', '0', 4, None):
            with self.subTest(profile=profile), self.assertRaises(record.LiveViewError):
                record.live_view_url(camera(), profile)
        for url in ('https://TEST_USER:TEST_SECRET@192.0.2.1/live',
                    'rtsp://TEST_USER:TEST_SECRET@192.0.2.1:99999/live',
                    'rtsp://TEST_USER:TEST_SECRET@192.0.2.1/live space'):
            with self.subTest(kind=url.split(':')[0]), self.assertRaises(record.LiveViewError) as raised:
                record.live_view_url(camera(url), 'current')
            self.assertNotIn('TEST_SECRET', str(raised.exception))

    def test_codec_dimensions_come_from_h264_sample_entry_without_inventing_fps(self):
        info = record.live_view_video_info(video_moov())
        self.assertEqual(info, {'mime': 'video/mp4; codecs="avc1.640033"', 'width': 3840, 'height': 2160})
        self.assertNotIn('fps', info)
        low = record.live_view_video_info(video_moov(1280, 720, b'avc3', b'\x01\x42\xe0\x1f\xff\xe1\x00'))
        self.assertEqual(low['mime'], 'video/mp4; codecs="avc3.42e01f"')
        self.assertEqual((low['width'], low['height']), (1280, 720))

    def test_hevc_and_ambiguous_video_tracks_fail_explicitly(self):
        for kind in (b'hvc1', b'hev1'):
            with self.subTest(kind=kind), self.assertRaisesRegex(record.LiveViewError, 'H.264'):
                record.live_view_video_info(video_moov(kind=kind))
        with self.assertRaises(record.LiveViewError):
            record.live_view_video_info(video_moov() + video_moov(1280, 720))

    def test_malformed_bmff_boxes_and_sample_entries_are_rejected(self):
        for data in (b'123', struct.pack('>I4s', 0, b'moov'), struct.pack('>I4s', 7, b'moov'),
                     struct.pack('>I4s', 99, b'moov'), struct.pack('>I4s', 1, b'moov')):
            with self.subTest(length=len(data)), self.assertRaises(record.LiveViewError):
                list(record.live_view_boxes(data))
        self.assertEqual(list(record.live_view_boxes(box(b'free', b'ab', extended=True))), [(b'free', b'ab')])
        for moov in (video_moov(width=0), video_moov(height=0), video_moov(avcc=b'bad'),
                     box(b'trak', box(b'stsd', b'bad'))):
            with self.assertRaises(record.LiveViewError):
                record.live_view_video_info(moov)


class LiveViewSessionTests(unittest.TestCase):
    def test_fragmented_reads_deliver_exact_init_and_media_then_finalize_child(self):
        init = initialization()
        media = box(b'moof', b'fragment') + box(b'mdat', b'video bytes')
        session, process = dormant_session(init + media, fragmented=True)
        session._reader()
        ready = session.ready()
        self.assertEqual((ready['width'], ready['height']), (3840, 2160))
        self.assertEqual(b''.join(session.stream()), init + media)
        self.assertTrue(session._closed.is_set())
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        self.assertEqual(process.terminated, 1)

    def test_init_cap_and_wrong_box_order_stop_before_unbounded_reads(self):
        for data in (struct.pack('>I4s', record._LiveViewSession.MAX_INIT_BYTES + 1, b'moov'),
                     box(b'mdat', b'out of order')):
            with self.subTest(data_size=len(data)):
                session, process = dormant_session(data)
                session._reader()
                with self.assertRaises(record.LiveViewError):
                    session.ready()
                self.assertEqual(process.terminated, 1)

    def test_auth_error_is_classified_but_raw_stderr_and_url_never_escape(self):
        session, _ = dormant_session(errors=b'401 Unauthorized rtsp://TEST_USER:TEST_SECRET@192.0.2.1/live?token=TEST_TOKEN')
        session._stderr()
        session._reader()
        with self.assertRaises(record.LiveViewError) as raised:
            session.ready()
        self.assertIn('인증', str(raised.exception))
        for value in ('TEST_USER', 'TEST_SECRET', 'TEST_TOKEN', 'rtsp://'):
            self.assertNotIn(value, str(raised.exception))

    def test_slow_reader_hits_fixed_queue_cap_and_stops_receiving(self):
        amount = (record._LiveViewSession.QUEUE_CHUNKS + 2) * record._LiveViewSession.CHUNK_BYTES
        session, process = dormant_session(initialization() + b'x' * amount)
        session._reader()
        self.assertTrue(session._closed.is_set())
        self.assertIn('재생 속도', session.error)
        self.assertEqual(session._queue.qsize(), session.QUEUE_CHUNKS)
        self.assertEqual(process.terminated, 1)
        session.close()
        self.assertTrue(session._queue.empty())

    def test_one_consumer_only_and_generator_close_releases_resources(self):
        session, process = dormant_session(initialization() + b'fragment')
        session._reader()
        stream = session.stream()
        self.assertTrue(next(stream))
        with self.assertRaises(record.LiveViewError):
            session.stream()
        stream.close()
        self.assertTrue(session._closed.is_set())
        self.assertTrue(process.stdout.closed)

    def test_ready_timeout_terminates_child_without_retry(self):
        session, process = dormant_session()
        with mock.patch.object(session._ready, 'wait', return_value=False):
            with self.assertRaisesRegex(record.LiveViewError, '제한 시간'):
                session.ready()
        self.assertTrue(session._closed.is_set())
        self.assertEqual(process.terminated, 1)

    def test_attach_and_inactive_delivery_timeouts_close_the_child(self):
        for claimed, last_read, last_delivery in ((False, 100, 100), (True, 100, 0), (True, 0, 100)):
            with self.subTest(claimed=claimed, last_read=last_read):
                session, process = dormant_session()
                session._claimed = claimed
                session._created = 0
                session._last_read, session._last_delivery = last_read, last_delivery
                with mock.patch.object(session._closed, 'wait', side_effect=(False, True)), \
                        mock.patch.object(record.time, 'monotonic', return_value=100):
                    session._watch()
                self.assertTrue(session._closed.is_set())
                self.assertEqual(process.terminated, 1)

    def test_kill_race_after_terminate_timeout_is_harmless(self):
        session, process = dormant_session()
        process.terminate = mock.Mock()
        process.wait = mock.Mock(side_effect=(subprocess.TimeoutExpired('dummy', 1), 0))
        process.kill = mock.Mock(side_effect=ProcessLookupError('exited meanwhile'))
        session.close()
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_popen_failures_return_safe_errors_without_command_details(self):
        with mock.patch.object(record.subprocess, 'Popen', side_effect=OSError('TEST_SECRET')):
            with self.assertRaises(record.LiveViewError) as raised:
                record._LiveViewSession(['TEST_SECRET'], {})
        self.assertNotIn('TEST_SECRET', str(raised.exception))

    def test_real_inert_child_is_reaped_when_browser_generator_closes(self):
        encoded = base64.b64encode(initialization()).decode('ascii')
        code = 'import base64,sys,time;sys.stdout.buffer.write(base64.b64decode(%r));sys.stdout.buffer.flush();time.sleep(60)' % encoded
        session = record._LiveViewSession([sys.executable, '-u', '-c', code], {'name': 'fixture'})
        self.addCleanup(session.close)
        result = session.ready()
        self.assertEqual(result['width'], 3840)
        stream = session.stream()
        self.assertEqual(next(stream), initialization())
        stream.close()
        self.assertIsNotNone(session._process.poll())
        for worker in (session._reader_thread, session._stderr_thread, session._watch_thread):
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())


class LiveViewManagerTests(unittest.TestCase):
    def setUp(self):
        self.camera = camera()
        self.settings = SimpleNamespace(cameras=(self.camera,), output_dir=Path('unused'))
        self.manager = record.LiveViewManager(lambda: self.settings)

    def fake_session(self, identifier):
        session = mock.Mock()
        session.identifier = identifier
        session.ready.return_value = {'session_id': identifier, 'width': 1280, 'height': 720}
        return session

    def test_command_is_video_only_stream_copy_to_pipe_with_rtsp_tcp(self):
        session = self.fake_session('first')
        with mock.patch.object(record, 'check_ffmpeg', return_value=('ffmpeg-fixture', '-timeout')), \
                mock.patch.object(record, '_LiveViewSession', return_value=session) as launch:
            self.manager.start('camera_12', '4')
        args, info = launch.call_args[0]
        self.assertEqual(args[args.index('-rtsp_transport') + 1], 'tcp')
        self.assertEqual(args[args.index('-c:v') + 1], 'copy')
        self.assertEqual(args[args.index('-i') + 1], self.camera.url.replace('/profile2/', '/profile4/'))
        self.assertEqual(args[-1], 'pipe:1')
        for flag in ('-an', '-sn', '-dn', '-nostdin'):
            self.assertIn(flag, args)
        self.assertNotIn('TEST_SECRET', repr(info))
        self.assertEqual(self.camera.url, camera().url)
        self.manager.close()

    def test_replacing_session_closes_old_and_stale_stop_cannot_stop_new(self):
        first, second = self.fake_session('first'), self.fake_session('second')
        with mock.patch.object(record, 'check_ffmpeg', return_value=('ffmpeg', '-timeout')), \
                mock.patch.object(record, '_LiveViewSession', side_effect=(first, second)):
            self.manager.start('camera_12')
            self.manager.start('camera_12')
        first.close.assert_called_once()
        self.assertEqual(self.manager.stop('first'), {'stopped': False})
        second.close.assert_not_called()
        self.assertEqual(self.manager.stop('second'), {'stopped': True})
        second.close.assert_called_once()

    def test_unknown_camera_closed_manager_and_ffmpeg_failure_do_not_launch(self):
        with mock.patch.object(record, '_LiveViewSession') as launch:
            with self.assertRaises(record.LiveViewError):
                self.manager.start('other')
            with mock.patch.object(record, 'check_ffmpeg', side_effect=ValueError('TEST_SECRET')):
                with self.assertRaises(record.LiveViewError) as raised:
                    self.manager.start('camera_12')
            self.assertNotIn('TEST_SECRET', str(raised.exception))
            self.manager.close()
            with mock.patch.object(record, 'check_ffmpeg', return_value=('ffmpeg', '-timeout')):
                with self.assertRaises(record.LiveViewError):
                    self.manager.start('camera_12')
            launch.assert_not_called()

    def test_close_during_start_readiness_cancels_the_new_session(self):
        session = self.fake_session('first')
        def ready():
            self.manager.close()
            return {'session_id': session.identifier}
        session.ready.side_effect = ready
        with mock.patch.object(record, 'check_ffmpeg', return_value=('ffmpeg', '-timeout')), \
                mock.patch.object(record, '_LiveViewSession', return_value=session):
            with self.assertRaises(record.LiveViewError):
                self.manager.start('camera_12')
        session.close.assert_called_once()
        self.assertIsNone(self.manager._session)

    def test_failed_ready_session_is_closed_and_not_left_attached(self):
        session = self.fake_session('first')
        session.ready.side_effect = record.LiveViewError('No media')
        with mock.patch.object(record, 'check_ffmpeg', return_value=('ffmpeg', '-timeout')), \
                mock.patch.object(record, '_LiveViewSession', return_value=session):
            with self.assertRaises(record.LiveViewError):
                self.manager.start('camera_12')
        session.close.assert_called_once()
        self.assertIsNone(self.manager._session)
        with self.assertRaises(record.LiveViewError):
            self.manager.stream('first')


if __name__ == '__main__':
    unittest.main()
