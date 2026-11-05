# Embedded into record.py by build_embedded.py; localhost-only, standard-library UI.
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import webbrowser
import gzip
import errno
import stat
import ctypes
from contextlib import contextmanager
from ipaddress import IPv4Address
from urllib.parse import quote, unquote, parse_qs


STATUS_NAME = ".recorder-status.json"
RECORDING_TRASH = ".recorder-trash"


def recording_relative_parts(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("영상 파일 경로가 올바르지 않습니다.")
    parts = value.split("/")
    if (len(parts) > 32 or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
            or not parts[-1].lower().endswith(".mp4")):
        raise ValueError("저장 폴더 안의 MP4 영상만 사용할 수 있습니다.")
    return parts


def recording_identifier(relative):
    recording_relative_parts(relative)
    return base64.urlsafe_b64encode(relative.encode("utf-8")).decode("ascii").rstrip("=")


def recording_relative_from_id(identifier):
    try:
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,8192}", identifier):
            raise ValueError()
        raw = base64.b64decode(identifier + "=" * (-len(identifier) % 4), altchars=b"-_", validate=True)
        relative = raw.decode("utf-8")
        recording_relative_parts(relative)
        if recording_identifier(relative) != identifier:
            raise ValueError()
        return relative
    except (ValueError, UnicodeError):
        raise ValueError("영상 파일 식별자가 올바르지 않습니다.") from None


def recording_scoped_id(relative, root_info, file_info, trash=False):
    value = {"path": relative, "root": [root_info.st_dev, root_info.st_ino],
             "file": [file_info.st_dev, file_info.st_ino], "trash": bool(trash), "version": 1}
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")


def recording_scope_from_id(identifier, root_info, trash=False):
    try:
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,8192}", identifier):
            raise ValueError()
        raw = base64.b64decode(identifier + "=" * (-len(identifier) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, dict) or value.get("version") != 1 or value.get("trash") is not bool(trash)
                or value.get("root") != [root_info.st_dev, root_info.st_ino]
                or not isinstance(value.get("file"), list) or len(value["file"]) != 2
                or any(isinstance(number, bool) or not isinstance(number, int) for number in value["file"])):
            raise ValueError()
        relative = value["path"]
        if trash:
            if not isinstance(relative, str) or not re.fullmatch(r"[0-9a-f]{32}", relative):
                raise ValueError()
        else:
            recording_relative_parts(relative)
        return relative, tuple(value["file"])
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise ValueError("영상 목록이 변경되었거나 식별자가 올바르지 않습니다. 목록을 새로고침하세요.") from None


@contextmanager
def recording_directory(root_fd, parts=(), create=False):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            if part in ("", ".", "..") or "/" in part or "\\" in part:
                raise ValueError("영상 폴더 경로가 올바르지 않습니다.")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def recording_open_file(root_fd, relative):
    parts = recording_relative_parts(relative)
    with recording_directory(root_fd, parts[:-1]) as parent:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("일반 MP4 영상 파일만 사용할 수 있습니다.")
    return fd


def recording_walk(root_fd, prefix=()):
    """Walk directory descriptors, never following a symlink or a hidden folder."""
    if len(prefix) >= 32:
        return
    for name in sorted(os.listdir(root_fd)):
        if name.startswith(".") or "/" in name or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            continue
        try:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                with recording_directory(root_fd, (name,)) as child:
                    yield from recording_walk(child, prefix + (name,))
            elif stat.S_ISREG(info.st_mode) and name.lower().endswith(".mp4"):
                yield "/".join(prefix + (name,)), info
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                continue
            raise


def recording_read_json(root_fd, name, maximum=65536):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("영상 관리 정보 형식이 올바르지 않습니다.")
        with os.fdopen(os.dup(fd), "rb") as stream:
            return json.loads(stream.read(maximum + 1).decode("utf-8"))
    finally:
        os.close(fd)


def recording_writer_inodes(candidates, recorder_pids=()):
    """Find writable descriptors, reporting uncertainty rather than allowing a move."""
    wanted = {(info.st_dev, info.st_ino) for _, info in candidates}
    recorder_pids = set(recorder_pids)
    writers, uncertain = set(), False
    if not wanted:
        return writers, uncertain
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        return writers, True
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            # Sandboxed browsers and unrelated services may deny /proc reads,
            # even for our UID. Only an identified recorder makes this uncertain.
            uncertain = uncertain or int(process.name) in recorder_pids
            continue
        for descriptor in descriptors:
            inode = None
            try:
                info = descriptor.stat()
                inode = (info.st_dev, info.st_ino)
                if inode not in wanted:
                    continue
                detail = (process / "fdinfo" / descriptor.name).read_text(encoding="ascii")
                flags = re.search(r"^flags:\s*([0-7]+)$", detail, flags=re.M)
                if flags is None or int(flags.group(1), 8) & os.O_ACCMODE != os.O_RDONLY:
                    writers.add(inode)
            except FileNotFoundError:
                continue
            except OSError:
                if inode in wanted:
                    writers.add(inode)
                uncertain = uncertain or int(process.name) in recorder_pids
    return writers, uncertain


def recording_range(value, size):
    """One RFC byte range, including suffix requests; multipart is not supported."""
    if value is None:
        return 0, max(0, size - 1), 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()) or size == 0:
        raise ValueError("요청한 영상 범위를 읽을 수 없습니다.")
    start, end = match.groups()
    if not start:
        length = int(end)
        if length <= 0:
            raise ValueError("요청한 영상 범위를 읽을 수 없습니다.")
        return max(0, size - length), size - 1, 206
    start = int(start)
    end = min(int(end), size - 1) if end else size - 1
    if start >= size or end < start:
        raise ValueError("요청한 영상 범위를 읽을 수 없습니다.")
    return start, end, 206


