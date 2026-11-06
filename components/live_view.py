# Embedded before ui_server.py by build_embedded.py. Standard library only.
# This is an in-memory, video-only RTSP remuxer; it never alters recorder settings.
import queue
import secrets
import struct
import subprocess
import threading
import time
import re
from urllib.parse import urlsplit


class LiveViewError(ValueError):
    """Safe to display: never includes a camera URL or subprocess stderr."""


def live_view_camera_info(camera):
    parsed = urlsplit(camera.url)
    match = re.fullmatch(r"/profile([1-9][0-9]*)/media\.smp", parsed.path)
    return {"name": camera.name, "ip": parsed.hostname,
            "profile": match.group(1) if match else "current",
            "can_select_profile": bool(match and not parsed.query and not parsed.fragment)}


def live_view_url(camera, profile):
    if profile not in ("current", "2", "4"):
        raise LiveViewError("재생 프로파일은 현재 설정, 2번 또는 4번을 선택하세요.")
    parsed = urlsplit(camera.url)
    try:
        valid = (parsed.scheme.lower() == "rtsp" and parsed.hostname
                 and (parsed.port is None or 1 <= parsed.port <= 65535)
                 and not any(char.isspace() or ord(char) < 32 for char in camera.url))
    except ValueError:
        valid = False
    if not valid:
        raise LiveViewError("저장된 카메라의 RTSP 주소를 확인하세요.")
    if profile == "current":
        return camera.url
    if not live_view_camera_info(camera)["can_select_profile"]:
        raise LiveViewError("이 카메라는 현재 저장된 경로로만 재생할 수 있습니다.")
    return parsed._replace(path="/profile{}/media.smp".format(profile)).geturl()


def live_view_boxes(data, offset=0):
    """Strict ISO BMFF box iterator, used only on a bounded initialization box."""
    while offset < len(data):
        if len(data) - offset < 8:
            raise LiveViewError("카메라 영상 초기화 정보를 읽을 수 없습니다.")
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise LiveViewError("카메라 영상 초기화 정보를 읽을 수 없습니다.")
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        if size < header or offset + size > len(data):
            raise LiveViewError("카메라 영상 초기화 정보를 읽을 수 없습니다.")
        yield kind, data[offset + header:offset + size]
        offset += size


def live_view_video_info(moov):
    """Read actual codec and dimensions from the MP4 video sample entry.

    The fragmented initialization section has no reliable source frame rate.
    Leave FPS to playback measurement instead of inferring it from a profile ID.
    """
    def sample_entries(data):
        for kind, content in live_view_boxes(data):
            if kind in (b"trak", b"mdia", b"minf", b"stbl"):
                yield from sample_entries(content)
            elif kind == b"stsd":
                if len(content) < 8:
                    raise LiveViewError("카메라 영상 초기화 정보를 읽을 수 없습니다.")
                yield from live_view_boxes(content, 8)

    videos = []
    for kind, content in sample_entries(moov):
        if kind in (b"hvc1", b"hev1"):
            raise LiveViewError("이 브라우저 미리보기는 H.264만 지원합니다. H.264 프로파일을 선택하세요. 녹화 설정은 유지됩니다.")
        if kind not in (b"avc1", b"avc3"):
            continue
        if len(content) < 78:
            raise LiveViewError("카메라 영상 초기화 정보를 읽을 수 없습니다.")
        width, height = struct.unpack_from(">HH", content, 24)
        if not width or not height:
            raise LiveViewError("카메라 영상 해상도 정보를 읽을 수 없습니다.")
        for child, config in live_view_boxes(content, 78):
            if child == b"avcC" and len(config) >= 7 and config[0] == 1:
                codec = "{}.{}".format(kind.decode("ascii"), config[1:4].hex())
                videos.append({"mime": 'video/mp4; codecs="{}"'.format(codec),
                               "width": width, "height": height})
    if len(videos) != 1:
        raise LiveViewError("H.264 영상 정보를 받지 못했습니다. 카메라 프로파일을 확인하세요.")
    return videos[0]


def live_view_mime(moov):
    """Compatibility helper for callers that only need the MSE MIME type."""
    return live_view_video_info(moov)["mime"]


