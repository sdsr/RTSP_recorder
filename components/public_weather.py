# Public forecast adapters, embedded into record.py. No third-party Python packages.
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import random
import stat


PUBLIC_WEATHER_LIMIT = 4 * 1024 * 1024


def met_user_agent(condition):
    contact = condition.met_contact.strip()
    valid = bool(re.fullmatch(r"[^@\s/]+@[^@\s/]+\.[^@\s/]+", contact))
    if contact.startswith(("https://", "http://")):
        parsed = urlsplit(contact)
        valid = bool(parsed.hostname and not parsed.username and not parsed.password)
    if not contact.isascii() or any(char.isspace() or ord(char) < 32 for char in contact) or not valid:
        raise ValueError("MET Norway에는 연락용 이메일 또는 프로젝트 홈페이지 주소를 입력하세요. 개인 연락처도 가능합니다.")
    return "EDCameraRecorder/4.1 " + contact


def public_iso(value, naive_timezone=None):
    if not isinstance(value, str):
        raise ValueError("예보 시각 형식이 올바르지 않습니다.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("예보 시각 형식이 올바르지 않습니다.") from None
    if result.tzinfo is None:
        if naive_timezone is None:
            raise ValueError("예보 시각에 시간대 정보가 없습니다.")
        result = result.replace(tzinfo=naive_timezone)
    return result.astimezone(KST)


def point_covers_day(point, day):
    if "period_end" not in point:
        return str(point.get("time", "")).startswith(day.isoformat() + "T")
    try:
        start, end = public_iso(point["time"]), public_iso(point["period_end"])
        midnight = datetime(day.year, day.month, day.day, tzinfo=KST)
        return start < midnight + timedelta(days=1) and end > midnight and end > start
    except (ValueError, TypeError, KeyError):
        return False


def wmo_weather(code):
    if isinstance(code, bool) or not isinstance(code, (int, float)):
        return None
    if code in (0, 1):
        return 0, 1, "맑음"
    if code in (2, 3):
        return 0, 3 if code == 2 else 4, "구름" if code == 2 else "흐림"
    if code in (45, 48):
        return 0, None, "안개"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67):
        return 1, 4, "비"
    if code in (71, 73, 75, 77, 85, 86):
        return 3, 4, "눈"
    if code in (80, 81, 82):
        return 4, 4, "소나기"
    if code in (95, 96, 99):
        return 1, 4, "뇌우"
    return None


def met_symbol_weather(symbol):
    if not isinstance(symbol, str):
        return None
    value = re.sub(r"_(?:day|night|polartwilight)$", "", symbol)
    simple = {"clearsky": (0, 1, "맑음"), "fair": (0, 1, "맑음"),
              "partlycloudy": (0, 3, "구름"), "cloudy": (0, 4, "흐림"), "fog": (0, None, "안개")}
    if value in simple:
        return simple[value]
    match = re.fullmatch(r"(?:light|heavy)?(rain|sleet|snow)(showers)?(?:andthunder)?", value)
    if match is None:
        return None
    kind, showers = match.groups()
    return {"rain": (4 if showers else 1, 4, "소나기" if showers else "비"),
            "sleet": (2, 4, "비/눈"), "snow": (3, 4, "눈")}[kind]


def parse_open_meteo(body, checked):
    if not isinstance(body, dict) or body.get("timezone") != "Asia/Seoul" or body.get("utc_offset_seconds") != 32400:
        raise ValueError("Open-Meteo 예보의 한국 시간 정보를 확인할 수 없습니다.")
    hourly = body.get("hourly", {})
    if not isinstance(hourly, dict):
        raise ValueError("Open-Meteo 시간별 예보 형식이 올바르지 않습니다.")
    times, codes = hourly.get("time"), hourly.get("weather_code")
    temperatures = hourly.get("temperature_2m", [])
    if (not isinstance(times, list) or not 1 <= len(times) <= 1000 or not isinstance(codes, list)
            or len(times) != len(codes) or not isinstance(temperatures, list)):
        raise ValueError("Open-Meteo 시간별 예보 형식이 올바르지 않습니다.")
    points = {}
    for index, (raw_time, code) in enumerate(zip(times, codes)):
        weather = wmo_weather(code)
        if weather is None:
            continue
        stamp = public_iso(raw_time, KST)
        pty, sky, description = weather
        point = {"time": stamp.isoformat(), "issue": checked.isoformat(), "PTY": pty,
                 "description": description, "weather_code": code}
        if sky is not None:
            point["SKY"] = sky
        temp = temperatures[index] if index < len(temperatures) else None
        if isinstance(temp, (int, float)) and math.isfinite(temp) and abs(temp) < 900:
            point["TMP"] = temp
        points[stamp.isoformat()] = point
    if not points:
        raise ValueError("Open-Meteo에서 판단 가능한 날씨 예보를 받지 못했습니다.")
    return points, checked