def recording_rename_exclusive(source_fd, source_name, target_fd, target_name):
    """Linux atomic move; a competing file is never overwritten or unlinked."""
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise ValueError("이 환경에서는 안전한 영상 이동을 지원하지 않습니다. 파일을 그대로 보존했습니다.") from None
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source_name), target_fd, os.fsencode(target_name), 1) != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise FileExistsError(number, "Recording destination already exists")
        if number in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise ValueError("이 저장 장치에서는 안전한 영상 이동을 지원하지 않습니다. 파일을 그대로 보존했습니다.")
        raise OSError(number, "Could not move recording safely")


def camera_connection_defaults(mode="setup"):
    """The editable UI model never contains a stored camera password."""
    return {"mode": mode, "ips": "", "username": "admin", "password": "",
            "password_saved": False, "port": 554, "path": "/profile2/media.smp"}


def _direct_camera_details(item):
    """Parse only URLs that the shared-credential IPv4 form can represent."""
    if not isinstance(item, dict) or "section" in item or not isinstance(item.get("url"), str):
        return None
    try:
        url = item["url"]
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
            return None
        parsed = urlsplit(url)
        # Query strings can carry vendor-specific passwords/tokens. Keep these
        # URLs in the unexposed custom configuration instead of reflecting them.
        if parsed.scheme.lower() != "rtsp" or parsed.fragment or "?" in url:
            return None
        ip = str(IPv4Address(parsed.hostname))
        port = parsed.port if parsed.port is not None else 554
        if not 1 <= port <= 65535:
            return None
        path = parsed.path or "/"
        if not path.startswith("/") or path.startswith("//") or re.search(r"%(?![0-9a-fA-F]{2})", path):
            return None
        return {"ip": ip, "username": unquote(parsed.username or "", errors="strict"),
                "password": unquote(parsed.password or "", errors="strict"), "port": port, "path": path}
    except (ValueError, TypeError, UnicodeError):
        return None


def camera_connection_for_ui(data):
    cameras = data.get("cameras", "auto")
    if cameras == "auto":
        return camera_connection_defaults()
    result = camera_connection_defaults("custom")
    if not isinstance(cameras, list) or not cameras:
        return result
    values = [_direct_camera_details(item) for item in cameras]
    if any(value is None for value in values):
        return result
    first = values[0]
    shared = ("username", "password", "port", "path")
    if len({value["ip"] for value in values}) != len(values) or any(
            any(value[key] != first[key] for key in shared) for value in values):
        return result
    result.update(mode="direct", ips="\n".join(value["ip"] for value in values),
                  username=first["username"], password_saved=bool(first["password"]),
                  port=first["port"], path=first["path"])
    return result


def cameras_from_connection(connection, data):
    """Convert UI edits to the original camera schema without revealing credentials."""
    allowed = {"mode", "ips", "username", "password", "password_saved", "port", "path"}
    if not isinstance(connection, dict) or set(connection) - allowed:
        raise ValueError("카메라 연결 설정 형식이 올바르지 않습니다.")
    mode = connection.get("mode")
    if mode == "setup":
        return "auto"
    if mode == "custom":
        # Nonrepresentable legacy camera lists must survive unrelated edits exactly.
        return data.get("cameras", "auto")
    if mode != "direct":
        raise ValueError("카메라 연결 방식을 선택하세요.")
    text = connection.get("ips", "")
    if not isinstance(text, str) or len(text) > 16384:
        raise ValueError("카메라 IPv4 주소를 쉼표, 공백 또는 줄바꿈으로 구분해 입력하세요.")
    tokens = re.split(r"[\s,]+", text.strip())
    try:
        ips = list(dict.fromkeys(str(IPv4Address(token)) for token in tokens))
    except (ValueError, TypeError):
        raise ValueError("카메라 주소에는 IPv4 주소만 입력하세요. RTSP 주소나 주소 범위는 사용할 수 없습니다.") from None
    if not ips or len(ips) > 256:
        raise ValueError("카메라 IPv4 주소는 1개 이상, 256개 이하로 입력하세요.")
    username, password = connection.get("username", "admin"), connection.get("password", "")
    if any(not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value)
           for value in (username, password)):
        raise ValueError("카메라 계정과 비밀번호 형식을 확인하세요.")
    if password and not username:
        raise ValueError("비밀번호를 사용하는 카메라는 사용자 이름도 입력하세요.")
    port = connection.get("port", 554)
    if isinstance(port, str) and re.fullmatch(r"[0-9]{1,5}", port):
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("RTSP 포트는 1부터 65535 사이의 정수로 입력하세요.")
    path = connection.get("path", "/profile2/media.smp")
    if isinstance(path, str) and "?" in path:
        raise ValueError("쿼리가 포함된 RTSP 주소는 기존 개별 설정을 사용하세요. 직접 IP 입력에는 쿼리 없는 경로를 입력하세요.")
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or len(path) > 4096
            or "#" in path or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in path)
            or re.search(r"%(?![0-9a-fA-F]{2})", path)):
        raise ValueError("RTSP 경로는 /로 시작하고 공백이나 # 없이 입력하세요.")
    path = quote(path, safe="/%:@!$&'()*+,;=-._~[]")
    existing = data.get("cameras", [])
    pairs = [(item, _direct_camera_details(item)) for item in existing] if isinstance(existing, list) else []
    pairs = [(item, details) for item, details in pairs if details is not None]
    common_passwords = {details["password"] for _, details in pairs if details["username"] == username}
    # Reuse an existing name and labels for an unchanged physical target.
    matches = {ip: [(item, details) for item, details in pairs if details["ip"] == ip] for ip in ips}
    retained_names = {item.get("name", "").lower() for ip in ips for item, _ in matches[ip]
                      if isinstance(item.get("name"), str) and NAME.fullmatch(item["name"])}
    names, cameras = set(), []
    for ip in ips:
        candidates = matches[ip]
        old_item, old = candidates[0] if len(candidates) == 1 else ({}, None)
        secret = password
        if not secret and username:
            matching_passwords = {details["password"] for _, details in candidates if details["username"] == username}
            if len(matching_passwords) == 1:
                secret = next(iter(matching_passwords))
            elif not matching_passwords and len(common_passwords) == 1 and next(iter(common_passwords)):
                secret = next(iter(common_passwords))
            else:
                raise ValueError("새 카메라 또는 변경한 계정의 비밀번호를 입력하세요. 기존 비밀번호를 하나로 확인할 수 없습니다.")
        name = old_item.get("name", "")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name.lower() in names:
            name = "camera_" + ip.rsplit(".", 1)[1]
            if name.lower() in names or name.lower() in retained_names:
                name = "camera_" + ip.replace(".", "_")
            suffix, base = 2, name
            while name.lower() in names or name.lower() in retained_names:
                name = base + "_" + str(suffix)
                suffix += 1
        names.add(name.lower())
        item = dict(old_item)
        item["name"] = name
        unchanged = old and old["username"] == username and old["password"] == secret and old["port"] == port and old["path"] == path
        if not unchanged:
            credentials = (quote(username, safe="") + ":" + quote(secret, safe="") + "@") if username else ""
            item["url"] = "rtsp://{}{}:{}{}".format(credentials, ip, port, path)
        cameras.append(item)
    return cameras


