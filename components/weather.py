# Embedded into record.py by build_embedded.py; uses its standard-library helpers.
import base64
import hashlib
import zlib
from urllib.parse import urlencode, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError, HTTPError


WEATHER_LABELS = {"rain": "비", "snow": "눈", "clear": "맑음", "cloudy": "흐림"}
WEATHER_STATE_NAME = ".weather-forecast.json"
WEATHER_PROVIDERS = {
    "data_go_kr": ("https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst", "serviceKey"),
    "kma_api_hub": ("https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst", "authKey"),
    "open_meteo": ("https://api.open-meteo.com/v1/forecast", None),
    "met_no": ("https://api.met.no/weatherapi/locationforecast/2.0/compact", None),
}
WEATHER_SOURCES = {
    "data_go_kr": ("기상청", "https://www.data.go.kr/data/15084084/openapi.do", "발표"),
    "kma_api_hub": ("기상청", "https://apihub.kma.go.kr", "발표"),
    "open_meteo": ("Open-Meteo", "https://open-meteo.com/", "응답 확인"),
    "met_no": ("MET Norway", "https://api.met.no/", "자료 갱신"),
}
_REGION_DATA = None


def embedded_text(encoded):
    return zlib.decompress(base64.b64decode(encoded)).decode("utf-8")


def region_data():
    global _REGION_DATA
    if _REGION_DATA is None:
        _REGION_DATA = json.loads(embedded_text(EMBEDDED_REGIONS))
    return _REGION_DATA


@dataclass(frozen=True)
class WeatherCondition:
    enabled: bool
    region: tuple
    nx: int
    ny: int
    conditions: tuple
    provider: str
    key_file: Path = field(repr=False)
    poll_minutes: float = 30
    latitude: float = 0
    longitude: float = 0
    met_contact: str = ""

    @property
    def fingerprint(self):
        values = [self.region, self.nx, self.ny, sorted(self.conditions), self.provider]
        if self.provider in ("open_meteo", "met_no"):
            values += [self.latitude, self.longitude]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def default_weather_config():
    return {"enabled": False, "region": {"sido": "부산광역시", "sigungu": "해운대구", "eupmyeondong": ""},
            "conditions": ["rain"], "provider": "open_meteo", "api_key_file": "kma_service.key",
            "poll_minutes": 30, "met_contact": ""}