def parse_met_forecast(body, checked):
    try:
        properties = body["properties"]
        issue = public_iso(properties["meta"]["updated_at"])
        series = properties["timeseries"]
        if not isinstance(series, list) or not 1 <= len(series) <= 1000:
            raise ValueError()
        points = {}
        for item in series:
            start = public_iso(item["time"])
            data = item["data"]
            for key, hours in (("next_1_hours", 1), ("next_6_hours", 6), ("next_12_hours", 12)):
                block = data.get(key, {})
                weather = met_symbol_weather(block.get("summary", {}).get("symbol_code"))
                if weather is None:
                    continue
                pty, sky, description = weather
                point = {"time": start.isoformat(), "issue": issue.isoformat(), "PTY": pty,
                         "description": description, "period_hours": hours,
                         "period_end": (start + timedelta(hours=hours)).isoformat()}
                if sky is not None:
                    point["SKY"] = sky
                temp = data.get("instant", {}).get("details", {}).get("air_temperature")
                if isinstance(temp, (int, float)) and math.isfinite(temp) and abs(temp) < 900:
                    point["TMP"] = temp
                points[start.isoformat()] = point
                break
        if not points:
            raise ValueError()
        return points, issue
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("MET Norway 예보의 시각 또는 날씨 정보를 확인할 수 없습니다.") from None


def http_date(value):
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError, IndexError):
        return None


def public_cache_deadline(headers, now, poll_minutes):
    minimum = now + timedelta(minutes=poll_minutes)
    expires = http_date(headers.get("Expires"))
    match = re.search(r"(?:^|,)\s*max-age\s*=\s*(\d+)", headers.get("Cache-Control", ""), re.I)
    if match:
        # Age is the time already spent in an upstream cache.
        try:
            age = max(0, int(headers.get("Age", "0")))
        except ValueError:
            age = 0
        max_age = now + timedelta(seconds=max(0, int(match.group(1)) - age))
        expires = max_age if expires is None else max(expires, max_age)
    return max(minimum, expires) if expires is not None else minimum


class PublicWeatherRedirect(HTTPRedirectHandler):
    def __init__(self, hostname):
        super().__init__()
        self.hostname = hostname

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != self.hostname:
            raise ValueError("예보 서비스의 조회 주소가 변경되었습니다. 프로그램 업데이트를 확인하세요.")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


