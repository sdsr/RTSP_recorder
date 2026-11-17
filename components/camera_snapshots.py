# Embedded after live_view.py. No network access occurs until capture() is called.
import contextlib as _snap_contextlib
from datetime import datetime as _snap_datetime, timedelta as _snap_timedelta, timezone as _snap_timezone
import json as _snap_json
import os as _snap_os
from pathlib import Path as _snap_Path
import re as _snap_re
import secrets as _snap_secrets
import shutil as _snap_shutil
import stat as _snap_stat
import struct as _snap_struct
import subprocess as _snap_subprocess
import sys as _snap_sys
import threading as _snap_threading
import time as _snap_time
import zlib as _snap_zlib

_SNAP_KST = _snap_timezone(_snap_timedelta(hours=9), 'Asia/Seoul')
_SNAP_MAX_BYTES = 32 * 1024 * 1024
_SNAP_FFMPEG_CACHE = {}
_SNAP_FFMPEG_LOCK = _snap_threading.Lock()


class CameraSnapshotError(ValueError):
    """User-facing error: contains no RTSP URL, password or FFmpeg output."""


def _snapshot_require_linux():
    if _snap_sys.platform != 'linux':
        raise CameraSnapshotError('사진 캡처와 캡처 파일 조회는 Linux ED에서 실행하세요.')


