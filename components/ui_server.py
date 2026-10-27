# Embedded into record.py by build_embedded.py; localhost-only, standard-library UI.
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import webbrowser
import gzip


STATUS_NAME = ".recorder-status.json"


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
            for item in cameras if isinstance(item, dict)]
        return {"config": safe, "revision": revision, "path": str(self.config_path), "regions": region_data()}

    def setup_candidates(self):
        candidates = set()
        for parent in list(Path(__file__).resolve().parents)[:4]:
            candidate = parent / "config" / "setup.ini"
            if candidate.is_file():
                candidates.add(str(candidate.resolve()))
        candidates.update(str(path) for path in scan_setup_ini(DEFAULT_SETUP_SEARCH_ROOT))
        return sorted(candidates)

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
                   "segment_minutes", "retention_days", "min_free_gb", "weather_condition"}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError("저장할 설정 항목이 올바르지 않습니다.")
        with self.action_lock:
            data, current = self.read_config()
            if current != revision:
                raise ValueError("설정 파일이 다른 곳에서 변경되었습니다. 새로고침 후 다시 저장하세요.")
            if self._running() and self.mode == "once":
                raise ValueError("시험 녹화가 끝나거나 중지한 뒤 설정을 저장하세요.")
            data.update(patch)
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
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'nonce-" + token
                             + "'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)

        def _allowed(self, api=False):
            if self.headers.get("Host") not in hosts:
                self._json({"error": "허용되지 않은 접속 주소입니다."}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + host for host in hosts}:
                self._json({"error": "다른 웹사이트에서 보낸 요청은 허용하지 않습니다."}, 403)
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Recorder-Token", ""), token):
                self._json({"error": "화면을 새로고침한 뒤 다시 시도하세요."}, 403)
                return False
            return True

        def do_GET(self):
            if not self._allowed(self.path != "/"):
                return
            try:
                if self.path == "/":
                    page = embedded_text(EMBEDDED_UI).replace("__RECORDER_TOKEN__", token)
                    self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                elif self.path == "/api/config":
                    self._json(controller.configuration())
                elif self.path == "/api/state":
                    self._json(controller.state())
                else:
                    self._json({"error": "없는 경로입니다."}, 404)
            except (OSError, ValueError, TypeError, KeyError, configparser.Error) as exc:
                self._json({"error": friendly_ui_error(exc)}, 400)

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