@contextmanager
def public_cache_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("예보 캐시 잠금 파일을 확인하세요.")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("예보 캐시 잠금 경로가 일반 파일이 아닙니다.")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    handle.write(b"0")
                handle.seek(0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise ValueError("다른 녹화 작업에서 예보를 조회 중입니다. 잠시 후 다시 확인하세요.") from None
        yield
    finally:
        if locked:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        handle.close()


class PublicWeatherClient:
    def __init__(self, condition, root, opener=None):
        self.condition = condition
        params = {"latitude": "{:.4f}".format(condition.latitude), "longitude": "{:.4f}".format(condition.longitude)}
        endpoint = WEATHER_PROVIDERS[condition.provider][0]
        if condition.provider == "open_meteo":
            params.update(hourly="weather_code,temperature_2m", timezone="Asia/Seoul", forecast_days=3, past_days=1)
        else:
            params = {"lat": params["latitude"], "lon": params["longitude"]}
        self.url = endpoint + "?" + urlencode(params)
        self.identity = hashlib.sha256(self.url.encode("ascii")).hexdigest()
        self.path = Path(root) / (".weather-http-" + self.identity[:16] + ".json")
        self.lock_path = self.path.with_suffix(".lock")
        self.opener = opener or build_opener(PublicWeatherRedirect(urlsplit(self.url).hostname))
        self.cache = {}
        self._load()

    def _load(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 5 * 1024 * 1024:
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.cache = value if isinstance(value, dict) and value.get("identity") == self.identity else {}
            if self.cache.get("body"):
                if not isinstance(self.cache["body"], dict):
                    raise ValueError()
                public_iso(self.cache["checked_at"])
                public_iso(self.cache["body_received_at"])
            if self.cache.get("next_fetch"):
                public_iso(self.cache["next_fetch"])
        except (OSError, ValueError, TypeError, KeyError):
            self.cache = {}

    def _save(self):
        self.cache["identity"] = self.identity
        atomic_private_json(self.path, self.cache)

    def stats(self, now):
        current = self.cache.get("usage", {})
        if current.get("date") != now.astimezone(KST).date().isoformat():
            current = {}
        return {"body_bytes_today": current.get("bytes", 0), "requests_today": current.get("requests", 0),
                "next_check_at": self.cache.get("next_fetch"), "cached": self.cache.get("last_cached", False)}

    def _count(self, now, body_bytes):
        current = self.stats(now)
        self.cache["usage"] = {"date": now.date().isoformat(), "bytes": current["body_bytes_today"] + body_bytes,
                               "requests": current["requests_today"] + 1}

    def _parse(self, body, checked):
        if self.condition.provider == "open_meteo":
            return parse_open_meteo(body, checked)
        return parse_met_forecast(body, checked)

    def _return_cached(self):
        checked = public_iso(self.cache["checked_at"])
        points, issue = self._parse(self.cache["body"], public_iso(self.cache["body_received_at"]))
        self.cache["last_cached"] = True
        return points, issue, checked

    def _decompress(self, raw, encoding):
        if encoding not in ("gzip", "deflate"):
            if encoding not in ("", "identity"):
                raise ValueError("지원하지 않는 예보 압축 형식입니다.")
            return raw
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        result = decoder.decompress(raw, PUBLIC_WEATHER_LIMIT + 1)
        if len(result) > PUBLIC_WEATHER_LIMIT or decoder.unconsumed_tail or not decoder.eof:
            raise ValueError("예보 응답이 너무 크거나 압축 형식이 올바르지 않습니다.")
        return result

    def fetch(self, now=None):
        now = (now or datetime.now(KST)).astimezone(KST)
        if not any(self.condition.region) or not -90 <= self.condition.latitude <= 90 or not -180 <= self.condition.longitude <= 180:
            raise ValueError("예보를 조회할 지역을 선택하세요.")
        agent = met_user_agent(self.condition) if self.condition.provider == "met_no" else "EDCameraRecorder/4.1"
        with public_cache_lock(self.lock_path):
            self._load()
            try:
                next_fetch = public_iso(self.cache.get("next_fetch"))
            except (ValueError, TypeError):
                next_fetch = now
            if now < next_fetch:
                if self.cache.get("last_error"):
                    raise ValueError(self.cache["last_error"])
                if self.cache.get("body"):
                    return self._return_cached()
            headers = {"User-Agent": agent, "Accept": "application/json", "Accept-Encoding": "gzip, deflate"}
            for key, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
                if self.cache.get("body") and self.cache.get(key):
                    headers[header] = self.cache[key]
            request = Request(self.url, headers=headers)
            response_headers = {}
            received = 0
            counted = False
            try:
                try:
                    response = self.opener.open(request, timeout=15)
                except HTTPError as error:
                    if error.code != 304:
                        response_headers = error.headers or {}
                        status = error.code
                        error.close()
                        raise ValueError("예보 서비스 호출 한도로 대기합니다." if status == 429
                                         else "예보 서비스에 접속하지 못했습니다. HTTP {}".format(status)) from None
                    response = error
                with response:
                    response_headers = response.headers
                    status = response.getcode()
                    if status == 304:
                        if not self.cache.get("body"):
                            raise ValueError("재사용할 예보가 없습니다. 잠시 후 다시 조회하세요.")
                        body = self.cache["body"]
                        body_received = public_iso(self.cache["body_received_at"])
                    elif status in (200, 203):
                        raw = response.read(PUBLIC_WEATHER_LIMIT + 1)
                        received = len(raw)
                        if received > PUBLIC_WEATHER_LIMIT:
                            raise ValueError("예보 응답이 너무 큽니다.")
                        decoded = self._decompress(raw, response_headers.get("Content-Encoding", "").lower())
                        body = json.loads(decoded.decode("utf-8-sig"))
                        body_received = now
                    else:
                        raise ValueError("예보 서비스가 정상 응답하지 않았습니다.")
                points, issue = self._parse(body, body_received)
                self._count(now, received)
                counted = True
                self.cache.update(body=body, body_received_at=body_received.isoformat(), checked_at=now.isoformat(),
                                  next_fetch=public_cache_deadline(response_headers, now, self.condition.poll_minutes).isoformat(),
                                  last_modified=response_headers.get("Last-Modified", self.cache.get("last_modified", "")),
                                  etag=response_headers.get("ETag", self.cache.get("etag", "")), last_error="", last_cached=status == 304)
                self._save()
                if status == 203:
                    LOG.warning("Weather provider marks this API version deprecated; check for an update")
                return points, issue, now
            except (OSError, URLError, TimeoutError, ValueError, TypeError, KeyError, zlib.error) as exc:
                if not counted:
                    self._count(now, received)
                safe = str(exc) if type(exc) is ValueError else "예보를 받지 못했습니다. 인터넷 연결을 확인하세요."
                retry = now + timedelta(minutes=self.condition.poll_minutes)
                retry_header = response_headers.get("Retry-After", "")
                if str(retry_header).isdigit():
                    retry = max(retry, now + timedelta(seconds=int(retry_header)))
                else:
                    retry_date = http_date(retry_header)
                    if retry_date is not None:
                        retry = max(retry, retry_date)
                self.cache.update(next_fetch=retry.isoformat(), last_error=safe)
                self._save()
                raise ValueError(safe) from None