def write_recorder_status(settings, recorder, weather_gate=None, finished=False):
    mono = time.monotonic()
    if not finished and mono - getattr(recorder, "last_status_write", -10) < 2:
        return
    recorder.last_status_write = mono
    now = datetime.now(KST)
    value = {"pid": os.getpid(), "updated_at": now.isoformat(), "running": not finished,
             "active_cameras": list(recorder.jobs) if not finished else [],
             "output_dir": str(settings.output_dir),
             "once_end": settings.once_window[1].isoformat() if settings.once_window else None,
             "weather_fingerprint": weather_gate.condition.fingerprint if weather_gate is not None else None,
             "weather": weather_gate.snapshot(now) if weather_gate is not None else {"enabled": False}}
    try:
        atomic_private_json(settings.output_dir / STATUS_NAME, value)
    except (OSError, ValueError):
        LOG.debug("Could not update the UI status file")


def safe_read_status(root):
    path = Path(root) / STATUS_NAME
    try:
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(value["updated_at"])
        if updated.tzinfo is None or not 0 <= (datetime.now(KST) - updated).total_seconds() < 12:
            return {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def friendly_ui_error(exc):
    text = str(exc)
    replacements = (
        ("FFmpeg not found", "FFmpeg가 설치되어 있지 않습니다. 설치 담당자에게 확인해 주세요."),
        ("Multiple setup.ini files found", "카메라 설정이 여러 개입니다. 아래에서 실제 운영 setup.ini를 선택하세요."),
        ("setup.ini not found", "카메라 설정을 찾지 못했습니다. setup.ini 경로를 입력하세요."),
        ("URL must be a valid rtsp", "선택한 카메라의 RTSP 주소를 확인하세요. 시험용 영상 파일은 사용할 수 없습니다."),
        ("No enabled cameras", "사용으로 설정된 카메라가 없습니다. setup.ini의 INUSE 값을 확인하세요."),
        ("output_dir=auto requires", "IP를 직접 입력할 때는 저장 폴더도 직접 지정하세요. 자동 저장 폴더는 setup.ini 방식에서 사용할 수 있습니다."),
        ("already using output_dir", "같은 저장 폴더를 사용하는 녹화가 이미 실행 중입니다."),
        ("not writable", "저장 폴더에 쓸 수 없습니다. 경로와 장비 계정의 권한을 확인하세요."),
        ("already passed today", "시작 시각이 이미 지났습니다. 이후 시각을 고르거나 지금 시작을 선택하세요."),
        ("Weather region", "기상청 지역 목록에서 시·도와 시·군·구를 선택하세요."),
        ("time-ranges", "시간 구간은 시작·종료 시각을 모두 지정하고, 두 시각을 다르게 설정하세요."),
        ("recording_dates", "녹화 날짜를 확인하세요. 날짜는 YYYY-MM-DD 형식으로 중복 없이 지정합니다."),
        ("segment_minutes", "파일 분할 길이를 1초 이상, 1,440분 이하로 입력하세요."),
        ("record-minutes", "한 번 녹화할 길이를 1초 이상, 1,440분 이하로 입력하세요."),
        ("retention_days", "보관 일수는 0 이상의 정수로 입력하세요. 0이면 자동 삭제하지 않습니다."),
        ("min_free_gb", "최소 여유 공간은 0 이상의 숫자로 입력하세요."),
    )
    for pattern, message in replacements:
        if pattern in text:
            return message
    if isinstance(exc, json.JSONDecodeError):
        return "설정 파일의 JSON 문법을 확인하세요. {}번째 줄 부근에 오류가 있습니다.".format(exc.lineno)
    if isinstance(exc, configparser.Error):
        return "카메라 INI 설정 형식을 확인하세요."
    if isinstance(exc, OSError):
        return "파일 또는 프로그램에 접근하지 못했습니다. 경로와 권한을 확인하세요."
    if "rtsp://" in text.lower() or "servicekey=" in text.lower() or "authkey=" in text.lower():
        return "설정을 확인하지 못했습니다. 카메라 주소와 기상청 인증 설정을 확인하세요."
    return text[:600]


class RecorderUiController:
    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser().resolve()
        self.action_lock = threading.RLock()
        self.log_lock = threading.Lock()
        self.logs = deque(maxlen=100)
        self.process = None
        self.mode = None
        self.reader = None
        self.preview = None
        self.preview_busy = False
        self.preview_thread = None
        self.closing = False
        self.cached_settings = None
        self.cached_revision = None

    def read_config(self):
        if self.config_path.is_symlink() or self.config_path.stat().st_size > 1024 * 1024:
            raise ValueError("설정 파일은 1MB 이하의 일반 JSON 파일이어야 합니다.")
        raw = self.config_path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("설정 파일은 JSON 객체여야 합니다.")
        return data, hashlib.sha256(raw).hexdigest()

    def settings(self, refresh=False):
        data, revision = self.read_config()
        if refresh or self.cached_settings is None or revision != self.cached_revision:
            self.cached_settings = settings_from_data(data, self.config_path.parent)
            self.cached_revision = revision
            self.preview = None
        return self.cached_settings

    def configuration(self):
        data, revision = self.read_config()
        safe = {key: data[key] for key in ("setup_ini", "output_dir", "intersection_name", "weather", "recording_dates",
                 "time_ranges", "segment_minutes", "retention_days", "min_free_gb", "weather_condition") if key in data}
        safe.setdefault("weather_condition", default_weather_config())
        if isinstance(safe["weather_condition"], dict):
            safe["weather_condition"] = {key: value for key, value in safe["weather_condition"].items()
                                         if key in ("enabled", "region", "conditions", "provider", "api_key_file", "poll_minutes", "met_contact")}
        safe.setdefault("setup_ini", "auto")
        safe.setdefault("output_dir", "auto")
        safe.setdefault("segment_minutes", data.get("segment_seconds", 1800) / 60)
        if "time_ranges" not in safe and "schedules" in data:
            # Display only representable minute-based schedules without silently
            # replacing an existing all-day or fractional-second legacy plan.
            values = []
            for schedule in data["schedules"]:
                duration = schedule.get("duration_hours", 0) * 60
                if duration != int(duration) or duration == 1440:
                    values = []
                    break
                hour, minute = map(int, schedule["start"].split(":"))
                end = (hour * 60 + minute + int(duration)) % 1440
                values.append(schedule["start"] + "-{:02d}:{:02d}".format(end // 60, end % 60))
            safe["time_ranges"] = values
            safe["legacy_schedule"] = not bool(values)
        cameras = data.get("cameras", "auto")
        safe["cameras"] = cameras if cameras == "auto" else [
            {"name": item.get("name") if isinstance(item.get("name"), str) and NAME.fullmatch(item["name"]) else "camera",
             "section": item.get("section") if isinstance(item.get("section"), str) and re.fullmatch(r"CAM[1-9][0-9]*", item["section"]) else None,
             "direct_url": "url" in item}
            for item in (cameras if isinstance(cameras, list) else []) if isinstance(item, dict)]
        safe["camera_connection"] = camera_connection_for_ui(data)
        return {"config": safe, "revision": revision, "path": str(self.config_path), "regions": region_data()}

    def setup_candidates(self):
        candidates = set()
        for parent in list(Path(__file__).resolve().parents)[:4]:
            candidate = parent / "config" / "setup.ini"
            if candidate.is_file():
                candidates.add(str(candidate.resolve()))
        candidates.update(str(path) for path in scan_setup_ini(DEFAULT_SETUP_SEARCH_ROOT))
        return sorted(candidates)

    @contextmanager
    def recording_root(self):
        settings = self.settings()
        root = Path(settings.output_dir)
        data, _ = self.read_config()
        configured = data.get("output_dir")
        if isinstance(configured, str) and configured != "auto":
            configured = Path(configured).expanduser()
            if not configured.is_absolute():
                configured = self.config_path.parent / configured
            for parent in (configured, *configured.parents):
                if parent.is_symlink():
                    raise ValueError("영상 관리에는 심볼릭 링크가 없는 저장 폴더를 사용하세요.")
        fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield fd
        finally:
            os.close(fd)

    def recording_activity(self, root_fd, candidates):
        uncertain = False
        status = {}
        try:
            value = recording_read_json(root_fd, STATUS_NAME)
            updated = datetime.fromisoformat(value["updated_at"])
            if updated.tzinfo is not None and 0 <= (datetime.now(KST) - updated).total_seconds() < 12:
                status = value
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        recorder_pids = set()
        for pid in (status.get("pid") if status.get("running") else None,
                    getattr(self.process, "pid", None) if self._running() else None):
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                recorder_pids.add(pid)
                try:
                    children = Path("/proc/{0}/task/{0}/children".format(pid)).read_text(encoding="ascii").split()
                    recorder_pids.update(int(child) for child in children[:256] if child.isdigit())
                except OSError:
                    pass
        writers, uncertain = recording_writer_inodes(candidates, recorder_pids)
        locked = False
        try:
            import fcntl
            lock_fd = os.open(".recorder.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    uncertain = True
                else:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except BlockingIOError:
                        locked = True
            finally:
                os.close(lock_fd)
        except FileNotFoundError:
            pass
        except (OSError, ImportError):
            uncertain = True
        active_cameras = status.get("active_cameras", []) if status.get("running") else []
        if not isinstance(active_cameras, list) or any(not isinstance(camera, str) for camera in active_cameras):
            active_cameras, uncertain = [], True
        protect_all = uncertain or ((locked or self._running()) and not status.get("running"))
        return writers, set(active_cameras), protect_all

    @staticmethod
    def recording_item(relative, info, activity=(set(), set(), False)):
        parts = recording_relative_parts(relative)
        camera = parts[0] if len(parts) > 1 else "기타"
        modified = datetime.fromtimestamp(info.st_mtime, KST)
        day = modified.date().isoformat()
        for part in parts[:-1]:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
                try:
                    day = date.fromisoformat(part).isoformat()
                    break
                except ValueError:
                    pass
        writers, cameras, protect_all = activity
        active = protect_all or camera in cameras or (info.st_dev, info.st_ino) in writers
        return {"id": recording_identifier(relative), "name": parts[-1], "relative_path": relative,
                "camera": camera, "camera_host": recording_filename_host(parts[-1]),
                "date": day, "size": info.st_size, "modified_at": modified.isoformat(),
                "active": active, "incomplete": info.st_size == 0 or "_pending_" in parts[-1]}

    def recording_trash_items(self, root_fd):
        items = []
        try:
            with recording_directory(root_fd, (RECORDING_TRASH,)) as trash:
                for identifier in os.listdir(trash):
                    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                        continue
                    try:
                        with recording_directory(trash, (identifier,)) as entry:
                            metadata = recording_read_json(entry, "metadata.json")
                            relative = metadata["relative_path"]
                            recording_relative_parts(relative)
                            info = os.stat("video.mp4", dir_fd=entry, follow_symlinks=False)
                            if not stat.S_ISREG(info.st_mode):
                                continue
                            item = self.recording_item(relative, info)
                            item.update(id=recording_scoped_id(identifier, os.fstat(root_fd), info, trash=True),
                                        trashed_at=metadata.get("trashed_at", ""))
                            items.append(item)
                    except (OSError, ValueError, TypeError, KeyError):
                        continue
        except FileNotFoundError:
            pass
        return items

    def recordings(self, camera="", day="", offset=0, limit=50, trash=False):
        try:
            if isinstance(offset, bool) or isinstance(limit, bool):
                raise ValueError()
            offset, limit = int(offset), int(limit)
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("영상 목록 범위가 올바르지 않습니다. 한 번에 1~100개를 조회하세요.") from None
        if not isinstance(camera, str) or not isinstance(day, str):
            raise ValueError("영상 검색 조건이 올바르지 않습니다.")
        if day:
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError()
            except ValueError:
                raise ValueError("영상 날짜는 YYYY-MM-DD 형식으로 선택하세요.") from None
        try:
            with self.recording_root() as root_fd:
                if trash:
                    items = self.recording_trash_items(root_fd)
                else:
                    candidates = list(recording_walk(root_fd))
                    activity = self.recording_activity(root_fd, candidates)
                    items = [self.recording_item(relative, info, activity) for relative, info in candidates]
                    root_info = os.fstat(root_fd)
                    for item, (relative, info) in zip(items, candidates):
                        item["id"] = recording_scoped_id(relative, root_info, info)
        except FileNotFoundError:
            items = []
        # A current URL is not evidence of the source of a historical recording.
        # Keep it separate from the host saved in new recording filenames.
        current_hosts = {entry.name: urlsplit(entry.url).hostname for entry in self.settings().cameras}
        for item in items:
            item["current_camera_host"] = current_hosts.get(item["camera"], "")
        cameras, dates = sorted({item["camera"] for item in items}), sorted({item["date"] for item in items}, reverse=True)
        selected = [item for item in items if (not camera or item["camera"] == camera) and (not day or item["date"] == day)]
        selected.sort(key=lambda item: (item.get("trashed_at", item["modified_at"]), item["relative_path"]), reverse=True)
        return {"items": selected[offset:offset + limit], "total": len(selected), "offset": offset, "limit": limit,
                "cameras": cameras, "dates": dates, "trash": bool(trash)}

    def recording_media(self, identifier):
        with self.recording_root() as root_fd:
            relative, expected = recording_scope_from_id(identifier, os.fstat(root_fd))
            fd = recording_open_file(root_fd, relative)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != expected:
                os.close(fd)
                raise ValueError("영상 파일이 변경되었습니다. 목록을 새로고침하세요.")
        return fd, relative.rsplit("/", 1)[-1]

    def _recording_require_closed(self, root_fd, relative, info):
        activity = self.recording_activity(root_fd, [(relative, info)])
        if self.recording_item(relative, info, activity)["active"]:
            raise ValueError("녹화 중이거나 사용 상태를 확인할 수 없는 영상은 보호됩니다. 녹화를 중지한 뒤 이동하세요.")

    @staticmethod
    def _recording_move(source_fd, source_name, target_fd, target_name, expected):
        """Move atomically with no overwrite, preserving raced-in files on either side."""
        current = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("영상 파일이 변경되었습니다. 목록을 새로고침하세요.")
        try:
            recording_rename_exclusive(source_fd, source_name, target_fd, target_name)
        except FileExistsError:
            raise ValueError("같은 위치에 영상이 이미 있습니다. 기존 파일을 덮어쓰지 않았습니다.") from None
        published = os.stat(target_name, dir_fd=target_fd, follow_symlinks=False)
        if not stat.S_ISREG(published.st_mode) or (published.st_dev, published.st_ino) != (expected.st_dev, expected.st_ino):
            # Source may have changed just before rename. Roll back only into a
            # vacant source path; if it is occupied, keep the moved file safely
            # at the destination (trash metadata remains available for recovery).
            try:
                recording_rename_exclusive(target_fd, target_name, source_fd, source_name)
            except (OSError, ValueError):
                pass
            raise ValueError("이동 중 영상 파일이 변경되어 작업을 중단했습니다. 파일은 보존했으며 목록을 새로고침하세요.")

    def trash_recording(self, identifier):
        with self.action_lock, self.recording_root() as root_fd:
            root_info = os.fstat(root_fd)
            relative, expected = recording_scope_from_id(identifier, root_info)
            parts = recording_relative_parts(relative)
            fd = recording_open_file(root_fd, relative)
            try:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != expected:
                    raise ValueError("영상 파일이 변경되었습니다. 목록을 새로고침하세요.")
                self._recording_require_closed(root_fd, relative, info)
                trash_id = uuid.uuid4().hex
                with recording_directory(root_fd, (RECORDING_TRASH,), create=True) as trash:
                    os.mkdir(trash_id, mode=0o700, dir_fd=trash)
                    try:
                        with recording_directory(trash, (trash_id,)) as entry:
                            metadata = {"relative_path": relative, "trashed_at": datetime.now(KST).isoformat()}
                            meta_fd = os.open("metadata.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=entry)
                            with os.fdopen(meta_fd, "w", encoding="utf-8") as stream:
                                json.dump(metadata, stream, ensure_ascii=False)
                                stream.flush()
                                os.fsync(stream.fileno())
                            with recording_directory(root_fd, parts[:-1]) as parent:
                                self._recording_require_closed(root_fd, relative, os.fstat(fd))
                                self._recording_move(parent, parts[-1], entry, "video.mp4", info)
                    except Exception:
                        # Remove only an empty failed entry, never a published video.
                        with recording_directory(trash, (trash_id,)) as entry:
                            if "video.mp4" not in os.listdir(entry):
                                if "metadata.json" in os.listdir(entry):
                                    os.unlink("metadata.json", dir_fd=entry)
                                os.rmdir(trash_id, dir_fd=trash)
                        raise
            finally:
                os.close(fd)
        return {"message": "영상을 휴지통으로 옮겼습니다. 영구 삭제하지 않았으며 복원할 수 있습니다.",
                "id": recording_scoped_id(trash_id, root_info, info, trash=True)}

    def restore_recording(self, identifier):
        with self.action_lock, self.recording_root() as root_fd:
            root_info = os.fstat(root_fd)
            trash_id, expected = recording_scope_from_id(identifier, root_info, trash=True)
            with recording_directory(root_fd, (RECORDING_TRASH,)) as trash:
                with recording_directory(trash, (trash_id,)) as entry:
                    metadata = recording_read_json(entry, "metadata.json")
                    relative = metadata["relative_path"]
                    parts = recording_relative_parts(relative)
                    fd = os.open("video.mp4", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=entry)
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError("일반 MP4 영상 파일만 복원할 수 있습니다.")
                        if (info.st_dev, info.st_ino) != expected:
                            raise ValueError("휴지통 영상이 변경되었습니다. 목록을 새로고침하세요.")
                        self._recording_require_closed(root_fd, relative, info)
                        with recording_directory(root_fd, parts[:-1], create=True) as target:
                            self._recording_move(entry, "video.mp4", target, parts[-1], info)
                        os.unlink("metadata.json", dir_fd=entry)
                    finally:
                        os.close(fd)
                os.rmdir(trash_id, dir_fd=trash)
        return {"message": "영상을 원래 위치로 복원했습니다.", "id": recording_scoped_id(relative, root_info, info)}

    def _running(self):
        return self.process is not None and self.process.poll() is None

    def state(self):
        result = {"running": self._running(), "mode": self.mode, "error": "", "external_running": False,
                  "forecast_busy": self.preview_busy, "now": datetime.now(KST).isoformat()}
        result["exit_code"] = self.process.poll() if self.process is not None else None
        with self.log_lock:
            result["logs"] = list(self.logs)
        try:
            settings = self.settings()
            status = safe_read_status(settings.output_dir)
            result["output_dir"] = str(settings.output_dir)
            result["cameras"] = [{"name": camera.name, "client_no": camera.client_no,
                                  "intersection": camera.intersection, "direction": camera.direction}
                                 for camera in settings.cameras]
            result["active_cameras"] = status.get("active_cameras", [])
            result["once_end"] = status.get("once_end")
            result["external_running"] = bool(status.get("running") and not result["running"])
            condition = settings.weather_condition or parse_weather_condition(default_weather_config(), self.config_path.parent)
            result["key_present"] = False
            result["weather_ready"] = False
            if condition:
                try:
                    validate_weather_access(condition)
                    result["weather_ready"] = True
                    result["key_present"] = condition.provider in ("data_go_kr", "kma_api_hub")
                except ValueError:
                    pass
            status_weather = status.get("weather") if status.get("weather_fingerprint") == condition.fingerprint else None
            preview = self.preview if self.preview and self.preview.get("fingerprint") == condition.fingerprint else None
            result["weather"] = status_weather or preview or {"enabled": bool(condition and condition.enabled)}
            if condition and condition.enabled and not status_weather and preview is None:
                gate = WeatherGate(condition, settings.output_dir, persist=False)
                result["weather"] = gate.snapshot()
            ancestor = settings.output_dir
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            usage = shutil.disk_usage(ancestor)
            result["disk"] = {"free_gb": round(usage.free / 1024 ** 3, 1), "total_gb": round(usage.total / 1024 ** 3, 1)}
        except (OSError, ValueError, configparser.Error, TypeError, KeyError) as exc:
            result["error"] = friendly_ui_error(exc)
        if not result["error"] and result["exit_code"] not in (None, 0):
            error_line = next((line for line in reversed(result["logs"]) if "ERROR" in line), "녹화 프로그램 실행에 실패했습니다.")
            result["error"] = "최근 녹화가 오류로 종료되었습니다. " + friendly_ui_error(ValueError(error_line))
        return result

    def save(self, patch, revision):
        allowed = {"setup_ini", "output_dir", "intersection_name", "weather", "recording_dates", "time_ranges",
                   "segment_minutes", "retention_days", "min_free_gb", "weather_condition", "camera_connection"}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError("저장할 설정 항목이 올바르지 않습니다.")
        with self.action_lock:
            data, current = self.read_config()
            if current != revision:
                raise ValueError("설정 파일이 다른 곳에서 변경되었습니다. 새로고침 후 다시 저장하세요.")
            if self._running() and self.mode == "once":
                raise ValueError("시험 녹화가 끝나거나 중지한 뒤 설정을 저장하세요.")
            # camera_connection is a password-redacted editor model, not an on-disk field.
            if "camera_connection" in patch:
                data["cameras"] = cameras_from_connection(patch["camera_connection"], data)
            data.update({key: value for key, value in patch.items() if key != "camera_connection"})
            if "time_ranges" in patch:
                data.pop("schedules", None)
            if "segment_minutes" in patch:
                data.pop("segment_seconds", None)
            proposed = settings_from_data(data, self.config_path.parent)
            resume = self._running() and self.mode == "scheduled"
            if resume:
                check_output_access(proposed)
                check_ffmpeg(proposed)
                if proposed.weather_condition is not None and proposed.weather_condition.enabled:
                    validate_weather_access(proposed.weather_condition)
            if self._running():
                self._stop()
            if self.read_config()[1] != current:
                raise ValueError("설정 파일이 다른 곳에서 변경되었습니다. 새로고침 후 다시 저장하세요.")
            atomic_private_json(self.config_path, data)
            self.preview = None
            if resume:
                self.start("scheduled")
            return {"message": "설정을 저장하고 예약 녹화를 다시 시작했습니다." if resume else "설정을 저장했습니다.",
                    "revision": self.read_config()[1]}

    def check(self):
        settings = self.settings(refresh=True)
        check_output_access(settings)
        check_ffmpeg(settings)
        return {"message": "설정 검사를 통과했습니다. 실제 영상은 5분 시험 녹화 후 확인하세요."}

    def start(self, mode, minutes=5, start_at=None):
        with self.action_lock:
            if self.closing:
                raise ValueError("화면을 종료하고 있습니다.")
            if self._running():
                raise ValueError("이미 녹화가 실행 중입니다. 먼저 중지하세요.")
            if mode not in ("scheduled", "once"):
                raise ValueError("녹화 실행 방식이 올바르지 않습니다.")
            settings = self.settings(refresh=True)
            check_output_access(settings)
            check_ffmpeg(settings)
            if sys.platform != "linux":
                raise ValueError("녹화 시작은 Linux ED에서 실행하세요. 이 PC에서는 설정 화면을 확인할 수 있습니다.")
            if safe_read_status(settings.output_dir).get("running"):
                raise ValueError("별도 녹화 서비스가 실행 중입니다. 기존 서비스를 중지한 뒤 화면에서 시작하세요.")
            if mode == "scheduled" and settings.weather_condition is not None and settings.weather_condition.enabled:
                validate_weather_access(settings.weather_condition)
            command = [sys.executable, str(Path(__file__).resolve()), "--config", str(self.config_path)]
            if mode == "once":
                make_once_window(minutes, start_at)
                command += ["--record-minutes", str(minutes)]
                if start_at:
                    command += ["--start-at", start_at]
            with self.log_lock:
                self.logs.clear()
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                            start_new_session=True)
            self.mode = mode
            self.reader = threading.Thread(target=self._read_log, args=(self.process,), daemon=True)
            self.reader.start()
            return {"message": "예약 녹화를 시작했습니다. 시간표와 날씨 조건에 따라 대기할 수 있습니다." if mode == "scheduled"
                    else "한 번 녹화를 시작했습니다. 지정한 시간이 끝나면 자동 종료합니다."}

    def _read_log(self, process):
        try:
            for line in process.stdout:
                value = line.rstrip()[:1000]
                if any(secret in value.lower() for secret in ("rtsp://", "servicekey=", "authkey=")):
                    continue
                with self.log_lock:
                    self.logs.append(value)
                LOG.info("Recorder: %s", value)
        finally:
            process.stdout.close()

    def _stop(self):
        if self._running():
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                raise ValueError("녹화 종료가 지연되고 있습니다. 잠시 후 상태를 다시 확인하세요.") from None
        if self.reader is not None:
            self.reader.join(timeout=1)

    def stop(self):
        with self.action_lock:
            self._stop()
        return {"message": "녹화를 중지했습니다. 저장한 영상과 설정은 유지됩니다."}

    def save_key(self, key):
        if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(char.isspace() for char in key.strip()):
            raise ValueError("인증키를 공백 없이 입력하세요.")
        with self.action_lock:
            settings = self.settings()
            condition = settings.weather_condition or parse_weather_condition(default_weather_config(), self.config_path.parent)
            if condition.provider in ("open_meteo", "met_no"):
                raise ValueError("선택한 예보 서비스는 API 키가 필요하지 않습니다.")
            if condition.key_file == self.config_path or condition.key_file.suffix.lower() != ".key":
                raise ValueError("인증키 저장 파일은 설정과 다른 .key 파일로 지정하세요.")
            atomic_private_text(condition.key_file, key.strip() + "\n", require_private=True)
        return {"message": "인증키를 저장했습니다. 화면이나 로그에 키를 다시 표시하지 않습니다."}

    def forecast(self):
        with self.action_lock:
            if self.preview_busy:
                return {"message": "날씨 예보를 조회하고 있습니다."}
            settings = self.settings()
            condition = settings.weather_condition or parse_weather_condition(default_weather_config(), self.config_path.parent)
            validate_weather_access(condition)
            if self._running() and self.mode == "scheduled" and condition.enabled and hasattr(signal, "SIGUSR1"):
                self.process.send_signal(signal.SIGUSR1)
                return {"message": "실행 중인 녹화에 예보 갱신을 요청했습니다."}
            self.preview_busy = True

            def query():
                gate = WeatherGate(condition, settings.output_dir, persist=False)
                try:
                    gate.refresh()
                    self.preview = gate.snapshot()
                finally:
                    self.preview_busy = False

            self.preview_thread = threading.Thread(target=query, daemon=True)
            self.preview_thread.start()
            return {"message": "예보를 확인합니다. 유효한 캐시가 있으면 추가 다운로드 없이 재사용합니다."}

    def close(self):
        with self.action_lock:
            self.closing = True
            self._stop()


def make_ui_handler(controller, token, port):
    hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}

    class UiHandler(BaseHTTPRequestHandler):
        server_version = "EDRecorderUI"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def _json(self, value, code=200):
            content = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self._send(content, "application/json; charset=utf-8", code)

        def _send(self, content, kind, code=200):
            compressed = len(content) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "").lower()
            if compressed:
                content = gzip.compress(content)
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self._security_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def _security_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'nonce-" + token
                             + "'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

        def _allowed(self, api=False, media_token=None):
            if self.headers.get("Host") not in hosts:
                self._json({"error": "허용되지 않은 접속 주소입니다."}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + host for host in hosts}:
                self._json({"error": "다른 웹사이트에서 보낸 요청은 허용하지 않습니다."}, 403)
                return False
            supplied = self.headers.get("X-Recorder-Token", "")
            if media_token is not None and not supplied:
                supplied = media_token
            if api and not secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
                self._json({"error": "화면을 새로고침한 뒤 다시 시도하세요."}, 403)
                return False
            return True

        @staticmethod
        def _query_value(query, key, default=""):
            values = query.get(key, [default])
            if len(values) != 1:
                raise ValueError("요청 항목을 중복해서 지정할 수 없습니다.")
            return values[0]

        def _media(self, identifier, download=False):
            fd, name = controller.recording_media(identifier)
            try:
                size = os.fstat(fd).st_size
                try:
                    start, end, code = recording_range(self.headers.get("Range"), size)
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */{}".format(size))
                    self.send_header("Content-Length", "0")
                    self._security_headers()
                    self.end_headers()
                    return
                length = end - start + 1 if size else 0
                self.send_response(code)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if code == 206:
                    self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
                disposition = "attachment" if download else "inline"
                self.send_header("Content-Disposition", disposition + "; filename=\"recording.mp4\"; filename*=UTF-8''" + quote(name, safe=""))
                self._security_headers()
                self.end_headers()
                if self.command == "HEAD":
                    return
                os.lseek(fd, start, os.SEEK_SET)
                while length:
                    block = os.read(fd, min(length, 256 * 1024))
                    if not block:
                        break
                    self.wfile.write(block)
                    length -= len(block)
            finally:
                os.close(fd)

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
                path = parsed.path
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
                media_token = self._query_value(query, "token") if path == "/api/recordings/media" else None
                if not self._allowed(path != "/", media_token):
                    return
                if path == "/":
                    page = embedded_text(EMBEDDED_UI).replace("__RECORDER_TOKEN__", token)
                    self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/config":
                    self._json(controller.configuration())
                elif path == "/api/state":
                    self._json(controller.state())
                elif path == "/api/recordings":
                    trash = self._query_value(query, "trash", "0")
                    if trash not in ("0", "1"):
                        raise ValueError("휴지통 조회 조건이 올바르지 않습니다.")
                    self._json(controller.recordings(self._query_value(query, "camera"), self._query_value(query, "date"),
                               self._query_value(query, "offset", "0"), self._query_value(query, "limit", "50"), trash == "1"))
                elif path == "/api/recordings/media":
                    self._media(self._query_value(query, "id"), self._query_value(query, "download") == "1")
                else:
                    self._json({"error": "없는 경로입니다."}, 404)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except (OSError, ValueError, TypeError, KeyError, configparser.Error) as exc:
                self._json({"error": friendly_ui_error(exc)}, 400)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            if not self._allowed(True):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024 or self.headers.get_content_type() != "application/json":
                    raise ValueError("요청 형식 또는 크기가 올바르지 않습니다.")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("요청 형식이 올바르지 않습니다.")
                if self.path == "/api/save":
                    result = controller.save(data.get("config"), data.get("revision"))
                elif self.path == "/api/check":
                    result = controller.check()
                elif self.path == "/api/start":
                    result = controller.start(data.get("mode"), data.get("minutes", 5), data.get("start_at") or None)
                elif self.path == "/api/stop":
                    result = controller.stop()
                elif self.path == "/api/forecast":
                    result = controller.forecast()
                elif self.path == "/api/key":
                    result = controller.save_key(data.get("key"))
                elif self.path == "/api/discover":
                    result = {"paths": controller.setup_candidates()}
                elif self.path == "/api/recordings/trash":
                    result = controller.trash_recording(data.get("id"))
                elif self.path == "/api/recordings/restore":
                    result = controller.restore_recording(data.get("id"))
                else:
                    self._json({"error": "없는 경로입니다."}, 404)
                    return
                self._json(result)
            except (OSError, ValueError, TypeError, KeyError, configparser.Error, subprocess.SubprocessError) as exc:
                self._json({"error": friendly_ui_error(exc)}, 400)

    return UiHandler


def run_recorder_ui(config_path, port=8765, autostart=False):
    controller = RecorderUiController(config_path)
    handler = make_ui_handler(controller, secrets.token_urlsafe(32), port)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    stopping = threading.Event()

    def stop_server(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    for sig in signals:
        signal.signal(sig, stop_server)
    url = "http://127.0.0.1:{}/".format(port)
    LOG.info("Recorder control panel: %s (config=%s)", url, config_path)
    if autostart:
        try:
            controller.start("scheduled")
        except (OSError, ValueError, configparser.Error) as exc:
            LOG.error("UI auto-start: %s", friendly_ui_error(exc))
    elif not os.environ.get("INVOCATION_ID"):
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()
        controller.close()