def parse_weather_condition(data, base):
    if data is None:
        return None
    allowed = {"enabled", "region", "conditions", "provider", "api_key_file", "poll_minutes", "met_contact"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Invalid weather_condition fields")
    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("weather_condition.enabled must be true or false")
    region = data.get("region", {})
    if not isinstance(region, dict) or set(region) - {"sido", "sigungu", "eupmyeondong"}:
        raise ValueError("weather_condition.region requires sido, sigungu and optional eupmyeondong")
    names = tuple(config_label(region.get(key, ""), "weather region") for key in ("sido", "sigungu", "eupmyeondong"))
    matches = [row for row in region_data()["rows"] if tuple(row[:3]) == names]
    if not matches and (enabled or any(names)):
        raise ValueError("Weather region is not in the KMA region list; choose the exact province/city/district")
    nx, ny = matches[0][3:5] if matches else (0, 0)
    conditions = data.get("conditions", ["rain"])
    if (not isinstance(conditions, list) or not conditions
            or any(not isinstance(item, str) or item not in WEATHER_LABELS for item in conditions)
            or len(set(conditions)) != len(conditions)):
        raise ValueError("weather conditions must be unique values from rain, snow, clear, cloudy")
    provider = data.get("provider", "data_go_kr")
    if not isinstance(provider, str) or provider not in WEATHER_PROVIDERS:
        raise ValueError("weather provider must be open_meteo, met_no, data_go_kr or kma_api_hub")
    key_value = data.get("api_key_file", "kma_service.key")
    resolve_path(key_value, base, "api_key_file")
    key_file = Path(key_value).expanduser()
    key_file = (Path(base) / key_file).absolute() if not key_file.is_absolute() else key_file.absolute()
    minutes = number(data.get("poll_minutes", 30), "weather poll_minutes", 10, 180)
    latitude, longitude = matches[0][5:7] if matches and len(matches[0]) >= 7 else (0, 0)
    contact = config_label(data.get("met_contact", ""), "met_contact")
    if len(contact) > 300:
        raise ValueError("MET Norway 연락처는 300자 이내로 입력하세요.")
    return WeatherCondition(enabled, names, nx, ny, tuple(conditions), provider, key_file, minutes,
                            latitude, longitude, contact)


def validate_weather_access(condition):
    if condition.provider == "met_no":
        return met_user_agent(condition)
    if condition.provider == "open_meteo":
        return None
    return weather_api_key(condition)


def weather_api_key(condition):
    value = os.environ.get("KMA_SERVICE_KEY", "").strip()
    if not value:
        try:
            if condition.key_file.is_symlink() or condition.key_file.stat().st_size > 4096:
                raise ValueError("기상청 인증키 파일을 확인하세요.")
            value = condition.key_file.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            raise ValueError("기상청 인증키가 없습니다. 화면에서 인증키를 한 번 등록하세요.") from None
    if not value or len(value) > 4096 or any(char.isspace() for char in value):
        raise ValueError("기상청 인증키 형식을 확인하세요.")
    value = unquote(value)
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("기상청 인증키 형식을 확인하세요.")
    return value


def atomic_private_json(path, value):
    atomic_private_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_private_text(path, value, require_private=False):
    import tempfile
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Refusing to replace a symlink or non-file configuration")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=str(path.parent),
                                         prefix=".recorder-save-", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(str(temporary), 0o600)
            if require_private and sys.platform == "linux" and os.fstat(handle.fileno()).st_mode & 0o777 != 0o600:
                raise ValueError("인증키는 Linux 파일 권한을 지원하는 폴더에 저장하세요. 예: /home/dis/tools")
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise ValueError("Refusing to replace a symlink configuration")
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def latest_forecast_base(now):
    ready = now.astimezone(KST) - timedelta(minutes=10)
    candidates = [ready.replace(hour=hour, minute=0, second=0, microsecond=0)
                  for hour in (2, 5, 8, 11, 14, 17, 20, 23)]
    return max((value for value in candidates if value <= ready),
               default=(ready - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0))


class NoWeatherRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("기상청 조회 주소가 변경되었습니다. 프로그램 업데이트를 확인하세요.")


def fetch_kma_forecast(condition, base, key, opener=None):
    opener = opener or build_opener(NoWeatherRedirect())
    endpoint, key_name = WEATHER_PROVIDERS[condition.provider]
    results = []
    for page in range(1, 7):
        params = {key_name: key, "pageNo": page, "numOfRows": 1000, "dataType": "JSON",
                  "base_date": base.strftime("%Y%m%d"), "base_time": base.strftime("%H%M"),
                  "nx": condition.nx, "ny": condition.ny}
        request = Request(endpoint + "?" + urlencode(params), headers={"User-Agent": "ED-Camera-Recorder/4"})
        try:
            with opener.open(request, timeout=10) as response:
                body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("기상청 응답이 너무 큽니다.")
            response = json.loads(body.decode("utf-8-sig"))["response"]
            code = str(response["header"]["resultCode"])
            if code != "00":
                if code in ("20", "30", "31", "32"):
                    raise ValueError("기상청 인증키 또는 해당 서비스의 활용 승인을 확인하세요.")
                if code in ("22", "23"):
                    raise ValueError("기상청 호출 한도를 초과했습니다. 잠시 후 다시 조회합니다.")
                raise ValueError("기상청 예보를 아직 받을 수 없습니다. 잠시 후 다시 조회합니다.")
            payload = response["body"]
            try:
                total = int(payload["totalCount"])
            except (ValueError, TypeError):
                raise ValueError("기상청 예보 항목 수가 올바르지 않습니다.") from None
            items = payload["items"]["item"]
            if not isinstance(items, list) or not items or not 1 <= total <= 6000:
                raise ValueError("기상청 예보 항목이 비어 있거나 올바르지 않습니다.")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("기상청 예보 항목 형식을 확인할 수 없습니다.")
                try:
                    grid = int(item.get("nx", condition.nx)), int(item.get("ny", condition.ny))
                except (ValueError, TypeError):
                    raise ValueError("기상청 예보의 지역 좌표가 올바르지 않습니다.") from None
                if (str(item.get("baseDate")) != base.strftime("%Y%m%d")
                        or str(item.get("baseTime")) != base.strftime("%H%M")
                        or grid != (condition.nx, condition.ny)):
                    raise ValueError("기상청 예보의 발표 시각 또는 지역이 요청과 다릅니다.")
            results.extend(items)
            if len(results) >= total:
                return results
        except (HTTPError, URLError, TimeoutError, OSError):
            raise ValueError("기상청에 연결하지 못했습니다. 장비의 인터넷 연결을 확인하세요.") from None
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError):
            raise ValueError("기상청 예보 응답 형식을 확인할 수 없습니다.") from None
    raise ValueError("기상청 예보 전체 항목을 받지 못했습니다.")