def _snapshot_choose_ffmpeg(settings):
    """Use a decoder-capable executable without changing recording settings.

    Some recorder bundles deliberately contain only RTSP remuxing components.
    A system FFmpeg can decode their saved camera stream independently. Check
    local capabilities only; never connect to a camera during this selection.
    """
    candidates = [_snap_shutil.which(settings.ffmpeg),
                  _snap_shutil.which('ffmpeg', path='/usr/bin:/bin'),
                  _snap_shutil.which('ffmpeg')]
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            executable = str(_snap_Path(candidate).resolve())
            info = _snap_os.stat(executable)
        except OSError:
            continue
        if executable in seen:
            continue
        seen.add(executable)
        key = (executable, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        with _SNAP_FFMPEG_LOCK:
            if key not in _SNAP_FFMPEG_CACHE:
                supported = None
                try:
                    capabilities = {}
                    for option in ('-decoders', '-encoders'):
                        result = _snap_subprocess.run([executable, '-hide_banner', option],
                            stdin=_snap_subprocess.DEVNULL, stdout=_snap_subprocess.PIPE,
                            stderr=_snap_subprocess.DEVNULL, timeout=5, text=True, errors='replace')
                        if result.returncode:
                            raise ValueError('unsupported capability command')
                        capabilities[option] = set(_snap_re.findall(r'^\s*[A-Z.]{6}\s+(\S+)\s',
                                                                   result.stdout, _snap_re.M))
                    if {'h264', 'hevc'} <= capabilities['-decoders'] and 'png' in capabilities['-encoders']:
                        supported = executable
                except (OSError, ValueError, _snap_subprocess.SubprocessError):
                    pass
                if len(_SNAP_FFMPEG_CACHE) >= 16:
                    _SNAP_FFMPEG_CACHE.clear()
                _SNAP_FFMPEG_CACHE[key] = supported
            selected = _SNAP_FFMPEG_CACHE[key]
        if selected:
            return selected
    raise CameraSnapshotError('이미지 캡처에는 H.264·H.265 디코더와 PNG 인코더가 있는 FFmpeg가 필요합니다. '
        '녹화 전용 FFmpeg는 유지하고 Ubuntu에 일반 FFmpeg를 설치하세요: sudo apt install ffmpeg')


def _snapshot_png_info(data):
    if not isinstance(data, bytes) or not 57 <= len(data) <= _SNAP_MAX_BYTES or data[:8] != b'\x89PNG\r\n\x1a\n':
        raise CameraSnapshotError('카메라에서 정상 PNG 이미지를 받지 못했습니다.')
    offset, dimensions, saw_data = 8, None, False
    while offset + 12 <= len(data):
        size, kind = _snap_struct.unpack_from('>I4s', data, offset)
        end = offset + 12 + size
        if end > len(data):
            break
        content = data[offset + 8:offset + 8 + size]
        crc = _snap_struct.unpack_from('>I', data, offset + 8 + size)[0]
        if _snap_zlib.crc32(kind + content) & 0xffffffff != crc:
            break
        if offset == 8:
            if kind != b'IHDR' or size != 13:
                break
            width, height = _snap_struct.unpack_from('>II', content)
            if not 0 < width <= 16384 or not 0 < height <= 16384 or width * height > 64_000_000:
                break
            dimensions = (width, height)
        elif kind == b'IHDR':
            break
        if kind == b'IDAT':
            saw_data = True
        if kind == b'IEND':
            if size == 0 and end == len(data) and dimensions and saw_data:
                return dimensions
            break
        offset = end
    raise CameraSnapshotError('캡처 이미지가 불완전합니다. 카메라 연결 상태를 확인하세요.')


@_snap_contextlib.contextmanager
def _snapshot_dir(name, parent_fd=None, create=False):
    _snapshot_require_linux()
    if create:
        try:
            _snap_os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = _snap_os.open(name, _snap_os.O_RDONLY | _snap_os.O_DIRECTORY | _snap_os.O_NOFOLLOW,
                       dir_fd=parent_fd)
    try:
        yield fd
    finally:
        _snap_os.close(fd)


def _snapshot_read(dir_fd, filename, maximum):
    fd = _snap_os.open(filename, _snap_os.O_RDONLY | _snap_os.O_NOFOLLOW | _snap_os.O_NONBLOCK,
                       dir_fd=dir_fd)
    with _snap_os.fdopen(fd, 'rb') as stream:
        info = _snap_os.fstat(stream.fileno())
        if not _snap_stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
            raise CameraSnapshotError('캡처 파일 형식 또는 크기가 올바르지 않습니다.')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise CameraSnapshotError('캡처 파일이 허용 크기를 초과했습니다.')
    return data


def _snapshot_write(dir_fd, name, data):
    """Publish a complete new file; never follow or overwrite an existing name."""
    temporary = '.snapshot-' + _snap_secrets.token_hex(16) + '.partial'
    fd = _snap_os.open(temporary, _snap_os.O_WRONLY | _snap_os.O_CREAT | _snap_os.O_EXCL |
                       _snap_os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        with _snap_os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            _snap_os.fsync(stream.fileno())
        _snap_os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                      follow_symlinks=False)
    finally:
        _snap_os.unlink(temporary, dir_fd=dir_fd)


class CameraSnapshotManager:
    """One bounded, explicit capture at a time, independent of live playback.

    capture() and list() return JSON-safe metadata. media(id) returns
    (metadata, PNG bytes); the HTTP layer supplies authenticated URLs. The saved
    recording profile is used by default. No camera settings are changed.
    """
    CAPTURE_TIMEOUT = 25.0
    MAX_LIST_SCAN = 10000

    def __init__(self, settings_provider, config_path):
        self._settings = settings_provider
        self._parent = _snap_Path(config_path).expanduser().resolve().parent
        self._capture_lock = _snap_threading.Lock()
        self._state_lock = _snap_threading.RLock()
        self._closed = False
        self._process = None
        self._index = {}

    @_snap_contextlib.contextmanager
    def _root(self, create=False):
        with _snapshot_dir(str(self._parent)) as parent:
            with _snapshot_dir('snapshots', parent, create) as root:
                yield root

    @staticmethod
    def _stop_process(process):
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except _snap_subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        except (OSError, _snap_subprocess.SubprocessError):
            pass

    def _capture_bytes(self, command, input_data=None, deadline=None):
        if input_data is not None and (not isinstance(input_data, bytes) or len(input_data) > _SNAP_MAX_BYTES):
            raise CameraSnapshotError('이미지 변환 입력이 허용 크기를 초과했습니다.')
        deadline = deadline if deadline is not None else _snap_time.monotonic() + self.CAPTURE_TIMEOUT
        if deadline <= _snap_time.monotonic():
            raise CameraSnapshotError('이미지 캡처 제한 시간을 초과했습니다.')
        result, done = {}, _snap_threading.Event()
        with self._state_lock:
            if self._closed:
                raise CameraSnapshotError('이미지 캡처 서비스가 종료되었습니다.')
            try:
                process = _snap_subprocess.Popen(command,
                    stdin=_snap_subprocess.PIPE if input_data is not None else _snap_subprocess.DEVNULL,
                    stdout=_snap_subprocess.PIPE, stderr=_snap_subprocess.DEVNULL,
                    close_fds=True, bufsize=0)
            except (OSError, ValueError):
                raise CameraSnapshotError('이미지 캡처용 FFmpeg를 실행할 수 없습니다.') from None
            self._process = process

        def read_image():
            image = bytearray()
            try:
                while len(image) <= _SNAP_MAX_BYTES:
                    chunk = process.stdout.read(min(64 * 1024, _SNAP_MAX_BYTES + 1 - len(image)))
                    if not chunk:
                        result['data'] = bytes(image)
                        break
                    image.extend(chunk)
                else:
                    result['oversized'] = True
            except (OSError, ValueError):
                result['read_failed'] = True
            finally:
                done.set()

        def write_input():
            try:
                view = memoryview(input_data)
                while view:
                    written = process.stdin.write(view[:64 * 1024])
                    if not written:
                        break
                    view = view[written:]
            except (OSError, ValueError):
                # FFmpeg may finish the selected frame before consuming the
                # container's trailing index. Its exit code determines success.
                pass
            finally:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass

        reader = _snap_threading.Thread(target=read_image, daemon=True, name='camera-snapshot-output')
        reader.start()
        writer = None
        if input_data is not None:
            writer = _snap_threading.Thread(target=write_input, daemon=True, name='camera-snapshot-input')
            writer.start()
        try:
            if not done.wait(max(0, deadline - _snap_time.monotonic())):
                raise CameraSnapshotError('제한 시간 안에 이미지를 받지 못했습니다. 연결 상태·프로파일·계정을 확인하세요.')
            if result.get('oversized'):
                raise CameraSnapshotError('이미지가 32 MiB를 초과해 저장하지 않았습니다.')
            try:
                status = process.wait(timeout=max(0.01, deadline - _snap_time.monotonic()))
            except _snap_subprocess.TimeoutExpired:
                raise CameraSnapshotError('이미지 캡처 제한 시간을 초과했습니다.') from None
            with self._state_lock:
                if self._closed:
                    raise CameraSnapshotError('이미지 캡처가 중지되었습니다.')
            if status != 0 or result.get('read_failed'):
                raise CameraSnapshotError('이미지 캡처에 실패했습니다. 카메라 연결·계정·프로파일을 확인하세요. 자동 재시도하지 않습니다.')
            data = result.get('data', b'')
            if not data:
                raise CameraSnapshotError('카메라 이미지 데이터를 받지 못했습니다.')
            return data
        finally:
            self._stop_process(process)
            reader.join(timeout=1)
            if writer is not None:
                writer.join(timeout=1)
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if process.stdout:
                process.stdout.close()
            with self._state_lock:
                if self._process is process:
                    self._process = None

    def _capture_png(self, command, input_data=None, deadline=None):
        data = self._capture_bytes(command, input_data, deadline)
        _snapshot_png_info(data)
        return data

    def capture(self, camera_name, profile='current'):
        _snapshot_require_linux()
        if not isinstance(camera_name, str) or not _snap_re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', camera_name):
            raise CameraSnapshotError('저장된 카메라를 선택하세요.')
        if not self._capture_lock.acquire(blocking=False):
            raise CameraSnapshotError('다른 이미지 캡처가 진행 중입니다. 완료 후 다시 시도하세요.')
        try:
            with self._state_lock:
                if self._closed:
                    raise CameraSnapshotError('이미지 캡처 서비스가 종료되었습니다.')
            settings = self._settings()
            cameras = [camera for camera in settings.cameras if camera.name == camera_name]
            if len(cameras) != 1:
                raise CameraSnapshotError('저장된 카메라를 선택하세요.')
            camera = cameras[0]
            try:
                url = live_view_url(camera, profile)
                info = live_view_camera_info(camera)
                executable, timeout_option = check_ffmpeg(settings)
            except (ValueError, OSError, _snap_subprocess.SubprocessError):
                raise CameraSnapshotError('저장된 RTSP 주소·프로파일 또는 녹화용 FFmpeg 설정을 확인하세요.') from None
            decoder = _snapshot_choose_ffmpeg(settings)
            with self._root(create=True) as root:
                space = _snap_os.fstatvfs(root)
                reserve = max(256 * 1024 * 1024, float(getattr(settings, 'min_free_gb', 5)) * 1024 ** 3)
                if space.f_bavail * space.f_frsize < reserve + _SNAP_MAX_BYTES:
                    raise CameraSnapshotError('저장 공간이 부족합니다. 녹화용 여유 공간을 확보한 후 다시 시도하세요.')
            # Only the configured recorder executable authenticates with the
            # camera. An older system decoder may not support its RTSP digest.
            network_command = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-rtsp_transport', 'tcp', timeout_option, '10000000', '-i', url,
                '-map', '0:v:0', '-an', '-sn', '-dn', '-frames:v', '1', '-c:v', 'copy',
                '-f', 'mp4', '-movflags', '+frag_keyframe+empty_moov+default_base_moof', 'pipe:1']
            decode_command = [decoder, '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-i', 'pipe:0', '-map', '0:v:0', '-an', '-sn', '-dn', '-frames:v', '1', '-c:v', 'png',
                '-threads', '1', '-f', 'image2pipe', 'pipe:1']
            deadline = _snap_time.monotonic() + self.CAPTURE_TIMEOUT
            frame = self._capture_bytes(network_command, deadline=deadline)
            data = self._capture_png(decode_command, input_data=frame, deadline=deadline)
            width, height = _snapshot_png_info(data)
            now = _snap_datetime.now(_SNAP_KST)
            identifier = _snap_secrets.token_hex(16)
            day = now.strftime('%Y-%m-%d')
            safe_ip = _snap_re.sub(r'[^A-Za-z0-9_.-]', '_', info['ip'] or 'unknown')[:100]
            filename = '{}_ip{}_{}_KST_{}.png'.format(camera_name, safe_ip,
                now.strftime('%Y%m%d_%H%M%S'), identifier)
            metadata = {'id': identifier, 'camera': camera_name, 'ip': info['ip'],
                'profile': info['profile'] if profile == 'current' else profile,
                'captured_at': now.isoformat(timespec='seconds'), 'filename': filename,
                'width': width, 'height': height, 'size': len(data), 'mime': 'image/png'}
            meta_name = identifier + '.json'
            with self._root(create=True) as root:
                with _snapshot_dir(camera_name, root, True) as cam:
                    with _snapshot_dir(day, cam, True) as target:
                        _snapshot_write(target, filename, data)
                        try:
                            _snapshot_write(target, meta_name,
                                _snap_json.dumps(metadata, ensure_ascii=False).encode('utf-8'))
                        except Exception:
                            # The complete PNG has been published. Preserve it
                            # (and any concurrent replacement) if its index fails.
                            # Removing by pathname here could delete another file.
                            raise CameraSnapshotError('사진은 저장했지만 캡처 목록을 기록하지 못했습니다. '
                                '설정 폴더 아래 snapshots의 PNG 파일과 저장 공간을 확인하세요.') from None
            with self._state_lock:
                self._index[identifier] = (camera_name, day)
            return metadata
        except (OSError, NotImplementedError):
            raise CameraSnapshotError('캡처 저장 폴더를 안전하게 열거나 쓸 수 없습니다. 권한·링크·저장 공간을 확인하세요.') from None
        finally:
            self._capture_lock.release()

    @staticmethod
    def _metadata(target, identifier, camera, day):
        metadata = _snap_json.loads(_snapshot_read(target, identifier + '.json', 16384))
        required = {'id', 'camera', 'ip', 'profile', 'captured_at', 'filename', 'width', 'height', 'size', 'mime'}
        if (not isinstance(metadata, dict) or set(metadata) != required or metadata['id'] != identifier
                or metadata['camera'] != camera or metadata['mime'] != 'image/png'
                or not isinstance(metadata['filename'], str)
                or not _snap_re.fullmatch(r'[A-Za-z0-9_.-]{1,240}', metadata['filename'])
                or not metadata['filename'].endswith('_' + identifier + '.png')
                or not isinstance(metadata['captured_at'], str) or not metadata['captured_at'].startswith(day + 'T')
                or not isinstance(metadata['ip'], str) or len(metadata['ip']) > 253
                or not isinstance(metadata['profile'], str) or len(metadata['profile']) > 32
                or any(type(metadata[key]) is not int or metadata[key] <= 0 for key in ('width', 'height', 'size'))
                or metadata['size'] > _SNAP_MAX_BYTES):
            raise CameraSnapshotError('캡처 목록 정보가 올바르지 않습니다.')
        return metadata

    def _scan(self):
        found = []
        count = 0
        try:
            with self._root() as root:
                for camera in sorted(_snap_os.listdir(root))[:256]:
                    if not _snap_re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', camera):
                        continue
                    try:
                        with _snapshot_dir(camera, root) as cam:
                            for day in sorted(_snap_os.listdir(cam), reverse=True)[:3660]:
                                if not _snap_re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                                    continue
                                try:
                                    with _snapshot_dir(day, cam) as target:
                                        for name in sorted(_snap_os.listdir(target), reverse=True):
                                            if not _snap_re.fullmatch(r'[0-9a-f]{32}\.json', name):
                                                continue
                                            count += 1
                                            if count > self.MAX_LIST_SCAN:
                                                return found
                                            try:
                                                meta = self._metadata(target, name[:-5], camera, day)
                                                info = _snap_os.stat(meta['filename'], dir_fd=target, follow_symlinks=False)
                                                if not _snap_stat.S_ISREG(info.st_mode) or info.st_size != meta['size']:
                                                    continue
                                                found.append(meta)
                                                with self._state_lock:
                                                    self._index[meta['id']] = (camera, day)
                                            except (OSError, ValueError, TypeError, KeyError):
                                                continue
                                except OSError:
                                    continue
                    except OSError:
                        continue
        except FileNotFoundError:
            pass
        except (OSError, NotImplementedError):
            raise CameraSnapshotError('캡처 저장 폴더에 접근할 수 없습니다.') from None
        return found

    def list(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise CameraSnapshotError('캡처 목록 개수는 1~1,000개로 지정하세요.')
        return sorted(self._scan(), key=lambda item: (item['captured_at'], item['id']), reverse=True)[:limit]

    def media(self, identifier):
        if not isinstance(identifier, str) or not _snap_re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise CameraSnapshotError('캡처 이미지를 찾을 수 없습니다.')
        with self._state_lock:
            location = self._index.get(identifier)
        if location is None:
            self._scan()
            with self._state_lock:
                location = self._index.get(identifier)
        if location is None:
            raise CameraSnapshotError('캡처 이미지를 찾을 수 없습니다.')
        camera, day = location
        try:
            with self._root() as root:
                with _snapshot_dir(camera, root) as cam:
                    with _snapshot_dir(day, cam) as target:
                        metadata = self._metadata(target, identifier, camera, day)
                        data = _snapshot_read(target, metadata['filename'], _SNAP_MAX_BYTES)
            if len(data) != metadata['size'] or _snapshot_png_info(data) != (metadata['width'], metadata['height']):
                raise CameraSnapshotError('저장된 캡처 이미지가 변경되었거나 불완전합니다.')
            return metadata, data
        except (OSError, ValueError, TypeError, KeyError, NotImplementedError):
            raise CameraSnapshotError('캡처 이미지를 안전하게 읽을 수 없습니다.') from None

    def close(self):
        with self._state_lock:
            self._closed = True
            process = self._process
        self._stop_process(process)