class _LiveViewSession:
    CHUNK_BYTES = 64 * 1024
    QUEUE_CHUNKS = 64  # At most 4 MiB of media plus a 2 MiB initialization section.
    MAX_INIT_BYTES = 2 * 1024 * 1024
    START_TIMEOUT = 15.0
    ATTACH_TIMEOUT = 15.0
    IDLE_TIMEOUT = 20.0

    def __init__(self, command, info):
        self.identifier = secrets.token_hex(24)
        self.info = info
        self.mime = None
        self.video_info = None
        self.error = None
        self._auth_error = False
        self._queue = queue.Queue(maxsize=self.QUEUE_CHUNKS)
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._finished = threading.Event()
        self._terminate_lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._claimed = False
        self._created = self._last_read = self._last_delivery = time.monotonic()
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             bufsize=0, close_fds=True)
        except (OSError, ValueError):
            raise LiveViewError("미리보기용 FFmpeg를 실행할 수 없습니다.") from None
        self._stderr_thread = threading.Thread(target=self._stderr, daemon=True, name="live-view-errors")
        self._reader_thread = threading.Thread(target=self._reader, daemon=True, name="live-view-media")
        self._watch_thread = threading.Thread(target=self._watch, daemon=True, name="live-view-timeout")
        self._stderr_thread.start()
        self._reader_thread.start()
        self._watch_thread.start()

    def _stderr(self):
        # Retain no raw stderr. A small rolling window is used only to classify
        # authentication failures; URLs/passwords never escape this function.
        tail = b""
        try:
            while not self._closed.is_set():
                block = self._process.stderr.read(1024)
                if not block:
                    break
                tail = (tail + block)[-2048:]
                if re.search(rb"401|403|unauthorized|authentication|forbidden|login attempts", tail, re.I):
                    self._auth_error = True
        except (OSError, ValueError):
            pass

    def _exact(self, length):
        data = bytearray()
        while len(data) < length and not self._closed.is_set():
            block = self._process.stdout.read(length - len(data))
            if not block:
                raise EOFError()
            self._last_read = time.monotonic()
            data.extend(block)
        if len(data) != length:
            raise EOFError()
        return bytes(data)

    def _put(self, chunk):
        if self._closed.is_set():
            return False
        try:
            self._queue.put(chunk, timeout=0.5)
            return True
        except queue.Full:
            self.error = "재생 속도가 영상 수신을 따라가지 못해 미리보기를 중지했습니다. HD 프로파일로 다시 시도하세요."
            self._closed.set()
            self._terminate()
            return False

    def _reader(self):
        try:
            init = bytearray()
            while not self._closed.is_set():
                header = self._exact(8)
                size, kind = struct.unpack(">I4s", header)
                if size == 1:
                    extended = self._exact(8)
                    size = struct.unpack(">Q", extended)[0]
                    header += extended
                if size < len(header) or size + len(init) > self.MAX_INIT_BYTES:
                    raise LiveViewError("카메라 영상 초기화 정보가 너무 크거나 올바르지 않습니다.")
                if kind not in (b"ftyp", b"free", b"skip", b"moov"):
                    raise LiveViewError("카메라 영상 초기화 순서가 올바르지 않습니다.")
                content = self._exact(size - len(header))
                init.extend(header)
                init.extend(content)
                if kind == b"moov":
                    self.video_info = live_view_video_info(content)
                    self.mime = self.video_info["mime"]
                    if not self._put(bytes(init)):
                        return
                    self._ready.set()
                    break
            while not self._closed.is_set():
                block = self._process.stdout.read(self.CHUNK_BYTES)
                if not block:
                    break
                self._last_read = time.monotonic()
                if not self._put(block):
                    break
        except LiveViewError as exc:
            self.error = str(exc)
        except (EOFError, OSError, ValueError):
            if not self._closed.is_set():
                self.error = "카메라 영상 수신이 종료되었습니다. 연결 상태와 프로파일을 확인하세요."
        finally:
            self._finished.set()
            self._stderr_thread.join(timeout=0.2)
            if self._auth_error and not self.mime:
                self.error = "카메라 인증에 실패했습니다. 저장된 계정·비밀번호 또는 카메라의 로그인 잠금을 확인하세요. 자동 재시도하지 않습니다."
            self._ready.set()
            self._terminate()

    def _terminate(self):
        with self._terminate_lock:
            process = self._process
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # The child can exit between the timeout and kill().
                    # Cleanup must still close the stream without surfacing a
                    # transient process-lookup error to the browser.
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                except OSError:
                    pass
            else:
                # Reap a child that has already exited.
                try:
                    process.wait(timeout=0)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def _watch(self):
        while not self._closed.wait(0.5):
            now = time.monotonic()
            if not self._claimed and now - self._created > self.START_TIMEOUT + self.ATTACH_TIMEOUT:
                self.close()
            elif self._claimed and (now - self._last_delivery > self.IDLE_TIMEOUT
                                    or now - self._last_read > self.IDLE_TIMEOUT):
                self.close()

    def ready(self):
        if not self._ready.wait(self.START_TIMEOUT):
            self.close()
            raise LiveViewError("카메라가 제한 시간 안에 영상을 보내지 않았습니다. IP·RTSP 포트·프로파일을 확인하세요.")
        if not self.mime or self._closed.is_set():
            self.close()
            raise LiveViewError(self.error or "카메라 영상을 시작할 수 없습니다. 연결 상태를 확인하세요.")
        return dict(self.info, session_id=self.identifier, **self.video_info)

    def stream(self):
        with self._claim_lock:
            if self._claimed or self._closed.is_set():
                raise LiveViewError("이미 사용 중이거나 종료된 미리보기입니다. 재생을 다시 시작하세요.")
            self._claimed = True
            self._last_delivery = time.monotonic()
        return self._iterate()

    def _iterate(self):
        try:
            while not self._closed.is_set():
                try:
                    chunk = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if self._finished.is_set():
                        break
                    continue
                self._last_delivery = time.monotonic()
                yield chunk
        finally:
            self.close()

    def close(self):
        self._closed.set()
        self._terminate()
        for pipe in (self._process.stdout, self._process.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


class LiveViewManager:
    """One browser preview, independent of any scheduled recorder process."""
    def __init__(self, settings_callback):
        self._settings = settings_callback
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._session = None
        self._closed = False

    def cameras(self):
        return [live_view_camera_info(camera) for camera in self._settings().cameras]

    def start(self, camera_name, profile="current"):
        if not isinstance(camera_name, str):
            raise LiveViewError("저장된 카메라를 선택하세요.")
        with self._start_lock:
            settings = self._settings()
            matches = [camera for camera in settings.cameras if camera.name == camera_name]
            if len(matches) != 1:
                raise LiveViewError("저장된 카메라를 선택하세요.")
            camera = matches[0]
            url = live_view_url(camera, profile)
            try:
                executable, timeout_option = check_ffmpeg(settings)
            except (ValueError, OSError, subprocess.SubprocessError):
                raise LiveViewError("FFmpeg 실행 파일과 RTSP 지원 설정을 확인하세요.") from None
            command = [executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                       "-rtsp_transport", "tcp", timeout_option, "10000000", "-i", url,
                       "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy",
                       "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                       "-avoid_negative_ts", "make_zero", "-flush_packets", "1", "pipe:1"]
            info = live_view_camera_info(camera)
            if profile != "current":
                info["profile"] = profile
            with self._lock:
                if self._closed:
                    raise LiveViewError("미리보기 서비스가 종료되었습니다.")
                self.stop()
                session = _LiveViewSession(command, info)
                self._session = session
            try:
                result = session.ready()
                with self._lock:
                    if self._session is not session:
                        raise LiveViewError("미리보기가 중지되었습니다.")
                return result
            except Exception:
                self.stop(session.identifier)
                raise

    def stream(self, session_id):
        with self._lock:
            if not self._session or self._session.identifier != session_id:
                raise LiveViewError("미리보기 세션이 없습니다. 재생을 다시 시작하세요.")
            return self._session.stream()

    def stop(self, session_id=None):
        with self._lock:
            session = self._session
            if not session or (session_id is not None and session.identifier != session_id):
                return {"stopped": False}
            self._session = None
        session.close()
        return {"stopped": True}

    def close(self):
        with self._lock:
            self._closed = True
        self.stop()