def forecast_points(items, issue):
    points = {}
    for item in items:
        category = item.get("category")
        if category not in ("PTY", "SKY", "POP", "TMP"):
            continue
        try:
            stamp = datetime.strptime(str(item["fcstDate"]) + str(item["fcstTime"]), "%Y%m%d%H%M").replace(tzinfo=KST)
            value = float(item["fcstValue"])
            if not math.isfinite(value) or abs(value) >= 900:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        point = points.setdefault(stamp.isoformat(), {"time": stamp.isoformat(), "issue": issue.isoformat()})
        point[category] = value
    return points


def forecast_matches(point, condition):
    pty, sky = point.get("PTY"), point.get("SKY")
    if condition == "rain":
        return pty in (1, 2, 4)
    if condition == "snow":
        return pty in (2, 3)
    if condition == "clear":
        return pty == 0 and sky == 1
    if condition == "cloudy":
        return pty == 0 and sky in (3, 4)
    return False


class WeatherGate:
    """Refresh forecasts off the recorder thread and retain a positive decision for the whole start date."""
    def __init__(self, condition, root, fetcher=fetch_kma_forecast, persist=True):
        self.condition = condition
        self.path = Path(root) / WEATHER_STATE_NAME
        self.fetcher = fetcher
        self.persist = persist
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.points = {}
        self.approved = {}
        self.checked_at = None
        self.issue = None
        self.error = ""
        self.network = {}
        self.public_client = PublicWeatherClient(condition, root) if condition.provider in ("open_meteo", "met_no") else None
        self._load()

    def _load(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 2 * 1024 * 1024:
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("fingerprint") != self.condition.fingerprint:
                return
            self.points = value.get("points", {})
            self.approved = value.get("approved", {})
            self.checked_at = value.get("checked_at")
            self.issue = value.get("issue")
            self.network = value.get("network", {})
            if not isinstance(self.points, dict) or not isinstance(self.approved, dict):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            self.points, self.approved, self.checked_at, self.issue = {}, {}, None, None

    def _save(self):
        if not self.persist:
            return
        atomic_private_json(self.path, {"version": 1, "fingerprint": self.condition.fingerprint,
                            "points": self.points, "approved": self.approved, "checked_at": self.checked_at,
                            "issue": self.issue, "network": self.network})

    def _matches(self, day):
        labels = set()
        for point in self.points.values():
            if isinstance(point, dict) and point_covers_day(point, day):
                labels.update(value for value in self.condition.conditions if forecast_matches(point, value))
        return sorted(labels)

    def _fresh(self, now):
        try:
            checked = datetime.fromisoformat(self.checked_at)
            return checked.tzinfo is not None and 0 <= (now - checked).total_seconds() <= 6 * 3600
        except (ValueError, TypeError):
            return False

    def allows_date(self, day, now=None):
        now = (now or datetime.now(KST)).astimezone(KST)
        with self.lock:
            if self.approved.get(day.isoformat()):
                return True
            if not self._fresh(now):
                return False
            matches = self._matches(day)
            if matches and day == now.date():
                self.approved[day.isoformat()] = matches
                try:
                    self._save()
                except (OSError, ValueError):
                    self.error = "날씨 판단 결과를 저장하지 못했습니다. 저장 폴더 권한을 확인하세요."
                    # Do not enable a day if restart would silently lose its decision.
                    self.approved.pop(day.isoformat(), None)
                    return False
            return bool(matches)

    def label_for_day(self, day):
        with self.lock:
            values = self.approved.get(day.isoformat()) or self._matches(day)
            names = [WEATHER_LABELS[value] for value in values if value in WEATHER_LABELS]
            return "-".join(names) + "예보일" if names else ""

    def refresh(self, now=None):
        now = (now or datetime.now(KST)).astimezone(KST)
        try:
            updates = {}
            checked = now
            if self.public_client is not None:
                updates, newest, checked = self.public_client.fetch(now)
                self.network = self.public_client.stats(now)
            else:
                key = weather_api_key(self.condition)
                newest = latest_forecast_base(now)
                baseline = (now - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
                with self.lock:
                    need_baseline = not any(str(value).startswith(now.date().isoformat() + "T00:") for value in self.points)
                if need_baseline and baseline != newest:
                    try:
                        updates.update(forecast_points(self.fetcher(self.condition, baseline, key), baseline))
                    except ValueError:
                        pass
                updates.update(forecast_points(self.fetcher(self.condition, newest, key), newest))
            if not any("PTY" in point for point in updates.values()):
                raise ValueError("날씨 조건을 판단할 예보가 없습니다.")
            with self.lock:
                for stamp, point in updates.items():
                    if stamp not in self.points or point["issue"] >= self.points[stamp].get("issue", ""):
                        self.points[stamp] = point
                cutoff = (now.date() - timedelta(days=1)).isoformat()
                self.points = {stamp: point for stamp, point in self.points.items() if stamp[:10] >= cutoff}
                self.approved = {day: values for day, values in self.approved.items() if day >= cutoff}
                self.checked_at, self.issue, self.error = checked.isoformat(), newest.isoformat(), ""
                self._save()
            self.allows_date(now.date(), now)
        except (OSError, ValueError, TypeError) as exc:
            with self.lock:
                if self.public_client is not None:
                    self.network = self.public_client.stats(now)
                self.error = str(exc) if isinstance(exc, ValueError) else "예보를 갱신하지 못했습니다. 잠시 후 다시 조회합니다."
            LOG.warning("Weather forecast unavailable: %s", self.error)

    def snapshot(self, now=None):
        now = (now or datetime.now(KST)).astimezone(KST)
        with self.lock:
            allowed = self.allows_date(now.date(), now)
            hours = [point for stamp, point in sorted(self.points.items()) if point_covers_day(point, now.date())]
            source, source_url, issue_label = WEATHER_SOURCES[self.condition.provider]
            return {"enabled": True, "region": " ".join(value for value in self.condition.region if value),
                    "allowed": allowed, "label": self.label_for_day(now.date()), "error": self.error,
                    "checked_at": self.checked_at, "issue": self.issue, "hours": hours,
                    "known": self._fresh(now) and bool(hours), "approved": bool(self.approved.get(now.date().isoformat())),
                    "provider": self.condition.provider, "source": source, "source_url": source_url,
                    "issue_label": issue_label, "network": self.network, "fingerprint": self.condition.fingerprint}

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            self.refresh()
            self.wake.wait(self.condition.poll_minutes * 60 + random.uniform(15, 60))
            self.wake.clear()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
