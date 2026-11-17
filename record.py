#!/usr/bin/env python3
"""Independent RTSP scheduled recorder. Python 3.8+, FFmpeg; Linux for service mode."""

import argparse
import configparser
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from urllib.parse import urlsplit


KST = timezone(timedelta(hours=9), "Asia/Seoul")
LOG = logging.getLogger("camera-recorder")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
RUN_NAME = re.compile(r"run_\d{8}T\d{12}_[a-f0-9]{32}\Z")
PART_NAME = re.compile(r"part_\d{6,}\.(?:mp4|mkv)\Z")
LEGACY_FILE_PREFIX = r"client(?:[0-9]{1,16}|Unknown_[A-Za-z0-9][A-Za-z0-9_-]{0,31})_[\w.-]+_[A-Za-z-]+(?:_[\w.-]+)?"
HOST_LABEL = r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}"
HOST_FILE_PREFIX = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}_ip" + HOST_LABEL + r"(?:__[\w.-]+)?"
FILE_PREFIX = r"(?:" + HOST_FILE_PREFIX + "|" + LEGACY_FILE_PREFIX + ")"
MAX_FILENAME_PREFIX_BYTES = 200  # Reserve 55 bytes for timestamp, collision ID, and index.
PENDING_PART_NAME = re.compile(FILE_PREFIX + r"_pending_(?P<index>\d{6,})\.mp4\Z")
NAMED_PART_NAME = re.compile(FILE_PREFIX + r"_\d{8}_\d{6}_KST_(?:r[a-f0-9]{8}_)?(?P<index>\d{6,})\.mp4\Z")
CAMERA_DIRECTIONS = {"1": "North", "2": "East", "3": "South", "4": "West",
                     "5": "North-East", "6": "East-South", "7": "South-West", "8": "West-North"}
RUN_MARKER = ".scheduled-recording"
MARKER_TEXT = "goomi-camera-recorder-v1\n"
DEFAULT_SETUP_SEARCH_ROOT = Path("/home/dis")
SETUP_SEARCH_SKIP_DIRS = {"__pycache__", "node_modules", "venv", "env"}
DEFAULT_TIME_RANGES = ('06:30-07:30', '10:00-11:00', '12:00-13:00', '17:00-18:00', '21:00-22:00')


@dataclass(frozen=True)
class Camera:
    name: str
    url: str = field(repr=False)
    client_no: str = "Unknown"
    intersection: str = "UnknownIntersection"
    direction: str = "UnknownDirection"
    weather: str = ""


@dataclass(frozen=True)
class Schedule:
    hour: int
    minute: int
    duration: timedelta


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    cameras: tuple
    schedules: tuple
    ffmpeg: str = "ffmpeg"
    segment_seconds: float = 1800
    retention_days: int = 0
    min_free_gb: float = 5.0
    reconnect_seconds: float = 10.0
    stall_seconds: float = 30.0
    recording_dates: tuple = ()
    once_window: tuple = ()
    weather_condition: object = None
    recording_mode: str = "window"


def number(value, label, minimum, maximum, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum
            or (integer and not isinstance(value, int))):
        raise ValueError("{} must be {} in [{}, {}]".format(
            label, "an integer" if integer else "a number", minimum, maximum))
    return value


def resolve_path(value, base, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(label + " must be a nonempty path")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def minutes_to_seconds(value):
    minutes = number(value, "segment_minutes", 1 / 60, 1440)
    seconds = round(float(minutes) * 60, 6)
    return int(seconds) if seconds.is_integer() else seconds


def filename_component(value, fallback, max_bytes=90):
    value = unicodedata.normalize("NFC", str(value or "")).strip()
    value = "".join(char if char.isalnum() or char in "_.-" else "_" for char in value)
    value = re.sub(r"_+", "_", value).strip("_.-")
    result = ""
    for char in value:
        if len((result + char).encode("utf-8")) > max_bytes:
            break
        result += char
    return result.strip("_.-") or fallback


def camera_filename_prefix(camera):
    # Parse the hostname only: URLs may contain credentials and query tokens.
    # IPv6 punctuation and non-ASCII/DNS characters become a display label.
    host = urlsplit(camera.url).hostname or "unknown-host"
    host = re.sub(r"[^A-Za-z0-9.-]", "-", host)[:63].strip(".-") or "unknown-host"
    prefix = camera.name + "_ip" + host
    labels = []
    raw_client = str(camera.client_no).strip()
    if re.fullmatch(r"[0-9]{1,16}", raw_client):
        labels.append("client" + str(int(raw_client)))
    for value, unknown, maximum in ((camera.intersection, "UnknownIntersection", 90),
                                    (camera.direction, "UnknownDirection", 30)):
        if value and value != unknown:
            label = filename_component(value, "", maximum)
            if label:
                labels.append(label)
    if camera.weather:
        label = filename_component(camera.weather, "", 30)
        if label:
            labels.append(label)
    for index, label in enumerate(labels):
        separator = "__" if index == 0 else "_"
        remaining = MAX_FILENAME_PREFIX_BYTES - len((prefix + separator).encode("utf-8"))
        if remaining <= 0:
            break
        label = filename_component(label, "", remaining)
        if label:
            prefix += separator + label
    return prefix


def recording_filename_host(name):
    """Return the recorded host label from new filenames, never today's config.

    Old files do not store an IP. Ambiguous custom names are left unidentified.
    The label is exact for IPv4; IPv6/DNS may have been sanitized or shortened.
    """
    if not isinstance(name, str) or Path(name).name != name:
        return ""
    # Older custom camera/metadata names may themselves contain "_ip" and
    # "__". A legacy-compatible name cannot prove its historical camera host.
    if re.fullmatch(LEGACY_FILE_PREFIX + r"_(?:pending_|\d{8}_\d{6}_KST_(?:r[a-f0-9]{8}_)?)\d{6,}\.mp4", name):
        return ""
    match = re.fullmatch(r"(?P<prefix>" + HOST_FILE_PREFIX + r")_(?:pending_|\d{8}_\d{6}_KST_(?:r[a-f0-9]{8}_)?)\d{6,}\.mp4", name)
    if not match:
        return ""
    prefix = match.group("prefix")
    candidates = []
    for marker in re.finditer("_ip", prefix):
        if not NAME.fullmatch(prefix[:marker.start()]):
            continue
        host, separator, metadata = prefix[marker.end():].partition("__")
        if (re.fullmatch(HOST_LABEL, host)
                and (not separator or re.fullmatch(r"[\w.-]+", metadata))):
            candidates.append(host)
    return candidates[0] if len(candidates) == 1 else ""


def config_label(value, label):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError(label + " must be a string without control characters (empty is allowed)")
    return value.strip()


def parse_recording_dates(values):
    if not isinstance(values, list):
        raise ValueError("recording_dates must be a list of YYYY-MM-DD dates; [] means every day")
    result = []
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("recording_dates must contain real dates in YYYY-MM-DD format")
        try:
            result.append(date.fromisoformat(value))
        except ValueError:
            raise ValueError("recording_dates must contain real dates in YYYY-MM-DD format") from None
    if len(result) != len(set(result)):
        raise ValueError("recording_dates must not contain duplicate dates")
    return tuple(sorted(result))


def read_camera_metadata(ini, section, setup_path, name):
    """CAM1 may be CLIENTNO=29: read client29.ini, never guess client1.ini."""
    metadata = {"client_no": "Unknown", "intersection": "UnknownIntersection", "direction": "UnknownDirection"}
    missing = []
    raw_client = ini.get(section, "CLIENTNO", fallback="").strip()
    if not re.fullmatch(r"[0-9]{1,16}", raw_client):
        missing.append("CLIENTNO missing/invalid")
    else:
        metadata["client_no"] = str(int(raw_client))
        client_path = Path(setup_path).parent / ("client" + metadata["client_no"] + ".ini")
        client = configparser.ConfigParser(interpolation=None)
        try:
            with client_path.open(encoding="utf-8-sig") as handle:
                client.read_file(handle)
            intersection = client.get("INTERSECTION", "NAME", fallback="").strip()
            direction = client.get("INTERSECTION", "CAMDIR", fallback="").strip()
            if intersection:
                metadata["intersection"] = intersection
            else:
                missing.append("INTERSECTION.NAME missing")
            if direction in CAMERA_DIRECTIONS:
                metadata["direction"] = CAMERA_DIRECTIONS[direction]
            else:
                missing.append("INTERSECTION.CAMDIR missing/invalid")
        except (OSError, configparser.Error, UnicodeError):
            missing.append("client metadata file missing/unreadable")
    if missing:
        LOG.warning("%s: %s; missing filename fields are omitted", name, "; ".join(missing))
    return metadata


def output_pattern(camera, run_dir, recording_mode="split"):
    # FFmpeg's numbered filename expansion also interprets % in parent paths.
    prefix = str(Path(run_dir) / (camera_filename_prefix(camera) + "_pending_"))
    if recording_mode == "window":
        return prefix + "000000.mp4"
    return prefix.replace("%", "%%") + "%06d.mp4"


def part_index(path):
    name = Path(path).name
    if PART_NAME.fullmatch(name):
        return int(Path(name).stem[5:])
    match = PENDING_PART_NAME.fullmatch(name) or NAMED_PART_NAME.fullmatch(name)
    return int(match.group("index")) if match else None


def load_settings(path):
    path = Path(path).resolve()
    with path.open(encoding="utf-8-sig") as handle:
        data = json.load(handle)
    return settings_from_data(data, path.parent)


def settings_from_data(data, base, allow_unconfigured=False):
    base = Path(base).resolve()
    allowed = {"setup_ini", "output_dir", "cameras", "schedules", "ffmpeg",
               "segment_minutes", "segment_seconds", "retention_days", "min_free_gb",
               "reconnect_seconds", "stall_seconds", "intersection_name", "weather",
               "recording_dates", "time_ranges", "weather_condition", "recording_mode"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Unknown configuration fields or invalid JSON object")
    intersection_name = config_label(data.get("intersection_name", ""), "intersection_name")
    weather = config_label(data.get("weather", ""), "weather")
    recording_dates = parse_recording_dates(data.get("recording_dates", []))
    weather_condition = parse_weather_condition(data.get("weather_condition"), base)
    camera_data = data.get("cameras")
    ini, setup = None, None

    def camera_setup():
        nonlocal ini, setup
        if ini is None:
            value = data.get("setup_ini", "../../config/setup.ini")
            setup = find_setup_ini() if value == "auto" else resolve_path(value, base, "setup_ini")
            ini = configparser.ConfigParser(interpolation=None)
            with setup.open(encoding="utf-8-sig") as handle:
                ini.read_file(handle)
        return ini

    if camera_data == "auto":
        sections = enabled_camera_sections(camera_setup())
        camera_data = [{"name": section.lower(), "section": section} for section in sections]
    if not isinstance(camera_data, list) or (not camera_data and not allow_unconfigured):
        raise ValueError("cameras must be a nonempty list")
    cameras, names = [], set()
    for item in camera_data:
        if not isinstance(item, dict) or set(item) - {"name", "section", "url", "intersection_name", "weather"}:
            raise ValueError("Invalid camera fields")
        name = item.get("name", "")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name.lower() in names:
            raise ValueError("Camera names must be unique letters/digits/underscores/hyphens (1-32 chars)")
        names.add(name.lower())
        if ("section" in item) == ("url" in item):
            raise ValueError(name + ": provide exactly one of section or url")
        if "section" in item:
            section = item["section"]
            if not isinstance(section, str) or not re.fullmatch(r"CAM[1-9][0-9]*", section):
                raise ValueError(name + ": invalid setup.ini camera section")
            camera_setup()
            if not ini.has_option(section, "URL"):
                raise ValueError(name + ": camera section is missing URL")
            url = ini.get(section, "URL").strip()
        else:
            url = item["url"]
        # Never put credentials or the supplied URL in errors or logs.
        try:
            if not isinstance(url, str) or any(c.isspace() for c in url):
                raise ValueError()
            parsed = urlsplit(url)
            if parsed.scheme.lower() != "rtsp" or not parsed.hostname:
                raise ValueError()
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError()
        except ValueError:
            raise ValueError(name + ": URL must be a valid rtsp:// camera URL (local videos unsupported)") from None
        metadata = read_camera_metadata(ini, section, setup, name) if "section" in item else {}
        label = config_label(item.get("intersection_name", intersection_name), name + ".intersection_name")
        if label:
            metadata["intersection"] = label
        metadata["weather"] = config_label(item.get("weather", weather), name + ".weather")
        cameras.append(Camera(name, url, **metadata))
    output_value = data.get("output_dir", "../../runtime/scheduled_recordings")
    if output_value == "auto":
        if setup is None:
            raise ValueError("output_dir=auto requires a setup.ini camera; specify an output path for direct URLs")
        output = setup_project(setup) / "runtime/scheduled_recordings"
    else:
        output = resolve_path(output_value, base, "output_dir")
    if output == Path(output.anchor):
        raise ValueError("output_dir cannot be a filesystem root")
    schedules = []
    if "time_ranges" in data and "schedules" in data:
        raise ValueError("Use time_ranges or schedules, not both")
    schedule_data = parse_time_ranges(data["time_ranges"]) if "time_ranges" in data else data.get("schedules")
    if not isinstance(schedule_data, list) or not schedule_data:
        raise ValueError("schedules must be a nonempty list")
    for item in schedule_data:
        if not isinstance(item, dict) or set(item) != {"start", "duration_hours"}:
            raise ValueError("Each schedule needs start and duration_hours")
        start = item["start"]
        if not isinstance(start, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", start):
            raise ValueError("start must be HH:MM (00:00 through 23:59)")
        hours = number(item["duration_hours"], "duration_hours", 1 / 3600, 24)
        hour, minute = map(int, start.split(":"))
        schedules.append(Schedule(hour, minute, timedelta(hours=hours)))
    ffmpeg = data.get("ffmpeg", "ffmpeg")
    if not isinstance(ffmpeg, str) or not ffmpeg or any(ord(c) < 32 for c in ffmpeg):
        raise ValueError("ffmpeg must be an executable name or path")
    if "/" in ffmpeg or "\\" in ffmpeg:
        ffmpeg = str(resolve_path(ffmpeg, base, "ffmpeg"))
    if "segment_minutes" in data and "segment_seconds" in data:
        raise ValueError("Use segment_minutes or legacy segment_seconds, not both")
    if "segment_seconds" in data:
        segment_seconds = number(data["segment_seconds"], "segment_seconds", 1, 86400, True)
    else:
        segment_seconds = minutes_to_seconds(data.get("segment_minutes", 30))
    recording_mode = data.get("recording_mode",
                              "split" if "segment_minutes" in data or "segment_seconds" in data else "window")
    if recording_mode not in ("window", "split"):
        raise ValueError("recording_mode must be window or split")
    return Settings(
        output, tuple(cameras), tuple(schedules), ffmpeg,
        segment_seconds,
        number(data.get("retention_days", 0), "retention_days", 0, 36500, True),
        number(data.get("min_free_gb", 5), "min_free_gb", 0, 1000000),
        number(data.get("reconnect_seconds", 10), "reconnect_seconds", 1, 3600),
        number(data.get("stall_seconds", 30), "stall_seconds", 5, 3600),
        recording_dates,
        weather_condition=weather_condition, recording_mode=recording_mode,
    )


def scan_setup_ini(search_root):
    """Search a bounded home tree without following symlinks or hidden caches."""
    root = Path(search_root).expanduser().resolve()
    if not root.is_dir():
        return []
    candidates = []
    errors = []
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=errors.append):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs
                         if not name.startswith(".") and name not in SETUP_SEARCH_SKIP_DIRS
                         and not (parent / name).is_symlink())
        if "setup.ini" in files:
            candidate = parent / "setup.ini"
            if not candidate.is_symlink() and candidate.is_file():
                candidates.append(candidate.resolve())
    if errors:
        blocked = ", ".join(str(error.filename or root) for error in errors[:3])
        raise ValueError("setup.ini search incomplete (cannot read {}); use --setup-ini PATH".format(blocked))
    return sorted(set(candidates), key=str)


def find_setup_ini(script_path=None, search_root=None):
    """Prefer the local project, then discover one setup.ini under /home/dis."""
    script = Path(script_path or __file__).resolve()
    for parent in list(script.parents)[:4]:
        candidate = parent / "config" / "setup.ini"
        if candidate.is_file():
            return candidate.resolve()
    root = DEFAULT_SETUP_SEARCH_ROOT if search_root is None else Path(search_root)
    candidates = scan_setup_ini(root)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        paths = "\n".join("  " + str(path) for path in candidates[:10])
        more = "\n  ... and {} more".format(len(candidates) - 10) if len(candidates) > 10 else ""
        raise ValueError("Multiple setup.ini files found; choose one with --setup-ini PATH:\n" + paths + more)
    raise ValueError("setup.ini not found near this script or under {}; use --setup-ini PATH".format(root))


def parse_time_ranges(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("time-ranges must contain at least one HH:MM-HH:MM range")
    result = []
    clock = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(clock + "-" + clock, value):
            raise ValueError("time-ranges must use HH:MM-HH:MM (00:00 through 23:59)")
        start, end = value.split("-")
        start_h, start_m = map(int, start.split(":"))
        end_h, end_m = map(int, end.split(":"))
        minutes = (end_h * 60 + end_m - start_h * 60 - start_m) % 1440
        if not minutes:
            raise ValueError("time-ranges start and end must differ; use --duration-hours 24 for all-day recording")
        result.append({"start": start, "duration_hours": minutes / 60})
    return result


def setup_project(setup):
    return setup.parent.parent if setup.parent.name.lower() == "config" else setup.parent


def enabled_camera_sections(ini):
    sections = sorted((section for section in ini.sections()
                       if re.fullmatch(r"CAM[1-9][0-9]*", section)
                       and ini.getboolean(section, "INUSE", fallback=False)),
                      key=lambda section: int(section[3:]))
    if not sections:
        raise ValueError("No enabled cameras in setup.ini; select cameras explicitly or set INUSE=True")
    return sections


def standalone_settings(args):
    setup = Path(args.setup_ini).expanduser().resolve() if args.setup_ini else find_setup_ini()
    ini = configparser.ConfigParser(interpolation=None)
    with setup.open(encoding="utf-8-sig") as handle:
        ini.read_file(handle)
    if args.cameras is not None:
        sections = [section.upper() for section in args.cameras]
    else:
        sections = enabled_camera_sections(ini)
    if not sections:
        raise ValueError("No enabled cameras in setup.ini; use --cameras CAM1 CAM2 or set INUSE=True")
    project = setup_project(setup)
    output = Path(args.output_dir).expanduser().resolve() if args.output_dir else project / "runtime/scheduled_recordings"
    shared_duration = args.start_times is not None or args.duration_hours is not None
    if args.time_ranges is not None and shared_duration:
        raise ValueError("--time-ranges cannot be combined with --start-times or --duration-hours")
    if shared_duration:
        starts = args.start_times if args.start_times is not None else [value.split("-")[0] for value in DEFAULT_TIME_RANGES]
        hours = args.duration_hours if args.duration_hours is not None else 2
        schedules = [{"start": start, "duration_hours": hours} for start in starts]
    else:
        schedules = parse_time_ranges(args.time_ranges if args.time_ranges is not None else DEFAULT_TIME_RANGES)
    data = {
        "setup_ini": str(setup),
        "output_dir": str(output),
        "cameras": [{"name": section.lower(), "section": section} for section in sections],
        "schedules": schedules,
        "segment_minutes": args.segment_minutes if args.segment_minutes is not None else 30,
        "recording_mode": getattr(args, "recording_mode", None) or ("split" if args.segment_minutes is not None else "window"),
        "retention_days": args.retention_days if args.retention_days is not None else 0,
        "min_free_gb": args.min_free_gb if args.min_free_gb is not None else 5,
        "ffmpeg": args.ffmpeg if args.ffmpeg is not None else "ffmpeg",
        "reconnect_seconds": getattr(args, "reconnect_seconds", None) if getattr(args, "reconnect_seconds", None) is not None else 10,
        "stall_seconds": getattr(args, "stall_seconds", None) if getattr(args, "stall_seconds", None) is not None else 30,
    }
    return settings_from_data(data, Path.cwd()), setup


def service_arguments(args, settings, setup_path):
    """Persist runtime options in the unit, without a separately copied JSON."""
    if args.config:
        result = ["--config", str(Path(args.config).expanduser().resolve())]
        if args.segment_minutes is not None:
            result += ["--segment-minutes", format(args.segment_minutes, ".12g")]
        if getattr(args, "recording_mode", None) is not None:
            result += ["--recording-mode", args.recording_mode]
        return result
    result = ["--setup-ini", str(setup_path), "--output-dir", str(settings.output_dir)]
    if args.start_times is not None or args.duration_hours is not None:
        # Preserve fractional-hour shared-duration options (including seconds).
        result += ["--start-times"]
        result += ["{:02d}:{:02d}".format(schedule.hour, schedule.minute) for schedule in settings.schedules]
        result += ["--duration-hours", format(settings.schedules[0].duration.total_seconds() / 3600, ".12g")]
    else:
        # Each default/custom range has its own end time: never apply the first
        # range's duration to all starts when saving the service arguments.
        result += ["--time-ranges"]
        for schedule in settings.schedules:
            end_minutes = (schedule.hour * 60 + schedule.minute + int(schedule.duration.total_seconds() / 60)) % 1440
            result.append("{:02d}:{:02d}-{:02d}:{:02d}".format(
                schedule.hour, schedule.minute, end_minutes // 60, end_minutes % 60))
    result += ["--recording-mode", settings.recording_mode,
               "--segment-minutes", format(settings.segment_seconds / 60, ".12g"),
               "--retention-days", str(settings.retention_days),
               "--min-free-gb", format(settings.min_free_gb, ".12g"),
               "--ffmpeg", settings.ffmpeg]
    # Omit an implicit list so INUSE changes are read on the next service start.
    if args.cameras is not None:
        result += ["--cameras"] + [section.upper() for section in args.cameras]
    return result


def active_window(now, schedules, recording_dates=(), day_allowed=None, merge_adjacent=True):
    """Merge windows in KST; optional dates select the day each window starts."""
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    now = now.astimezone(KST)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if recording_dates:
        # Include all selected days so adjacent all-day windows have their real
        # final deadline, rather than an artificial tomorrow-night boundary.
        starts = (datetime(day.year, day.month, day.day, tzinfo=KST) for day in recording_dates)
    else:
        starts = (midnight + timedelta(days=offset) for offset in (-1, 0, 1))
    windows = []
    for day in starts:
        if day_allowed is not None and not day_allowed(day.date()):
            continue
        for schedule in schedules:
            start = day + timedelta(hours=schedule.hour, minutes=schedule.minute)
            windows.append((start, start + schedule.duration))
    merged = []
    for start, end in sorted(windows):
        if merged and (start < merged[-1][1] or (merge_adjacent and start == merged[-1][1])):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return next((window for window in merged if window[0] <= now < window[1]), None)


def select_once_camera(settings, name):
    """Select one saved camera for this run without rewriting its configuration."""
    if name is None:
        return settings
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("한 번 녹화할 카메라를 저장된 목록에서 선택하세요.")
    matches = tuple(camera for camera in settings.cameras if camera.name == name)
    if len(matches) != 1:
        raise ValueError("선택한 카메라가 저장된 목록에 없습니다. 목록을 새로고침하세요.")
    return replace(settings, cameras=matches)


def make_once_window(minutes, start_at=None, now=None):
    seconds = round(number(minutes, "record-minutes", 1 / 60, 1440) * 60, 6)
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    now = now.astimezone(KST)
    start = now
    if start_at is not None:
        if not isinstance(start_at, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", start_at):
            raise ValueError("--start-at must be HH:MM in Korean time, for today")
        hour, minute = map(int, start_at.split(":"))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start < now:
            raise ValueError("--start-at has already passed today; choose a later time or omit it to start now")
    return start, start + timedelta(seconds=seconds)


def settings_window(now, settings, day_allowed=None):
    if settings.once_window:
        start, end = settings.once_window
        return (start, end) if start <= now < end else None
    return active_window(now, settings.schedules, settings.recording_dates, day_allowed,
                         merge_adjacent=settings.recording_mode == "split")


def log_once_window(window):
    start, end = window
    LOG.info("One-time recording window: %s to %s KST; configured schedule is ignored; exits after this window",
             start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))


def continuous_coverage(schedules):
    """Whether the union of daily schedules covers the entire 24-hour clock."""
    spans = []
    for schedule in schedules:
        start = schedule.hour * 3600 + schedule.minute * 60
        end = start + schedule.duration.total_seconds()
        spans.append((start, min(end, 86400)))
        if end > 86400:
            spans.append((0, end - 86400))
    covered = 0
    for start, end in sorted(spans):
        if start > covered:
            return False
        covered = max(covered, end)
    return covered >= 86400


def check_output_access(settings):
    ancestor = settings.output_dir
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(str(ancestor), os.W_OK | os.X_OK):
        raise ValueError("output_dir or its existing parent is not writable by this account")


def check_ffmpeg(settings):
    executable = shutil.which(settings.ffmpeg)
    if not executable:
        raise ValueError("FFmpeg not found; install it with: sudo apt install ffmpeg")
    # Older FFmpeg uses stimeout; newer versions call this input option timeout.
    result = subprocess.run([executable, "-hide_banner", "-h", "demuxer=rtsp"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=10, text=True, errors="replace")
    if result.returncode:
        raise ValueError("FFmpeg RTSP capability check failed")
    # In FFmpeg 4.x, -timeout may ALSO exist but means server listen timeout
    # in seconds. Prefer -stimeout on those builds to stay in RTSP client mode.
    if re.search(r"^\s+-stimeout\s", result.stdout, re.MULTILINE):
        timeout_option = "-stimeout"
    elif re.search(r"^\s+-timeout\s", result.stdout, re.MULTILINE):
        timeout_option = "-timeout"
    else:
        raise ValueError("FFmpeg does not expose an RTSP socket timeout option")
    return executable, timeout_option


def build_command(settings, camera, target, remaining, executable, timeout_option):
    weather_enabled = settings.weather_condition is not None and settings.weather_condition.enabled
    unlimited = (settings.recording_mode == "split" and not settings.once_window
                 and not settings.recording_dates and not weather_enabled and continuous_coverage(settings.schedules))
    duration_args = [] if unlimited else ["-t", "{:.3f}".format(remaining)]
    # Write MP4 initialization metadata first and fragment at keyframes so a
    # process/network interruption does not leave a traditional unfinalized MP4.
    flags = "+frag_keyframe+empty_moov+default_base_moof"
    if settings.recording_mode == "window":
        muxer_args = ["-f", "mp4", "-movflags", flags]
    else:
        muxer_args = ["-f", "segment", "-segment_format", "mp4",
                      "-segment_format_options", "movflags=" + flags,
                      "-segment_time", str(settings.segment_seconds), "-reset_timestamps", "1"]
    return [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-rtsp_transport", "tcp", timeout_option,
            str(int(settings.stall_seconds * 1000000)), "-i", camera.url,
            "-map", "0:v:0", "-an", "-c:v", "copy"] + duration_args + muxer_args + [
            "-progress", "pipe:1", "-nostats", str(target)]


class Storage:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.output_dir
        self.lock = None

    def acquire(self):
        # Linux lock shared by every invocation writing this output root.
        import fcntl
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.root / ".recorder.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            self.lock = None
            raise ValueError("Another recorder is already using output_dir") from None

    def close(self):
        if self.lock:
            self.lock.close()
            self.lock = None

    def create_run(self, camera, now):
        now = now.astimezone(KST)
        # Refuse links within the managed tree; cleanup must never traverse them.
        parent = self.root
        for name in (camera.name, now.strftime("%Y-%m-%d")):
            parent = parent / name
            if parent.is_symlink():
                raise ValueError("Symlinks are not allowed inside output_dir")
            parent.mkdir(exist_ok=True)
        run = parent / ("run_" + now.strftime("%Y%m%dT%H%M%S%f") + "_" + uuid.uuid4().hex)
        run.mkdir()
        (run / RUN_MARKER).write_bytes(MARKER_TEXT.encode("ascii"))
        return run

    def has_space(self):
        return shutil.disk_usage(self.root).free >= self.settings.min_free_gb * 1024 ** 3

    def cleanup(self, now, active_runs=()):
        """Opt-in retention: only marked runs, excluding the current open segment."""
        if not self.settings.retention_days:
            return 0
        cutoff = now.timestamp() - self.settings.retention_days * 86400
        active_runs = set(active_runs)
        removed = 0
        for camera in self.settings.cameras:
            cam_dir = self.root / camera.name
            if cam_dir.is_symlink() or not cam_dir.is_dir():
                continue
            for day in cam_dir.iterdir():
                if day.is_symlink() or not day.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name):
                    continue
                for run in day.iterdir():
                    if (run.is_symlink() or not run.is_dir()
                            or not RUN_NAME.fullmatch(run.name)):
                        continue
                    marker = run / RUN_MARKER
                    if (marker.is_symlink() or not marker.is_file() or marker.stat().st_size != len(MARKER_TEXT)
                            or marker.read_text(encoding="ascii", errors="replace") != MARKER_TEXT):
                        continue
                    parts = [part for part in run.iterdir() if part_index(part) is not None
                             and not part.is_symlink() and part.is_file()]
                    # Segment muxer closes each numbered file before opening the
                    # next. Always protect the highest index while FFmpeg runs,
                    # including during startup/stalls. Older parts are closed.
                    # Keep the newest file per format in an active run. Older
                    # releases wrote MKV; their owned files retain the same policy.
                    open_parts = set()
                    if run in active_runs:
                        for extension in (".mp4", ".mkv"):
                            candidates = [part for part in parts if part.suffix == extension]
                            if candidates:
                                latest = max(part_index(part) for part in candidates)
                                open_parts.update(part for part in candidates if part_index(part) == latest)
                    for part in parts:
                        if part not in open_parts and part.stat().st_mtime < cutoff:
                            part.unlink()
                            removed += 1
                    if (run not in active_runs and marker.stat().st_mtime < cutoff
                            and set(run.iterdir()) == {marker}):
                        marker.unlink()
                        run.rmdir()
                if not any(day.iterdir()):
                    day.rmdir()
        if removed:
            LOG.info("Retention removed %d expired segments", removed)
        return removed


class SegmentNamer:
    """Label newly-created files without restarting the FFmpeg input stream.

    Time is the ED wall clock when creation is observed, not source-frame PTS.
    On Linux an open file keeps the same inode after its name is changed.
    """
    def __init__(self, run_dir, prefix):
        self.run_dir = Path(run_dir)
        self.prefix = prefix
        if not re.fullmatch(FILE_PREFIX, prefix):
            raise ValueError("Invalid recording filename prefix")
        self.next_index = 0
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.warned = False
        self.pending_target = None

    def rename_available(self, now=None):
        if now is not None and now.tzinfo is None:
            raise ValueError("Filename timestamp must have a timezone")
        renamed = []
        with self.lock:
            while True:
                index = self.next_index
                source = self.run_dir / (self.prefix + "_pending_{:06d}.mp4".format(index))
                if source.is_symlink():
                    raise ValueError("Refusing to rename a symlink recording")
                if not source.is_file():
                    break
                if self.pending_target is None:
                    stamp = (now or datetime.now(KST)).astimezone(KST).strftime("%Y%m%d_%H%M%S")
                    self.pending_target = self.run_dir / (self.prefix + "_" + stamp + "_KST_{:06d}.mp4".format(index))
                target = self.pending_target
                for _ in range(10):
                    try:
                        # Exclusive publication never replaces a prior recording.
                        ini_publish_file(source, target)
                        break
                    except FileExistsError:
                        if not target.is_symlink() and os.path.samefile(source, target):
                            break  # A prior publish succeeded but unlink was interrupted.
                        name_base = target.name.rsplit("_KST_", 1)[0] + "_KST_"
                        target = self.run_dir / (name_base + "r" + uuid.uuid4().hex[:8] + "_{:06d}.mp4".format(index))
                        self.pending_target = target
                else:
                    raise ValueError("Could not allocate a unique recording filename")
                if source.exists():
                    source.unlink()
                self.next_index += 1
                self.pending_target = None
                renamed.append(target)
        return renamed

    def _try_rename(self):
        try:
            self.rename_available()
            self.warned = False
        except (OSError, ValueError):
            if not self.warned:
                LOG.warning("Video filename update failed; original pending file is preserved")
                self.warned = True

    def _loop(self):
        while not self.stop_event.is_set():
            self._try_rename()
            self.stop_event.wait(0.2)
        self._try_rename()

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                LOG.warning("Video filename update is still waiting for storage")
        else:
            self._try_rename()


class Recording:
    def __init__(self, command, run_dir):
        self.run_dir = run_dir
        self.last_progress = time.monotonic()
        self.last_timestamp = None
        self.namer = None
        # FFmpeg errors can echo passwords in RTSP URLs. Keep raw stderr private
        # (discarded); surface camera name, exit code, stalls and retries instead.
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
        self.reader = threading.Thread(target=self._read_progress, daemon=True)
        self.reader.start()
        target_name = Path(command[-1]).name
        for suffix in ("_pending_%06d.mp4", "_pending_000000.mp4"):
            if target_name.endswith(suffix):
                self.namer = SegmentNamer(run_dir, target_name[:-len(suffix)])
                self.namer.start()
                break

    def _read_progress(self):
        try:
            for raw in self.process.stdout:
                key, separator, value = raw.strip().partition(b"=")
                if separator and key in (b"out_time_us", b"out_time_ms"):
                    try:
                        timestamp = int(value)
                    except ValueError:
                        continue
                    if timestamp != self.last_timestamp:
                        self.last_timestamp = timestamp
                        self.last_progress = time.monotonic()
        except (OSError, ValueError):
            pass

    def poll(self):
        return self.process.poll()

    def finish(self):
        if self.namer:
            self.namer.close()
        self.reader.join(timeout=1)
        # Only close once the reader has exited; pipe close can otherwise block.
        if not self.reader.is_alive():
            self.process.stdout.close()


def stop_recordings(recordings):
    """Signal all cameras first, then bound total stop time even if RTSP is hung."""
    recordings = list(recordings)
    for rec in recordings:
        if rec.poll() is None:
            try:
                rec.process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    for rec in recordings:
        try:
            rec.process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                rec.process.kill()
            except ProcessLookupError:
                pass
    failed = False
    deadline = time.monotonic() + 3
    for rec in recordings:
        try:
            rec.process.wait(timeout=max(0, deadline - time.monotonic()))
            rec.finish()
        except subprocess.TimeoutExpired:
            failed = True
    if failed:
        # Never let TimeoutExpired format process.args (which contains secrets).
        raise RuntimeError("FFmpeg could not be stopped; cannot safely continue") from None


class Recorder:
    def __init__(self, settings, executable, timeout_option, storage, factory=Recording, weather_gate=None):
        self.settings = settings
        self.executable = executable
        self.timeout_option = timeout_option
        self.storage = storage
        self.factory = factory
        self.weather_gate = weather_gate
        self.jobs = {}
        self.job_windows = {}
        self.retry_at = {}
        self.next_storage_check = 0
        self.next_cleanup = 0
        self.space_ok = True

    def tick(self, now=None, monotonic=None):
        now = now or datetime.now(KST)
        mono = time.monotonic() if monotonic is None else monotonic
        day_allowed = (lambda day: self.weather_gate.allows_date(day, now)) if self.weather_gate is not None else None
        window = settings_window(now, self.settings, day_allowed)
        if mono >= self.next_cleanup:
            try:
                self.storage.cleanup(now, [job.run_dir for job in self.jobs.values()])
            except OSError:
                LOG.warning("Retention cleanup failed; check storage permissions")
            self.next_cleanup = mono + 3600
        if mono >= self.next_storage_check:
            space_ok = self.storage.has_space()
            if not space_ok and self.space_ok:
                LOG.error("Free storage below %.2f GiB; recording paused", self.settings.min_free_gb)
            elif space_ok and not self.space_ok:
                LOG.info("Free storage recovered; recording may resume")
            self.space_ok = space_ok
            self.next_storage_check = mono + 5

        stopping = []
        for name, job in list(self.jobs.items()):
            code = job.poll()
            if not window or not self.space_ok:
                reason = "outside schedule" if not window else "low disk space"
            elif (self.settings.recording_mode == "window" and
                  name in self.job_windows and now >= self.job_windows[name][1]):
                reason = "recording window ended"
            elif code is not None:
                reason = "FFmpeg exited (code {})".format(code)
            elif mono - job.last_progress >= self.settings.stall_seconds:
                reason = "no video progress"
            else:
                continue
            LOG.info("%s: stopped: %s", name, reason)
            stopping.append(self.jobs.pop(name))
            self.job_windows.pop(name, None)
            self.retry_at[name] = mono if reason == "recording window ended" else mono + self.settings.reconnect_seconds
        stop_recordings(stopping)

        if not window or not self.space_ok:
            return
        # Re-read clocks after potentially slow process termination. A reconnect
        # must never start after the scheduled window has ended.
        if monotonic is None:
            now, mono = datetime.now(KST), time.monotonic()
            window = settings_window(now, self.settings, day_allowed)
            if not window:
                return
        for camera in self.settings.cameras:
            if camera.name in self.jobs or mono < self.retry_at.get(camera.name, 0):
                continue
            remaining = (window[1] - now).total_seconds()
            if remaining <= 0:
                continue
            try:
                if self.weather_gate is not None:
                    camera = replace(camera, weather=self.weather_gate.label_for_day(window[0].date()))
                run_dir = self.storage.create_run(camera, now)
                command = build_command(self.settings, camera, output_pattern(camera, run_dir, self.settings.recording_mode),
                                        remaining, self.executable, self.timeout_option)
                self.jobs[camera.name] = self.factory(command, run_dir)
                self.job_windows[camera.name] = window
                LOG.info("%s: recording until %s KST, directory=%s", camera.name,
                         window[1].strftime("%Y-%m-%d %H:%M:%S"), run_dir)
            except (OSError, ValueError):
                LOG.error("%s: cannot start recording; check storage and FFmpeg", camera.name)
                self.retry_at[camera.name] = mono + self.settings.reconnect_seconds

    def close(self):
        stop_recordings(self.jobs.values())
        self.jobs.clear()
        self.job_windows.clear()


SERVICE_NAME = "goomi-camera-recorder.service"
UNIT_PATH = Path("/etc/systemd/system") / SERVICE_NAME
SERVICE_MARKER = "# Managed by Goomi camera recorder"
SERVICE_MARKERS = {SERVICE_MARKER, "# Managed by tools/scheduled_recording/install.py"}
SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def service_unit_quote(value, command=False):
    """Quote systemd word-list values (ExecStart and Environment)."""
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Service arguments must not contain control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def service_working_directory(value):
    """WorkingDirectory is a raw scalar path, without unquoting/C-unescaping."""
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Service paths must not contain control characters")
    value = value.replace("%", "%%")
    # Preserve final whitespace/backslashes against INI trimming/continuation.
    return value + "/." if value.endswith((" ", "\\")) else value


def resolve_service_user(requested=None):
    import pwd

    name = requested or os.environ.get("SUDO_USER")
    if not name:
        if os.geteuid() == 0:
            raise ValueError("When running directly as root, specify --user USER")
        name = pwd.getpwuid(os.geteuid()).pw_name
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        raise ValueError("Service user does not exist: " + name) from None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*\$?", account.pw_name):
        raise ValueError("Unsupported systemd service user name")
    return account.pw_name


def service_python_executable():
    executable = Path("/usr/bin/python3")
    return executable if executable.is_file() else Path(sys.executable).resolve()


def render_service_unit(service_args, user, script_path=None, working_directory=None,
                        python_path=None):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*\$?", user):
        raise ValueError("Unsupported systemd service user name")
    script_path = script_path if script_path is not None else Path(__file__).resolve()
    working_directory = working_directory if working_directory is not None else script_path.parent
    python_path = python_path if python_path is not None else service_python_executable()
    command = " ".join(service_unit_quote(value, command=True)
                       for value in (python_path, script_path, *service_args))
    return "\n".join([
        SERVICE_MARKER,
        "[Unit]",
        "Description=Goomi standalone camera scheduled recorder",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "User=" + user,
        "WorkingDirectory=" + service_working_directory(working_directory),
        "Environment=" + service_unit_quote("PATH=" + SERVICE_PATH),
        "Environment=PYTHONUNBUFFERED=1",
        "ExecStart=" + command,
        "Restart=on-failure",
        "RestartSec=10",
        "KillMode=mixed",
        "TimeoutStopSec=30",
        "UMask=0077",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def service_systemctl(*args, capture=False):
    return subprocess.run(["systemctl", *args], check=True, text=True, timeout=60,
                          stdout=subprocess.PIPE if capture else None)


def service_ensure_managed(unit_path=None):
    unit_path = UNIT_PATH if unit_path is None else unit_path
    if unit_path.is_symlink():
        raise ValueError("Refusing to change a symlink or masked unit: " + str(unit_path))
    if unit_path.exists():
        if not unit_path.is_file():
            raise ValueError("Service unit is not a regular file: " + str(unit_path))
        with unit_path.open(encoding="utf-8") as handle:
            marker = handle.readline().rstrip("\r\n")
        if marker not in SERVICE_MARKERS:
            raise ValueError("Refusing to change an unmanaged unit: " + str(unit_path))


def service_ensure_no_foreign_unit(unit_path=None):
    unit_path = UNIT_PATH if unit_path is None else unit_path
    service_ensure_managed(unit_path)
    result = service_systemctl("show", "--property=FragmentPath", "--property=DropInPaths",
                               SERVICE_NAME, capture=True)
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    fragment = properties.get("FragmentPath", "")
    if fragment and Path(fragment) != unit_path:
        raise ValueError("A different unit already owns " + SERVICE_NAME + ": " + fragment)
    if properties.get("DropInPaths", ""):
        raise ValueError("Service has external drop-ins; review/remove them before using this installer")


def service_preflight(service_args, user, script_path, python_path):
    runuser = shutil.which("runuser")
    env = shutil.which("env")
    if not runuser or not env:
        raise ValueError("runuser and env are required (Ubuntu: util-linux and coreutils)")
    subprocess.run([runuser, "-u", user, "--", env, "PATH=" + SERVICE_PATH,
                    "PYTHONDONTWRITEBYTECODE=1", str(python_path), str(script_path),
                    *service_args, "--check"],
                   cwd=str(script_path.parent), check=True, timeout=30)


def service_write_unit(content, unit_path=None):
    import tempfile

    unit_path = UNIT_PATH if unit_path is None else unit_path
    service_ensure_managed(unit_path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=str(unit_path.parent), prefix="." + SERVICE_NAME,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(unit_path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def install_service(service_args, user):
    if os.geteuid() != 0:
        raise ValueError("Use sudo to install the system service")
    service_args = tuple(str(arg) for arg in service_args)
    script_path = Path(__file__).resolve()
    python_path = service_python_executable()
    content = render_service_unit(service_args, user, script_path=script_path,
                                  python_path=python_path)
    # Check the copied script with the exact runtime arguments and service user
    # before any unit write, enable, stop or start operation.
    service_preflight(service_args, user, script_path, python_path)
    service_ensure_no_foreign_unit()
    service_write_unit(content)
    service_systemctl("daemon-reload")
    service_systemctl("enable", SERVICE_NAME)
    service_systemctl("restart", SERVICE_NAME)
    print("Installed and started " + SERVICE_NAME)
    print("Logs: journalctl -u " + SERVICE_NAME + " -f")


def uninstall_service():
    if os.geteuid() != 0:
        raise ValueError("Use sudo to uninstall the system service")
    service_ensure_no_foreign_unit()
    if not UNIT_PATH.exists():
        print(SERVICE_NAME + " is not installed by this tool")
        return
    service_systemctl("disable", "--now", SERVICE_NAME)
    service_ensure_managed()
    UNIT_PATH.unlink()
    service_systemctl("daemon-reload")
    print("Removed " + SERVICE_NAME + "; configuration and recordings were kept")


INI_FIELDS = ("setup_ini", "cameras", "time_ranges", "start_times", "duration_hours",
              "segment_minutes", "recording_mode", "output_dir", "retention_days", "min_free_gb", "ffmpeg",
              "reconnect_seconds", "stall_seconds")
INI_SCHEMA = {
    "CAMERAS": {"setup_ini", "cameras"},
    "RECORDING": {"time_ranges", "start_times", "duration_hours", "segment_minutes", "recording_mode",
                  "output_dir", "retention_days", "min_free_gb", "ffmpeg",
                  "reconnect_seconds", "stall_seconds"},
}


def default_ini_path():
    return Path(__file__).resolve().with_name("recording.ini")


def ini_default_options():
    return argparse.Namespace(
        setup_ini=None, cameras=None, time_ranges=list(DEFAULT_TIME_RANGES),
        start_times=None, duration_hours=None, segment_minutes=30, recording_mode="window",
        output_dir=None, retention_days=0, min_free_gb=5, ffmpeg="ffmpeg",
        reconnect_seconds=10, stall_seconds=30,
    )


def ini_list(value, label):
    values = re.split(r"[,\s]+", value.strip()) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or not values or not all(values):
        raise ValueError(label + " must contain at least one value")
    return list(values)


def validate_ini_options(options):
    if options.cameras is not None:
        options.cameras = [value.upper() for value in ini_list(options.cameras, "cameras")]
        if (any(not re.fullmatch(r"CAM[1-9][0-9]*", value) for value in options.cameras)
                or len(set(options.cameras)) != len(options.cameras)):
            raise ValueError("cameras must contain unique CAM1, CAM2, ... section names")
    shared = options.start_times is not None or options.duration_hours is not None
    if options.time_ranges is not None:
        if shared:
            raise ValueError("Use time_ranges or start_times + duration_hours, not both")
        options.time_ranges = ini_list(options.time_ranges, "time_ranges")
        parse_time_ranges(options.time_ranges)
    else:
        if options.start_times is None or options.duration_hours is None:
            raise ValueError("Provide both start_times and duration_hours when time_ranges is absent")
        options.start_times = ini_list(options.start_times, "start_times")
        if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
               for value in options.start_times):
            raise ValueError("start_times must contain HH:MM values (00:00 through 23:59)")
        number(options.duration_hours, "duration_hours", 1 / 3600, 24)
    if options.recording_mode not in ("window", "split"):
        raise ValueError("recording_mode must be window or split")
    minutes_to_seconds(options.segment_minutes)
    number(options.retention_days, "retention_days", 0, 36500, True)
    number(options.min_free_gb, "min_free_gb", 0, 1000000)
    number(options.reconnect_seconds, "reconnect_seconds", 1, 3600)
    number(options.stall_seconds, "stall_seconds", 5, 3600)
    if options.output_dir is not None:
        output = Path(options.output_dir)
        if output == Path(output.anchor):
            raise ValueError("output_dir cannot be a filesystem root")
    if (not isinstance(options.ffmpeg, str) or not options.ffmpeg
            or options.ffmpeg != options.ffmpeg.strip()
            or any(ord(char) < 32 for char in options.ffmpeg)):
        raise ValueError("ffmpeg must be an executable name or path")
    return options


def ini_options(path):
    path = Path(path).expanduser().resolve()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except configparser.Error:
        raise ValueError("Invalid recording.ini structure") from None
    if parser.defaults():
        raise ValueError("recording.ini must not contain DEFAULT values")
    if set(parser.sections()) != set(INI_SCHEMA):
        raise ValueError("recording.ini requires exactly [CAMERAS] and [RECORDING] sections")
    for section, allowed in INI_SCHEMA.items():
        if set(parser[section]) - allowed:
            raise ValueError("Unknown recording.ini option in [" + section + "]")
    result = ini_default_options()
    camera = parser["CAMERAS"]
    recording = parser["RECORDING"]
    result.recording_mode = recording.get("recording_mode", "split" if "segment_minutes" in recording else "window").strip()
    for key in ("setup_ini", "output_dir"):
        values = camera if key == "setup_ini" else recording
        value = values.get(key, "auto").strip()
        setattr(result, key, None if value.lower() == "auto" else resolve_path(value, path.parent, key))
    cameras = camera.get("cameras", "auto").strip()
    result.cameras = None if cameras.lower() == "auto" else ini_list(cameras, "cameras")
    shared = "start_times" in recording or "duration_hours" in recording
    if "time_ranges" in recording and shared:
        raise ValueError("Use time_ranges or start_times + duration_hours, not both")
    if shared:
        if "start_times" not in recording or "duration_hours" not in recording:
            raise ValueError("Provide both start_times and duration_hours when time_ranges is absent")
        result.time_ranges = None
        result.start_times = ini_list(recording["start_times"], "start_times")
        result.duration_hours = ini_number(recording["duration_hours"], "duration_hours")
    elif "time_ranges" in recording:
        result.time_ranges = ini_list(recording["time_ranges"], "time_ranges")
    for key in ("segment_minutes", "min_free_gb", "reconnect_seconds", "stall_seconds"):
        if key in recording:
            setattr(result, key, ini_number(recording[key], key))
    if "retention_days" in recording:
        result.retention_days = ini_number(recording["retention_days"], "retention_days", integer=True)
    result.ffmpeg = recording.get("ffmpeg", "ffmpeg").strip()
    if "/" in result.ffmpeg or "\\" in result.ffmpeg:
        result.ffmpeg = str(resolve_path(result.ffmpeg, path.parent, "ffmpeg"))
    return validate_ini_options(result)


def ini_number(value, label, integer=False):
    try:
        return int(value) if integer else float(value)
    except ValueError:
        raise ValueError("Invalid recording.ini numeric option: " + label) from None


def merge_ini_overrides(base, overrides=None):
    result = argparse.Namespace(**{field: getattr(base, field) for field in INI_FIELDS})
    if overrides is None:
        return validate_ini_options(result)
    ranges = getattr(overrides, "time_ranges", None)
    starts = getattr(overrides, "start_times", None)
    duration = getattr(overrides, "duration_hours", None)
    if ranges is not None:
        if starts is not None or duration is not None:
            raise ValueError("--time-ranges cannot be combined with --start-times or --duration-hours")
        result.time_ranges, result.start_times, result.duration_hours = ranges, None, None
    elif starts is not None or duration is not None:
        if result.time_ranges is not None:
            result.start_times = [value.split("-")[0] for value in result.time_ranges]
            result.duration_hours = 2
        result.time_ranges = None
        if starts is not None:
            result.start_times = starts
        if duration is not None:
            result.duration_hours = duration
    if (getattr(overrides, "segment_minutes", None) is not None
            and getattr(overrides, "recording_mode", None) is None):
        result.recording_mode = "split"
    for field in INI_FIELDS:
        if field in ("time_ranges", "start_times", "duration_hours"):
            continue
        value = getattr(overrides, field, None)
        if value is None:
            continue
        if field in ("setup_ini", "output_dir"):
            value = resolve_path(str(value), Path.cwd(), field)
        elif field == "ffmpeg" and ("/" in value or "\\" in value):
            value = str(resolve_path(value, Path.cwd(), field))
        setattr(result, field, value)
    return validate_ini_options(result)


def load_ini_settings(path, overrides=None):
    options = merge_ini_overrides(ini_options(path), overrides)
    return standalone_settings(options)


def render_ini_config(overrides=None):
    options = merge_ini_overrides(ini_default_options(), overrides)
    schedule_lines = (["time_ranges = " + ", ".join(options.time_ranges)]
                      if options.time_ranges is not None else
                      ["start_times = " + ", ".join(options.start_times),
                       "duration_hours = " + str(options.duration_hours)])
    return "\n".join([
        "; 카메라 예약 녹화 설정 — 수정 후 녹화 프로그램/서비스를 재시작하세요.",
        "; 경로를 직접 지정하면 이 INI 파일의 폴더를 기준으로 해석합니다.",
        "; RTSP 주소와 계정은 기존 setup.ini에서 읽습니다.",
        "",
        "[CAMERAS]",
        "; auto: 프로그램 근처 config/setup.ini, 이후 /home/dis에서 검색",
        "setup_ini = " + (str(options.setup_ini) if options.setup_ini is not None else "auto"),
        "; auto: INUSE=True 카메라 / 직접 선택 예: CAM1, CAM2",
        "cameras = " + (", ".join(options.cameras) if options.cameras is not None else "auto"),
        "",
        "[RECORDING]",
        "; 한국 시간 기준 매일 녹화 구간. 쉼표로 구분합니다. 자정 통과 가능.",
        "; 예: 06:30-07:30, 10:00-11:00, 12:00-13:00, 17:00-18:00, 21:00-22:00",
        *schedule_lines,
        "; window: 예약 구간 전체를 한 파일로 저장. split: 아래 간격으로 분할.",
        "recording_mode = " + options.recording_mode,
        "; split일 때만 사용하는 MP4 파일 분할 간격(분)",
        "segment_minutes = " + str(options.segment_minutes),
        "; auto: setup.ini가 속한 프로젝트의 runtime/scheduled_recordings",
        "output_dir = " + (str(options.output_dir) if options.output_dir is not None else "auto"),
        "; 0이면 자동 삭제하지 않습니다.",
        "retention_days = " + str(options.retention_days),
        "; 이 여유 공간(GiB) 미만이면 녹화를 멈춥니다.",
        "min_free_gb = " + str(options.min_free_gb),
        "ffmpeg = " + options.ffmpeg,
        "reconnect_seconds = " + str(options.reconnect_seconds),
        "stall_seconds = " + str(options.stall_seconds),
        "",
    ])


def render_json_config(overrides=None):
    options = merge_ini_overrides(ini_default_options(), overrides)
    use_setup = options.setup_ini is not None or options.cameras is not None
    data = {
        "setup_ini": str(options.setup_ini) if options.setup_ini is not None else "auto",
        "output_dir": str(options.output_dir) if options.output_dir is not None else ("auto" if use_setup else "./recordings"),
        "intersection_name": "",
        "weather": "",
        "recording_dates": [],
        "weather_condition": default_weather_config(),
        "cameras": ([{"name": section.lower(), "section": section} for section in options.cameras]
                    if options.cameras is not None else ("auto" if use_setup else [])),
    }
    if options.time_ranges is not None:
        data["time_ranges"] = options.time_ranges
    else:
        data["schedules"] = [{"start": start, "duration_hours": options.duration_hours}
                             for start in options.start_times]
    for key in ("recording_mode", "segment_minutes", "retention_days", "min_free_gb", "ffmpeg",
                "reconnect_seconds", "stall_seconds"):
        data[key] = getattr(options, key)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def ini_publish_file(source, destination):
    # Windows rename fails when the destination already exists; POSIX rename
    # would replace it, so use an exclusive hard-link publication there.
    if os.name == "nt":
        os.rename(str(source), str(destination))
    else:
        os.link(str(source), str(destination))


def create_ini_config(path=None, overrides=None, owner=None):
    path = default_ini_path() if path is None else Path(path).expanduser().absolute()
    return create_config_file(path, lambda: render_ini_config(overrides), owner)


def create_json_config(path, overrides=None, owner=None):
    path = Path(path).expanduser().absolute()
    return create_config_file(path, lambda: render_json_config(overrides), owner)


def create_config_file(path, render, owner=None):
    import tempfile

    def already_exists():
        if path.is_symlink():
            raise ValueError(path.name + " must not be a symlink")
        if path.exists():
            if not path.is_file():
                raise ValueError(path.name + " must be a regular file")
            return True
        return False

    if already_exists():
        return False
    content = render()
    ownership = None
    if owner is not None and getattr(os, "geteuid", lambda: None)() == 0:
        import pwd

        try:
            account = pwd.getpwnam(owner)
        except KeyError:
            raise ValueError("Configuration owner does not exist: " + owner) from None
        ownership = account.pw_uid, account.pw_gid
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=str(path.parent), prefix=".recording-" + path.suffix[1:] + "-",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(str(temporary), 0o600)
            if ownership is not None:
                os.fchown(handle.fileno(), *ownership)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Publish a complete file atomically without ever replacing an existing
        # destination. Both names are in one directory/on the same filesystem.
        try:
            ini_publish_file(temporary, path)
        except FileExistsError:
            if already_exists():
                return False
            raise
        return True
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


# BEGIN EMBEDDED ASSETS
EMBEDDED_REGIONS = (
    "eNqsvV+PFUmS6PlVWv2w2pX6MuH/3UdarbQfYJ/27eo+9NxuzbbuTNeop1u7o32B5MAmnYkgezhwgEwmqYICSonqAAk3kZIvlOfk"
    "d1j3CDMPD/eIE+alK9X0nKLwX5ib/zM3Nzf/f3/9r9/95U//9fe//vtf/19//vO//Ovf/93f/e63f/7tjf/2z7+98Y/f3fhvf/q7"
    "//rdP//zX/74hz//29/9w3e//dPv/u53v//zb//wT/97+H3jd9/9b//wD3/63f/5b//y+//ju/9V/k/hX/wPbS3nv/7Nr//yL571"
    "+995Nm+4/k+N+U8N83/82z//+U9/+Ie//PkP3/3R/6eri/X29q3tx/e/2jx4ulm82q72N5/Of7V9c3N7+vBX2+8Pr4+O/+ft8eLq"
    "47fNg8X/8ptfBdbmzuJXZvv80a9C6Vc3PfS/fvdPf/nnP/7rr//+P//6X//wu+/8n/zrH/7xL3/8x7/4X7//y7/887/9/rs//u67"
    "P/6j/9c//j/hf/7N/88//fbPf/jzX373+/DT/7fu93/5za//9N3/HUj/+dfbxfH22cX1X79uPi22B8f+77X/6OY3jJvfCHNDaaH8"
    "b33D2f/ym7G/v/3+zubl8dXns7ygaQQUZHq+qNfP9tn59bO32xcPvZ4GJCtZRzINhbR3tn1zK2cYwZHhKIxLL1AhB5dQI0FgbL7c"
    "3C4fFQxnO4bWcp5x/eDedv0tY+iGQ5P4fkiQ4+x8u7xT6IMhg/F5xtXnh5u9tzlDO0BIRkCsb14/OyzEMNAsVlOapf3NbvAb4ob0"
    "wEIgA8p1jApTN/QYyAWxzI2mMRTSyfn106KljUSGFoSWXh2PMHxHAwan9Pz1t+3B6fb0mHUcligGOMzKCg4ve40GDncVHFFwFGBI"
    "6t1/vz256Ks1htEVGJ5rx0BD8amR8OpoZH7TMCk5p2eKbe8eXn36nAuvnIYZabK3IsAPnOvVogfoDiBxOmJyBrD5qZgAVBx5xs59"
    "frkoS8Pk7oyZq/2L19sv78raM+jZzs0Ars6Prk/OiiHKzNwQjRKcLPxq6ztCwRA4X4g5DfqOvDn4WtZCQA/Uc51gc/Bqu7cu2jAO"
    "qEbMSbD8tt1f5QDpEDCrhY/v/Y+yE3GsgSGpwA8gVUwvHNTAxVxj+qJxnmNZZwwApuY649N318unhQRSowSzLfnqaPPjZaEHjsvy"
    "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"
)

EMBEDDED_UI = (
    "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"
)
# END EMBEDDED ASSETS

# BEGIN EMBEDDED WEATHER
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
    return {"enabled": False, "region": {"sido": "", "sigungu": "", "eupmyeondong": ""},
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


def validate_weather_region(condition):
    if condition is None or not any(condition.region) or condition.nx <= 0 or condition.ny <= 0:
        raise ValueError("날씨 예보를 사용하려면 시간표 · 날씨에서 지역을 먼저 선택하세요.")


def validate_weather_access(condition):
    validate_weather_region(condition)
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
            # Disabled, unconfigured weather is valid as a saved draft, but no
            # manual/background request may send guessed coordinates such as 0,0.
            validate_weather_region(self.condition)
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
# END EMBEDDED WEATHER

# BEGIN EMBEDDED PUBLIC_WEATHER
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
# END EMBEDDED PUBLIC_WEATHER

# BEGIN EMBEDDED LIVE_VIEW
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
# END EMBEDDED LIVE_VIEW

# BEGIN EMBEDDED CAMERA_PROFILES
# Embedded before ui_server.py. Camera profile control uses only Python's standard library.
import hashlib as _cp_hashlib
import http.client as _cp_http
import ipaddress as _cp_ipaddress
import json as _cp_json
import os as _cp_os
from pathlib import Path as _cp_Path
import re as _cp_re
import secrets as _cp_secrets
import ssl as _cp_ssl
import threading as _cp_threading
import time as _cp_time
from urllib.parse import unquote as _cp_unquote, urlencode as _cp_urlencode, urlsplit as _cp_urlsplit
from urllib.request import parse_http_list as _cp_parse_list, parse_keqv_list as _cp_parse_pairs
import xml.etree.ElementTree as _cp_xml


class CameraProfileError(ValueError):
    pass


def _camera_digest_header(challenge, username, password, uri, nc=1, cnonce=None):
    """Digest auth, including SHA-256 supported by current Wisenet firmware."""
    if not challenge.lower().startswith('digest '):
        raise CameraProfileError('카메라의 HTTPS Digest 인증을 지원하지 않습니다.')
    fields = _cp_parse_pairs(_cp_parse_list(challenge[7:]))
    algorithm = fields.get('algorithm', 'MD5').upper()
    base = algorithm.removesuffix('-SESS') if hasattr(str, 'removesuffix') else algorithm.replace('-SESS', '')
    names = {'MD5': 'md5', 'SHA-256': 'sha256', 'SHA-512-256': 'sha512_256'}
    if base not in names or not fields.get('nonce') or 'realm' not in fields:
        raise CameraProfileError('지원하지 않는 카메라 인증 방식입니다.')
    qops = [part.strip() for part in fields.get('qop', '').split(',') if part.strip()]
    if qops and 'auth' not in qops:
        raise CameraProfileError('지원하지 않는 카메라 Digest qop입니다.')
    cnonce = cnonce or _cp_secrets.token_hex(16)
    digest = lambda value: _cp_hashlib.new(names[base], value.encode('utf-8')).hexdigest()
    ha1 = digest(username + ':' + fields['realm'] + ':' + password)
    if algorithm.endswith('-SESS'):
        ha1 = digest(ha1 + ':' + fields['nonce'] + ':' + cnonce)
    ha2 = digest('GET:' + uri)
    nctext = '{:08x}'.format(nc)
    response = digest(ha1 + ':' + fields['nonce'] + ':' +
                      (nctext + ':' + cnonce + ':auth:' if qops else '') + ha2)
    values = {'username': username, 'realm': fields['realm'], 'nonce': fields['nonce'],
              'uri': uri, 'response': response, 'algorithm': algorithm}
    if qops:
        values.update(qop='auth', nc=nctext, cnonce=cnonce)
    elif algorithm.endswith('-SESS'):
        values['cnonce'] = cnonce
    if 'opaque' in fields:
        values['opaque'] = fields['opaque']
    if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for value in values.values()):
        raise CameraProfileError('카메라 인증 응답 형식이 올바르지 않습니다.')
    quote = lambda value: '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return 'Digest ' + ', '.join(key + '=' + (value if key in ('algorithm', 'qop', 'nc') else quote(value))
                                  for key, value in values.items())


class WisenetProfileClient:
    """One registered IP, HTTPS only, no redirects/proxies or password retries.

    Local cameras use self-signed certificates. The certificate is pinned within
    this plan/apply session; this is not a CA-authenticated device identity.
    """
    def __init__(self, url):
        parsed = _cp_urlsplit(url)
        try:
            address = _cp_ipaddress.ip_address(parsed.hostname or '')
        except ValueError:
            raise CameraProfileError('프로파일 관리는 저장된 카메라의 IP 주소로만 접속할 수 있습니다.') from None
        if parsed.scheme != 'rtsp' or address.is_loopback or address.is_unspecified or address.is_multicast:
            raise CameraProfileError('저장된 RTSP 카메라 IP가 올바르지 않습니다.')
        if not parsed.username or parsed.password is None:
            raise CameraProfileError('카메라 설정을 변경하려면 저장된 RTSP 주소에 관리 계정이 필요합니다.')
        self.host = str(address)
        self.username, self.password = _cp_unquote(parsed.username), _cp_unquote(parsed.password)
        self.certificate = None
        self.challenge, self.nonce_count = None, 0
        self.context = _cp_ssl.SSLContext(_cp_ssl.PROTOCOL_TLS_CLIENT)
        self.context.check_hostname = False
        self.context.verify_mode = _cp_ssl.CERT_NONE

    def get(self, path, params=None):
        allowed = ('/stw-cgi/media.cgi', '/stw-cgi/system.cgi',
                   '/stw-cgi/attributes.cgi/cgis', '/stw-cgi/attributes.cgi/attributes')
        if path not in allowed:
            raise CameraProfileError('허용되지 않은 카메라 설정 경로입니다.')
        uri = path + ('?' + _cp_urlencode(params) if params else '')
        # At most one unauthenticated challenge followed by one authenticated call.
        for attempt in range(2):
            headers = {'Accept': 'text/plain, application/xml', 'Connection': 'close'}
            authenticated = self.challenge is not None
            if authenticated:
                self.nonce_count += 1
                headers['Authorization'] = _camera_digest_header(self.challenge, self.username, self.password, uri, self.nonce_count)
            connection = _cp_http.HTTPSConnection(self.host, port=443, timeout=8, context=self.context)
            try:
                connection.connect()
                certificate = _cp_hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                if self.certificate is not None and certificate != self.certificate:
                    raise CameraProfileError('카메라 인증서가 변경되어 작업을 중단했습니다. IP 중복 여부를 확인하세요.')
                self.certificate = certificate
                connection.request('GET', uri, headers=headers)
                response = connection.getresponse()
                status = response.status
                challenge = response.getheader('WWW-Authenticate', '')
                raw = response.read(4 * 1024 * 1024 + 1)
            except CameraProfileError:
                raise
            except (OSError, _cp_http.HTTPException):
                raise CameraProfileError('카메라 HTTPS 설정 연결에 실패했습니다. IP·연결·HTTPS 서비스를 확인하세요.') from None
            finally:
                connection.close()
            if status == 401:
                if authenticated or attempt:
                    raise CameraProfileError('카메라 인증이 거부되었습니다. 계정·암호·로그인 잠금을 확인하세요. 재시도하지 않았습니다.')
                self.challenge = challenge
                continue
            if status != 200:
                raise CameraProfileError('카메라 설정 요청이 거부되었습니다 (HTTP {}).'.format(status))
            if len(raw) > 4 * 1024 * 1024:
                raise CameraProfileError('카메라 설정 응답이 너무 큽니다.')
            try:
                return raw.decode('utf-8-sig')
            except UnicodeError:
                raise CameraProfileError('카메라 설정 응답을 읽을 수 없습니다.') from None
        raise CameraProfileError('카메라 인증을 완료하지 못했습니다.')


def _camera_pairs(raw):
    rows = {}
    for line in raw.splitlines():
        key, separator, value = line.partition('=')
        if separator:
            if key in rows:
                raise CameraProfileError('카메라 설정 응답에 중복 항목이 있습니다.')
            rows[key] = value
    return rows


def _camera_profile_rows(raw):
    result = {}
    for key, value in _camera_pairs(raw).items():
        match = _cp_re.fullmatch(r'Channel\.(\d+)\.Profile\.(\d+)\.([A-Za-z0-9_.]+)', key)
        if match:
            if match[1] != '0':
                raise CameraProfileError('여러 채널 카메라는 자동 프로파일 변경을 지원하지 않습니다.')
            result.setdefault(match[2], {})[match[3]] = value
    if not result:
        raise CameraProfileError('카메라가 Wisenet 프로파일 목록을 반환하지 않았습니다.')
    return result


def _camera_policy(raw):
    rows = _camera_pairs(raw)
    if any(key.startswith('Channel.') and not key.startswith('Channel.0.') for key in rows):
        raise CameraProfileError('여러 채널 카메라의 기본 프로파일은 변경할 수 없습니다.')
    if 'Channel.0.DefaultProfile' not in rows:
        raise CameraProfileError('카메라 기본 프로파일을 확인할 수 없습니다.')
    return rows


def _camera_xml(raw):
    if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        raise CameraProfileError('카메라 기능 정보의 XML 형식이 올바르지 않습니다.')
    try:
        return _cp_xml.fromstring(raw)
    except _cp_xml.ParseError:
        raise CameraProfileError('카메라 기능 정보를 확인할 수 없습니다.') from None


def _camera_capabilities(cgis, attributes):
    root, attrs = _camera_xml(cgis), _camera_xml(attributes)
    action = root.find("cgi[@name='media']/submenu[@name='videoprofile']/action[@name='add/update']")
    policy = root.find("cgi[@name='media']/submenu[@name='videoprofilepolicy']/action[@name='set']")
    if action is None or policy is None:
        raise CameraProfileError('이 카메라는 확인된 프로파일 생성·변경 API를 지원하지 않습니다.')
    parameters = {item.get('name'): item for item in action.findall('parameter') if item.get('request') == 'true'}
    model = attrs.find("group[@name='System']/category[@name='Property']/attribute[@name='ModelName']")
    support = attrs.find("group[@name='Media']/category[@name='Support']/channel[@number='0']")
    limit = attrs.find("group[@name='Media']/category[@name='Limit']/channel[@number='0']")
    if support is None or limit is None:
        raise CameraProfileError('카메라 채널 0의 프로파일 지원 범위를 확인할 수 없습니다.')
    features = {item.get('name'): item.get('value') for item in support.findall('attribute')}
    limits = {item.get('name'): item.get('value') for item in limit.findall('attribute')}
    def enums(name):
        parameter = parameters.get(name)
        return [item.get('value') for item in parameter.findall('dataType/enum/entry')] if parameter is not None else []
    def bounds(name):
        parameter = parameters.get(name)
        node = parameter.find('dataType/int') if parameter is not None else None
        try:
            return [int(node.get('min')), int(node.get('max'))] if node is not None else []
        except (TypeError, ValueError):
            return []
    return {'model': model.get('value') if model is not None else 'Unknown',
            'parameters': sorted(parameters), 'resolutions': enums('Resolution'),
            'encodings': enums('EncodingType'), 'fps': bounds('FrameRate'), 'bitrate': bounds('Bitrate'),
            'gov': bounds('H264.GOVLength'), 'profile_range': bounds('Profile'),
            'profile_add_by_index': features.get('ProfileAddByIndex') == 'True',
            'max_profile': limits.get('MaxProfile'), 'max_resolution': limits.get('MaxResolution'),
            'default_set': policy.find("parameter[@name='DefaultProfile']") is not None,
            'schema_sha256': _cp_hashlib.sha256((cgis + '\n' + attributes).encode()).hexdigest()}


_CAMERA_PROFILE_COPY_KEYS = ('EncodingType', 'Resolution', 'FrameRate', 'Bitrate',
    'RTPMulticastEnable', 'H264.BitrateControlType', 'H264.PriorityType', 'H264.GOVLength',
    'H264.Profile', 'H264.EntropyCoding', 'H264.SmartCodecEnable', 'H264.DynamicFPSEnable',
    'H264.MinDynamicFPS', 'H264.DynamicGOVEnable', 'H264.DynamicGOVLength',
    'AudioInputEnable', 'ViewModeType', 'IsDigitalPTZProfile', 'IsFixedFrameRateProfile')


def _camera_profile_targets(snapshot):
    profiles, caps = snapshot['profiles'], snapshot['capabilities']
    if ('2' not in profiles or profiles['2'].get('EncodingType') != 'H264'
            or 'H264' not in caps['encodings'] or not {'3840x2160', '1280x720'}.issubset(caps['resolutions'])
            or not caps['fps'] or not caps['fps'][0] <= 15 <= 30 <= caps['fps'][1]
            or not caps['profile_range'] or not caps['profile_range'][0] <= 2 <= 4 <= caps['profile_range'][1]):
        raise CameraProfileError('이 카메라에서 H.264 프로파일 2·4와 HD 15fps·4K 30fps 지원을 확인하지 못했습니다.')
    required = {'EncodingType', 'Resolution', 'FrameRate', 'Bitrate', 'H264.GOVLength'}
    if not required.issubset(caps['parameters']):
        raise CameraProfileError('필요한 카메라 프로파일 변경 기능을 확인하지 못했습니다.')
    if '4' not in profiles and not caps['profile_add_by_index']:
        raise CameraProfileError('이 카메라는 4번 프로파일을 지정해서 생성할 수 없습니다.')
    if ('4' in profiles and profiles['4'].get('IsFixedProfile') == 'True'
            and profiles['4'].get('EncodingType') != 'H264'):
        raise CameraProfileError('4번이 다른 코덱의 고정 프로파일이어서 자동 변경할 수 없습니다.')
    choose = lambda resolution, fps: next((fields for fields in profiles.values()
        if fields.get('EncodingType') == 'H264' and fields.get('Resolution') == resolution
        and fields.get('FrameRate') == fps), None)
    warnings = []
    targets = {}
    for profile, resolution, fps, bitrate, gov in (('4', '3840x2160', '30', 8192, '60'), ('2', '1280x720', '15', 1024, '30')):
        source = choose(resolution, fps)
        existing_source = source is not None
        source = source or profiles['2']
        target = {key: source[key] for key in _CAMERA_PROFILE_COPY_KEYS
                  if key in source and key in caps['parameters']}
        target.update(EncodingType='H264', Resolution=resolution, FrameRate=fps)
        if not existing_source:
            # Generic camera minimum bitrates can depend on its active resolution.
            # Honor advertised limits instead of assuming the tested model's 720p limit.
            bounds = caps['bitrate']
            if len(bounds) != 2 or not caps['gov'] or not caps['gov'][0] <= int(gov) <= caps['gov'][1]:
                raise CameraProfileError('새 프로파일의 비트레이트·GOV 지원 범위를 확인하지 못했습니다.')
            target['Bitrate'] = str(max(bounds[0], min(bitrate, bounds[1])))
            target['H264.GOVLength'] = gov
            for key in ('H264.SmartCodecEnable', 'H264.DynamicFPSEnable', 'H264.DynamicGOVEnable'):
                if key in caps['parameters']:
                    target[key] = 'False'
            warnings.append('{}번은 카메라 지원 범위에 맞춰 {} · {}fps · {}kbps 설정을 생성합니다.'.format(profile, resolution, fps, target['Bitrate']))
        if profile == '4' and profiles.get('4', {}).get('IsFixedProfile') != 'True':
            if 'Name' not in caps['parameters']:
                raise CameraProfileError('4번 프로파일 이름 설정을 지원하지 않습니다.')
            target['Name'] = '4K'
        if not required.issubset(target):
            raise CameraProfileError('원본 H.264 프로파일의 필수 영상 설정이 누락되어 변경할 수 없습니다.')
        targets[profile] = target
    if '4' in profiles and any(profiles['4'].get(key) != value for key, value in targets['4'].items()):
        warnings.append('현재 4번 프로파일을 사용하는 수신 장비도 변경된 4K 영상을 받습니다.')
    if any(profiles['2'].get(key) != value for key, value in targets['2'].items()):
        warnings.append('현재 2번 프로파일을 사용하는 수신 장비는 HD 영상으로 바뀝니다.')
    return targets, warnings


def _camera_private_json(path, value):
    encoded = _cp_json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    temp = path.with_name('.' + path.name + '.' + _cp_secrets.token_hex(8))
    fd = _cp_os.open(str(temp), _cp_os.O_WRONLY | _cp_os.O_CREAT | _cp_os.O_EXCL, 0o600)
    try:
        with _cp_os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            _cp_os.fsync(stream.fileno())
        _cp_os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


class CameraProfileManager:
    """Read and prepare plans; consume a plan once while the recorder is stopped."""
    def __init__(self, controller, client_factory=None):
        self.controller = controller
        self.client_factory = client_factory or WisenetProfileClient
        self.plans = {}
        self.lock = _cp_threading.RLock()

    def _camera(self, name):
        if not isinstance(name, str):
            raise CameraProfileError('저장된 카메라를 선택하세요.')
        cameras = [camera for camera in self.controller.settings().cameras if camera.name == name]
        if len(cameras) != 1:
            raise CameraProfileError('현재 설정에서 선택한 카메라를 찾을 수 없습니다.')
        return cameras[0]

    @staticmethod
    def _snapshot(client):
        profiles = _camera_profile_rows(client.get('/stw-cgi/media.cgi', {'msubmenu': 'videoprofile', 'action': 'view'}))
        policy = _camera_policy(client.get('/stw-cgi/media.cgi', {'msubmenu': 'videoprofilepolicy', 'action': 'view'}))
        cgis = client.get('/stw-cgi/attributes.cgi/cgis')
        attributes = client.get('/stw-cgi/attributes.cgi/attributes')
        capabilities = _camera_capabilities(cgis, attributes)
        info = _camera_pairs(client.get('/stw-cgi/system.cgi', {'msubmenu': 'deviceinfo', 'action': 'view'}))
        identity = {key: info[key] for key in ('Model', 'SerialNumber', 'ConnectedMACAddress', 'FirmwareVersion') if key in info}
        if not identity.get('SerialNumber') and not identity.get('ConnectedMACAddress'):
            raise CameraProfileError('카메라 장치 식별 정보를 확인하지 못했습니다.')
        return {'profiles': profiles, 'policy': policy, 'capabilities': capabilities,
                'identity': identity, 'certificate_sha256': client.certificate}

    def read(self, camera_name):
        with self.lock:
            camera = self._camera(camera_name)
            client = self.client_factory(camera.url)
            snapshot = self._snapshot(client)
            return dict(snapshot, camera=camera_name, host=client.host, read_only=True)

    def plan(self, camera_name, default_profile4=True):
        if not isinstance(default_profile4, bool):
            raise CameraProfileError('기본 프로파일 선택이 올바르지 않습니다.')
        with self.lock:
            camera = self._camera(camera_name)
            _, revision = self.controller.read_config()
            client = self.client_factory(camera.url)
            snapshot = self._snapshot(client)
            targets, warnings = _camera_profile_targets(snapshot)
            if default_profile4 and not snapshot['capabilities']['default_set']:
                raise CameraProfileError('기본 프로파일 변경 기능을 확인하지 못했습니다.')
            changes = [{'profile': profile, 'action': 'update' if profile in snapshot['profiles'] else 'add',
                        'settings': targets[profile]} for profile in ('4', '2')
                       if any(snapshot['profiles'].get(profile, {}).get(key) != value for key, value in targets[profile].items())]
            token = _cp_secrets.token_urlsafe(32)
            now = _cp_time.monotonic()
            self.plans = {key: value for key, value in self.plans.items() if now - value['created'] < 600}
            if len(self.plans) >= 32:
                self.plans.clear()
            entry = {'created': now, 'camera': camera_name, 'host': client.host, 'client': client,
                     'revision': revision, 'url_digest': _cp_hashlib.sha256(camera.url.encode()).hexdigest(),
                     'snapshot': snapshot, 'targets': targets, 'changes': changes,
                     'default_profile4': default_profile4}
            self.plans[token] = entry
            return {'token': token, 'expires_seconds': 600, 'camera': camera_name, 'host': client.host,
                    'profiles': snapshot['profiles'], 'before': snapshot['profiles'], 'targets': targets,
                    'changes': changes, 'warnings': warnings, 'default_profile4': default_profile4,
                    'default_before': snapshot['policy']['Channel.0.DefaultProfile'],
                    'model': snapshot['capabilities']['model'], 'read_only': True}

    def apply(self, token):
        if not isinstance(token, str):
            raise CameraProfileError('변경 계획을 먼저 확인하세요.')
        with self.controller.action_lock, self.lock:
            if self.controller.closing:
                raise CameraProfileError('녹화 관리 화면을 종료하고 있어 카메라 설정을 변경할 수 없습니다.')
            entry = self.plans.pop(token, None)
            if entry is None or _cp_time.monotonic() - entry['created'] >= 600:
                raise CameraProfileError('변경 계획이 만료되었습니다. 다시 조회하세요.')
            state = self.controller.state()
            if state.get('error') or state.get('running') or state.get('external_running') or state.get('active_cameras'):
                raise CameraProfileError('카메라 프로파일을 바꾸기 전에 녹화 프로그램을 중지하세요. 예약 대기도 중지해야 합니다.')
            camera = self._camera(entry['camera'])
            _, revision = self.controller.read_config()
            if revision != entry['revision'] or _cp_hashlib.sha256(camera.url.encode()).hexdigest() != entry['url_digest']:
                raise CameraProfileError('저장된 카메라 설정이 변경되었습니다. 계획을 다시 조회하세요.')
            client = entry['client']
            before = self._snapshot(client)
            if before != entry['snapshot']:
                raise CameraProfileError('카메라 설정이나 장치가 변경되었습니다. 계획을 다시 조회하세요.')
            root = _cp_Path(self.controller.config_path).parent / '.camera-profile-backups'
            if root.is_symlink():
                raise CameraProfileError('카메라 설정 백업 경로가 심볼릭 링크입니다.')
            root.mkdir(mode=0o700, exist_ok=True)
            directory = root / (_cp_time.strftime('%Y%m%d_%H%M%S') + '_' + _cp_secrets.token_hex(6))
            directory.mkdir(mode=0o700)
            journal = {'camera': entry['camera'], 'host': entry['host'], 'before': before,
                       'changes': entry['changes'], 'default_profile4': entry['default_profile4'],
                       'steps': [], 'complete': False}
            journal_path = directory / 'backup-and-result.json'
            _camera_private_json(journal_path, journal)
            expected_profiles, expected_policy = before['profiles'], before['policy']
            try:
                for change in entry['changes']:
                    profile, wanted = change['profile'], change['settings']
                    step = {'profile': profile, 'action': change['action'], 'attempted': True, 'verified': False}
                    journal['steps'].append(step)
                    _camera_private_json(journal_path, journal)
                    client.get('/stw-cgi/media.cgi', dict(msubmenu='videoprofile', action=change['action'], Channel='0', Profile=profile, **wanted))
                    after = _camera_profile_rows(client.get('/stw-cgi/media.cgi', {'msubmenu': 'videoprofile', 'action': 'view'}))
                    if (set(after) != set(expected_profiles) | {profile}
                            or any(after[profile].get(key) != value for key, value in wanted.items())
                            or any(after.get(key) != value for key, value in expected_profiles.items() if key != profile)):
                        raise CameraProfileError('저장 후 프로파일 값이 예상과 달라 후속 변경을 중단했습니다.')
                    expected_profiles = after
                    step.update(verified=True, after_profiles=after)
                    _camera_private_json(journal_path, journal)
                if entry['default_profile4'] and expected_policy['Channel.0.DefaultProfile'] != '4':
                    step = {'policy': 'DefaultProfile', 'attempted': True, 'verified': False}
                    journal['steps'].append(step)
                    _camera_private_json(journal_path, journal)
                    client.get('/stw-cgi/media.cgi', {'msubmenu': 'videoprofilepolicy', 'action': 'set', 'Channel': '0', 'DefaultProfile': '4'})
                    policy = _camera_policy(client.get('/stw-cgi/media.cgi', {'msubmenu': 'videoprofilepolicy', 'action': 'view'}))
                    wanted_policy = dict(expected_policy, **{'Channel.0.DefaultProfile': '4'})
                    if policy != wanted_policy:
                        raise CameraProfileError('저장 후 기본 프로파일 정책이 예상과 다릅니다.')
                    expected_policy = policy
                    step['verified'] = True
                    _camera_private_json(journal_path, journal)
                final = self._snapshot(client)
                if (final['profiles'] != expected_profiles or final['policy'] != expected_policy
                        or final['identity'] != before['identity'] or final['certificate_sha256'] != before['certificate_sha256']):
                    raise CameraProfileError('최종 카메라 설정 확인에 실패했습니다.')
                journal.update(complete=True, after=final)
                _camera_private_json(journal_path, journal)
            except Exception:
                journal['error'] = '적용 또는 검증 실패. 자동 재시도·자동 원복하지 않았습니다.'
                try:
                    _camera_private_json(journal_path, journal)
                except OSError:
                    # A full/disconnected backup disk must not hide the warning
                    # that the camera may already have accepted a prior step.
                    pass
                raise CameraProfileError('카메라 변경을 완료하지 못했습니다. 일부 단계는 적용됐을 수 있습니다. 백업·결과: ' + str(journal_path)) from None
            return {'ok': True, 'camera': entry['camera'], 'host': entry['host'], 'profiles': final['profiles'],
                    'policy': final['policy'], 'backup': str(journal_path),
                    'message': '2번 HD·4번 4K 설정을 확인했습니다. 녹화 주소 변경은 별도 적용하세요.'}


def camera_profiles_cli(config_path, camera_name, apply=False, default_profile4=True):
    """Used by record.py CLI. --apply means explicitly execute the generated plan."""
    controller = RecorderUiController(config_path)
    manager = CameraProfileManager(controller)
    plan = manager.plan(camera_name, default_profile4=default_profile4)
    if not apply:
        plan.pop('token', None)  # An in-memory token cannot survive CLI exit.
        return plan
    return manager.apply(plan['token'])
# END EMBEDDED CAMERA_PROFILES

# BEGIN EMBEDDED CAMERA_PING
# Embedded camera reachability monitor; no routes, interfaces or camera settings are changed.
import concurrent.futures as _ping_futures
from datetime import datetime as _ping_datetime, timedelta as _ping_timedelta, timezone as _ping_timezone
import ipaddress as _ping_ipaddress
import json as _ping_json
import math as _ping_math
import os as _ping_os
from pathlib import Path as _ping_Path
import re as _ping_re
import shutil as _ping_shutil
import stat as _ping_stat
import subprocess as _ping_subprocess
import sys as _ping_sys
import threading as _ping_threading
import time as _ping_time
from urllib.parse import urlsplit as _ping_urlsplit
import uuid as _ping_uuid

_PING_KST = _ping_timezone(_ping_timedelta(hours=9), 'Asia/Seoul')
_PING_MAX_SAMPLES = 32768
_PING_RETENTION_SECONDS = 24 * 60 * 60
_PING_MAX_CAMERAS = 64
_PING_ERROR_TYPES = {'', 'timeout', 'unreachable', 'ping_unavailable', 'permission_denied', 'probe_error'}
_PING_PLATFORM_NOTICE = 'ping 이력 감시는 Linux에서 사용할 수 있습니다. 이 PC에서는 설정 화면만 확인합니다.'


def _ping_iso(timestamp):
    return _ping_datetime.fromtimestamp(timestamp, _PING_KST).isoformat(timespec='seconds')


def _ping_literal_ip(value):
    if not isinstance(value, str) or '%' in value:
        raise ValueError('Camera IP must be a literal address')
    address = _ping_ipaddress.ip_address(value)
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError('Camera IP must be a unicast address')
    return str(address)


def _ping_one(ip):
    """One ICMP echo. A failed result does not imply RTSP or camera power failure."""
    ip = _ping_literal_ip(ip)
    if _ping_sys.platform != 'linux':
        return {'ok': False, 'rtt_ms': None, 'error': 'ping_unavailable'}
    executable = _ping_shutil.which('ping')
    if not executable:
        return {'ok': False, 'rtt_ms': None, 'error': 'ping_unavailable'}
    try:
        result = _ping_subprocess.run([executable, '-n', '-c', '1', '-W', '1', ip],
            stdin=_ping_subprocess.DEVNULL, stdout=_ping_subprocess.PIPE, stderr=_ping_subprocess.PIPE,
            text=True, errors='replace', timeout=3, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
    except _ping_subprocess.TimeoutExpired:
        return {'ok': False, 'rtt_ms': None, 'error': 'timeout'}
    except FileNotFoundError:
        return {'ok': False, 'rtt_ms': None, 'error': 'ping_unavailable'}
    except PermissionError:
        return {'ok': False, 'rtt_ms': None, 'error': 'permission_denied'}
    except OSError:
        return {'ok': False, 'rtt_ms': None, 'error': 'probe_error'}
    if result.returncode == 0:
        match = _ping_re.search(r'\btime([=<])\s*([0-9]+(?:\.[0-9]+)?)\s*ms', result.stdout)
        return {'ok': True, 'rtt_ms': float(match[2]) if match else None, 'error': '',
                'rtt_upper_bound': bool(match and match[1] == '<')}
    combined = (result.stdout + result.stderr).lower()
    if 'operation not permitted' in combined or 'permission denied' in combined:
        error = 'permission_denied'
    elif 'unreachable' in combined:
        error = 'unreachable'
    elif result.returncode == 1:
        error = 'timeout'
    else:
        error = 'probe_error'
    return {'ok': False, 'rtt_ms': None, 'error': error}


class CameraPingMonitor:
    """Monitor only while the local UI is running; persist at most 24 hours.

    Instantiate without starting a thread. Call start() from the running UI,
    close() on shutdown, and request_check() for a rate-limited manual check.
    """
    def __init__(self, settings_callback, config_path, interval_seconds=60, runner=None, clock=None):
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)) or not 10 <= interval_seconds <= 3600:
            raise ValueError('Ping interval must be between 10 and 3600 seconds')
        self.settings_callback = settings_callback
        self.path = _ping_Path(config_path).expanduser().absolute().parent / '.camera-ping-history.json'
        self.interval_seconds = float(interval_seconds)
        self.runner = runner or _ping_one
        self.clock = clock or _ping_time.time
        self.lock = _ping_threading.RLock()
        self.stop_event = _ping_threading.Event()
        self.wake_event = _ping_threading.Event()
        self.thread = None
        self.samples = []
        self.started_at = None
        self.last_checked = None
        self.last_round_started = None
        self.last_request_monotonic = None
        self.last_probe_monotonic = None
        self.checking = False
        self.notice = ''
        self._load()

    def _registered(self):
        cameras = []
        seen_names = set()
        settings = self.settings_callback()
        for camera in settings.cameras:
            if len(cameras) >= _PING_MAX_CAMERAS:
                break
            name = camera.name
            if (not isinstance(name, str) or not _ping_re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', name)
                    or name in seen_names):
                continue
            seen_names.add(name)
            try:
                ip = _ping_literal_ip(_ping_urlsplit(camera.url).hostname or '')
            except (ValueError, TypeError):
                cameras.append({'name': name, 'ip': '', 'supported': False})
                continue
            cameras.append({'name': name, 'ip': ip, 'supported': True})
            if len(cameras) >= _PING_MAX_CAMERAS:
                break
        return cameras

    @staticmethod
    def _sample(value):
        """Rebuild allowed fields, dropping any accidental/old secret fields."""
        if not isinstance(value, dict):
            raise ValueError()
        name, ip, timestamp = value.get('camera'), value.get('ip'), value.get('time')
        if not isinstance(name, str) or not _ping_re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', name):
            raise ValueError()
        ip = _ping_literal_ip(ip)
        parsed = _ping_datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            raise ValueError()
        epoch = parsed.timestamp()
        if not _ping_math.isfinite(epoch) or type(value.get('ok')) is not bool:
            raise ValueError()
        rtt, error = value.get('rtt_ms'), value.get('error', '')
        if rtt is not None and (isinstance(rtt, bool) or not isinstance(rtt, (int, float)) or not _ping_math.isfinite(rtt) or rtt < 0):
            raise ValueError()
        if error not in _PING_ERROR_TYPES:
            raise ValueError()
        return {'camera': name, 'ip': ip, 'time': _ping_iso(epoch), 'ok': value['ok'],
                'rtt_ms': rtt if value['ok'] else None, 'error': error if not value['ok'] else '',
                'rtt_upper_bound': bool(value.get('rtt_upper_bound', False)) if value['ok'] else False}

    def _prune(self, now):
        lower = now - _PING_RETENTION_SECONDS
        self.samples = [sample for sample in self.samples
            if lower <= _ping_datetime.fromisoformat(sample['time']).timestamp() <= now + 60][-_PING_MAX_SAMPLES:]

    def _load(self):
        # The CLI and desktop configuration preview remain importable on other
        # systems; do not access Linux-only flags or run a platform's different
        # ping command merely by opening those screens.
        if _ping_sys.platform != 'linux':
            self.notice = _PING_PLATFORM_NOTICE
            return
        try:
            fd = _ping_os.open(str(self.path), _ping_os.O_RDONLY | _ping_os.O_NOFOLLOW | _ping_os.O_NONBLOCK)
        except FileNotFoundError:
            return
        except OSError:
            self.notice = '이전 ping 이력을 읽을 수 없습니다. 현재 상태 검사는 계속할 수 있습니다.'
            return
        try:
            info = _ping_os.fstat(fd)
            if not _ping_stat.S_ISREG(info.st_mode) or info.st_size > 12 * 1024 * 1024:
                raise ValueError()
            with _ping_os.fdopen(fd, 'r', encoding='utf-8') as stream:
                fd = None
                data = _ping_json.load(stream)
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('samples'), list):
                raise ValueError()
            if len(data['samples']) > _PING_MAX_SAMPLES:
                raise ValueError()
            self.samples = [self._sample(sample) for sample in data['samples']]
            self.samples.sort(key=lambda sample: sample['time'])
            self._prune(self.clock())
            if self.samples:
                self.last_checked = self.samples[-1]['time']
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            self.samples = []
            self.notice = '이전 ping 이력 파일이 손상되어 새 이력으로 시작합니다.'
        finally:
            if fd is not None:
                _ping_os.close(fd)

    def _save(self):
        if _ping_sys.platform != 'linux':
            self.notice = _PING_PLATFORM_NOTICE
            return
        temporary = self.path.with_name('.camera-ping-history.' + _ping_uuid.uuid4().hex + '.tmp')
        try:
            if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
                raise OSError('Unsafe history path')
            fd = _ping_os.open(str(temporary), _ping_os.O_WRONLY | _ping_os.O_CREAT | _ping_os.O_EXCL | _ping_os.O_NOFOLLOW, 0o600)
            with _ping_os.fdopen(fd, 'w', encoding='utf-8') as stream:
                _ping_json.dump({'version': 1, 'samples': self.samples}, stream, ensure_ascii=False, separators=(',', ':'))
                stream.write('\n')
                stream.flush()
                _ping_os.fsync(stream.fileno())
            _ping_os.replace(str(temporary), str(self.path))
        except OSError:
            self.notice = 'ping 이력을 파일에 저장하지 못했습니다. 현재 실행 중인 이력은 화면에서 볼 수 있습니다.'
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def start(self):
        with self.lock:
            if _ping_sys.platform != 'linux':
                self.notice = _PING_PLATFORM_NOTICE
                return
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.wake_event.clear()
            self.started_at = _ping_iso(self.clock())
            self.thread = _ping_threading.Thread(target=self._loop, name='camera-ping-monitor', daemon=True)
            self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        thread = self.thread
        if thread is not None and thread is not _ping_threading.current_thread():
            thread.join(timeout=5)

    def request_check(self):
        with self.lock:
            now = _ping_time.monotonic()
            running = self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()
            recent = [stamp for stamp in (self.last_request_monotonic, self.last_probe_monotonic) if stamp is not None]
            remaining = max(0.0, 10 - (now - max(recent))) if recent else 0
            if not running or self.checking or remaining:
                return {'accepted': False, 'next_check_in': round(remaining, 1), 'monitoring': running, 'checking': self.checking}
            self.last_request_monotonic = now
            self.wake_event.set()
            return {'accepted': True, 'next_check_in': 10, 'monitoring': True, 'checking': False}

    def _loop(self):
        while not self.stop_event.is_set():
            self.wake_event.clear()
            started = _ping_time.monotonic()
            try:
                self._check_round()
            except Exception:
                with self.lock:
                    self.notice = 'ping 검사 중 오류가 발생했습니다. 다음 주기에 다시 검사합니다.'
            if self.stop_event.is_set():
                break
            self.wake_event.wait(timeout=max(0, self.interval_seconds - (_ping_time.monotonic() - started)))

    def _probe(self, camera):
        try:
            result = self.runner(camera['ip'])
            return self._sample(dict(result, camera=camera['name'], ip=camera['ip'], time=_ping_iso(self.clock())))
        except Exception:
            return {'camera': camera['name'], 'ip': camera['ip'], 'time': _ping_iso(self.clock()),
                    'ok': False, 'rtt_ms': None, 'rtt_upper_bound': False, 'error': 'probe_error'}

    def _check_round(self):
        with self.lock:
            if self.checking:
                return
            self.checking = True
            self.last_round_started = _ping_iso(self.clock())
            self.last_probe_monotonic = _ping_time.monotonic()
        collected = []
        try:
            try:
                cameras = [camera for camera in self._registered() if camera['supported']]
            except Exception:
                with self.lock:
                    self.notice = '저장된 카메라 설정을 읽지 못해 이번 ping 검사를 건너뛰었습니다.'
                return
            # At most four in flight; do not queue probes after UI shutdown.
            with _ping_futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='camera-ping') as pool:
                for offset in range(0, len(cameras), 4):
                    if self.stop_event.is_set():
                        break
                    collected.extend(pool.map(self._probe, cameras[offset:offset + 4]))
            with self.lock:
                self.samples.extend(collected)
                self.samples.sort(key=lambda sample: sample['time'])
                self._prune(self.clock())
                if collected:
                    self.last_checked = max(sample['time'] for sample in collected)
                    self._save()
        finally:
            with self.lock:
                self.checking = False

    def history(self, camera=None, hours=24):
        if isinstance(hours, str):
            try:
                hours = float(hours) if len(hours) <= 32 else None
            except ValueError:
                hours = None
        if (isinstance(hours, bool) or not isinstance(hours, (int, float))
                or not _ping_math.isfinite(hours) or not 1 <= hours <= 24):
            raise ValueError('ping 조회 시간은 1~24시간이어야 합니다.')
        if camera is not None and (not isinstance(camera, str) or not _ping_re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', camera)):
            raise ValueError('카메라 선택이 올바르지 않습니다.')
        try:
            cameras = self._registered()
        except Exception:
            cameras = []
        with self.lock:
            now = self.clock()
            self._prune(now)
            lower = now - hours * 60 * 60
            samples = [dict(sample) for sample in self.samples if (camera is None or sample['camera'] == camera)
                       and _ping_datetime.fromisoformat(sample['time']).timestamp() >= lower]
            for item in cameras:
                matching = [sample for sample in self.samples if sample['camera'] == item['name'] and sample['ip'] == item['ip']]
                item['latest'] = dict(matching[-1]) if matching else None
                item['sample_count'] = len(matching)
                item['success_count'] = sum(sample['ok'] for sample in matching)
            return {'cameras': cameras, 'interval_seconds': self.interval_seconds,
                    'monitoring': bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set()),
                    'checking': self.checking, 'samples': samples, 'started_at': self.started_at,
                    'last_checked': self.last_checked, 'last_round_started': self.last_round_started,
                    'now': _ping_iso(now), 'hours': hours, 'notice': self.notice,
                    'meaning': 'ICMP ping 응답 이력입니다. ping 실패만으로 RTSP·카메라 전원 장애를 확정할 수 없습니다.'}
# END EMBEDDED CAMERA_PING

# BEGIN EMBEDDED CAMERA_SNAPSHOTS
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
# END EMBEDDED CAMERA_SNAPSHOTS

# BEGIN EMBEDDED SWITCH_MANAGER
# Embedded before ui_server.py. No I/O, credentials, defaults or network writes on import.
import datetime as _sm_datetime
import hashlib as _sm_hashlib
import ipaddress as _sm_ipaddress
import json as _sm_json
import os as _sm_os
from pathlib import Path as _sm_Path
import re as _sm_re
import secrets as _sm_secrets
import shutil as _sm_shutil
import socket as _sm_socket
import subprocess as _sm_subprocess
import threading as _sm_threading
import time as _sm_time


class SwitchManagerError(ValueError):
    pass


class _SwitchConnectionError(SwitchManagerError):
    pass


_SM_MODEL = 'POE9728MDTL2'
_SM_FIRMWARE = 'V200SP10241024'
_SM_MAC = r'(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}'
_SM_PROMPT = rb'(?:^|[\r\n])[ \t]*([A-Za-z0-9_.:/-]{1,100})(?:\([^\r\n]*\))?[>#][ \t]*$'


def _sm_clean(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    value = _sm_re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = _sm_re.sub(r'--\s*More\s*--[ \t\x08]*', '', value, flags=_sm_re.I)
    return value.replace('\x08', '').replace('\r', '')


def _sm_redact(value):
    return '\n'.join('[인증 설정 생략]' if _sm_re.search(
        r'password|passwd|secret|community|username|authentication|radius|tacacs|authorization', line, _sm_re.I)
        else line for line in _sm_clean(str(value)).splitlines())


def _sm_mac(value):
    if not isinstance(value, str) or not _sm_re.fullmatch(_SM_MAC, value):
        raise SwitchManagerError('CPU MAC 주소는 84:E5:D8:E1:72:42 형식으로 입력하세요.')
    return value.lower().replace('-', ':')


def _sm_ip(value):
    try:
        ip = _sm_ipaddress.IPv4Address(str(value))
    except ValueError:
        raise SwitchManagerError('유효한 IPv4 주소를 입력하세요.') from None
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast or int(ip) == 0xffffffff:
        raise SwitchManagerError('장비의 실제 IPv4 주소를 입력하세요.')
    return str(ip)


def _sm_int(value, low, high, label):
    if isinstance(value, bool) or not _sm_re.fullmatch(r'[0-9]+', str(value)):
        raise SwitchManagerError(label + ' 값이 올바르지 않습니다.')
    value = int(value)
    if not low <= value <= high:
        raise SwitchManagerError('{} 범위는 {}~{}입니다.'.format(label, low, high))
    return value


def _sm_identity(raw):
    expressions = {'model': r'Device:\s*([^,\s]+)',
                   'cpu_mac': r'CPU MAC\s+(' + _SM_MAC + ')',
                   'firmware': r'SoftWare Version\s+(\S+)',
                   'serial': r'Serial Num:\s*(\S+)'}
    result = {}
    for key, expression in expressions.items():
        match = _sm_re.search(expression, _sm_clean(raw), _sm_re.I)
        result[key] = match.group(1) if match else None
    if result['cpu_mac']:
        result['cpu_mac'] = _sm_mac(result['cpu_mac'])
    return result


def _sm_config_text(raw):
    rows = _sm_clean(raw).splitlines()
    start = next((i for i, line in enumerate(rows) if line.strip() == '!'), None)
    end = next((i for i, line in enumerate(rows) if line.strip() == 'end' and start is not None and i > start), None)
    if start is None or end is None:
        raise SwitchManagerError('허브 설정 본문을 해석하지 못했습니다. 변경하지 않습니다.')
    return '\n'.join(line.rstrip() for line in rows[start:end + 1]) + '\n'


def _sm_comparable(raw):
    return '\n'.join(line.strip() for line in _sm_config_text(raw).splitlines()
                     if line.strip() and line.strip() != '!')


def _sm_sections(raw):
    result, current = {}, None
    for line in _sm_config_text(raw).splitlines():
        row = line.strip()
        match = _sm_re.fullmatch(r'interface\s+(\S+)', row, _sm_re.I)
        if match:
            current = match.group(1).lower()
            result[current] = []
        elif row in ('!', 'end'):
            current = None
        elif current:
            result[current].append(row)
    return result


def _sm_unowned_config(raw, plan):
    """Settings outside this transaction must not change or be saved implicitly."""
    owned = ('ethernet1/0/' + str(plan['before']['port']) if plan['action'] in ('port', 'vlan')
             else 'vlan' + str(plan['before']['vlan']))
    result, section = [], None
    for line in _sm_config_text(raw).splitlines():
        row = line.strip()
        match = _sm_re.fullmatch(r'interface\s+(\S+)', row, _sm_re.I)
        if match:
            section = match.group(1).lower()
        elif row in ('!', 'end'):
            section = None
        if row == '!' or not row:
            continue
        if section == owned:
            if plan['action'] == 'port' and row in ('shutdown', 'no shutdown'):
                continue
            if plan['action'] == 'vlan' and _sm_re.fullmatch(r'switchport access vlan \d+', row):
                continue
            if plan['action'] == 'management' and row.startswith('ip address '):
                continue
        if plan['action'] == 'management' and _sm_re.fullmatch(
                r'ip route (?:0\.0\.0\.0/0|0\.0\.0\.0 0\.0\.0\.0) [0-9.]+', row):
            continue
        result.append(row)
    return result


def _sm_cli_error(raw):
    pattern = _sm_re.compile(r"^\s*(?:%\s*)?(?:Invalid(?:\s|:)|Unrecognized(?:\s|:)|Unknown command\b|"
                             r"Incomplete(?:\s|:)|Ambiguous(?:\s|:)|Error(?:\s|[:,])|Failed(?:\s+to\b|:)|"
                             r"Cann?'t\b|Cannot\b|Permission denied\b|Access denied\b|Command rejected\b)", _sm_re.I)
    for row in _sm_clean(raw).splitlines():
        if _sm_re.fullmatch(r'\s*Failed\s*:\s*\d+\s*', row, _sm_re.I):
            continue
        if pattern.search(row) or _sm_re.search(r'^\s*(?:Write|Save|Saving|Command|Operation)\b.*\bfailed\b', row, _sm_re.I):
            return _sm_redact(row)
    return None


def _sm_private_write(path, value):
    path = _sm_Path(path)
    flags = _sm_os.O_WRONLY | _sm_os.O_CREAT | _sm_os.O_EXCL | getattr(_sm_os, 'O_NOFOLLOW', 0)
    fd = _sm_os.open(str(path), flags, 0o600)
    with _sm_os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(value)
        handle.flush()
        _sm_os.fsync(handle.fileno())


def _sm_ip_json(*args):
    try:
        result = _sm_subprocess.run(['ip', '-j'] + list(args), capture_output=True, text=True, timeout=8, check=True)
        return _sm_json.loads(result.stdout)
    except (OSError, ValueError, _sm_subprocess.SubprocessError):
        raise SwitchManagerError('Linux ip 명령으로 카메라 NIC 경로를 확인하지 못했습니다.') from None


def _sm_route(connection, target=None, source=None):
    target, source = target or connection['target'], source or connection['source']
    interface = connection['interface']
    links = _sm_ip_json('-d', 'link', 'show')
    by_name = {row['ifname'].split('@')[0]: row for row in links}
    by_index = {row['ifindex']: row for row in links}
    current, visited = by_name.get(interface), set()
    if current is None:
        raise SwitchManagerError('지정한 관리 인터페이스가 없습니다. enp2 또는 그 VLAN 인터페이스를 지정하세요.')
    nic = current
    while not _sm_re.fullmatch(r'enp2[0-9A-Za-z]*', current.get('ifname', '')):
        if current['ifindex'] in visited or current.get('linkinfo', {}).get('info_kind') != 'vlan':
            raise SwitchManagerError('관리 접속은 enp2 카메라 NIC 및 그 VLAN 인터페이스에서만 허용합니다. enp3/LTE는 보호됩니다.')
        visited.add(current['ifindex'])
        parent = current.get('link')
        current = by_name.get(parent) if isinstance(parent, str) else by_index.get(current.get('link_index', parent))
        if current is None:
            raise SwitchManagerError('VLAN 인터페이스의 enp2 부모를 확인하지 못했습니다.')
    addresses = _sm_ip_json('address', 'show', 'dev', interface)
    if len(addresses) != 1 or source not in [row.get('local') for row in addresses[0].get('addr_info', []) if row.get('family') == 'inet']:
        raise SwitchManagerError('지정한 소스 IP가 관리 NIC에 없습니다. 이 화면은 PC IP·Netplan을 변경하지 않습니다.')
    routes = _sm_ip_json('-4', 'route', 'get', target, 'from', source)
    if len(routes) != 1 or routes[0].get('dev') != interface or routes[0].get('gateway') or routes[0].get('type') in ('local', 'unreachable', 'blackhole', 'prohibit'):
        raise SwitchManagerError('허브로 가는 직접 enp2 경로가 없습니다. LTE 기본 경로를 사용하지 않습니다.')
    if target == source:
        raise SwitchManagerError('허브 주소와 ED 소스 주소는 달라야 합니다.')
    return {'dev': interface, 'source': source, 'parent': current['ifname'], 'local_mac': _sm_mac(nic['address'])}


def _sm_no_collision(connection, target):
    if target == connection['target']:
        return
    binary = _sm_shutil.which('arping')
    if not binary:
        raise SwitchManagerError('새 관리 IP 중복 검사에 arping이 필요합니다. Ubuntu: sudo apt install iputils-arping')
    try:
        completed = _sm_subprocess.run([binary, '-D', '-I', connection['interface'], '-c', '3', '-w', '5', target],
                                       capture_output=True, text=True, timeout=8)
    except (OSError, _sm_subprocess.SubprocessError):
        raise SwitchManagerError('새 관리 IP 중복 검사를 완료하지 못했습니다. 변경하지 않습니다.') from None
    # iputils arping -D succeeds only when no duplicate replied; errors also reject.
    if completed.returncode != 0:
        raise SwitchManagerError('새 관리 IP에 응답이 있거나 중복 검사 권한이 없습니다. 주소 충돌 여부를 확인하세요.')


class _SwitchTelnet:
    """Small Telnet reader, avoiding Python 3.13's removed telnetlib dependency."""
    def __init__(self, sock):
        self.sock, self.buffer, self.state, self.option = sock, b'', 'data', None

    def write(self, data):
        self.sock.sendall(data.replace(b'\xff', b'\xff\xff'))

    def _feed(self, raw):
        output = bytearray()
        for value in raw:
            if self.state == 'data':
                if value == 255:
                    self.state = 'iac'
                else:
                    output.append(value)
            elif self.state == 'iac':
                if value == 255:
                    output.append(value)
                    self.state = 'data'
                elif value in (251, 252, 253, 254):
                    self.option, self.state = value, 'option'
                elif value == 250:
                    self.state = 'sub'
                else:
                    self.state = 'data'
            elif self.state == 'option':
                if self.option == 251:
                    self.sock.sendall(bytes((255, 253 if value in (1, 3) else 254, value)))
                elif self.option == 253:
                    self.sock.sendall(bytes((255, 252, value)))
                self.state = 'data'
            elif self.state == 'sub' and value == 255:
                self.state = 'sub_iac'
            elif self.state == 'sub_iac':
                self.state = 'data' if value == 240 else 'sub'
        self.buffer += bytes(output)

    def expect(self, patterns, timeout):
        deadline = _sm_time.monotonic() + timeout
        while True:
            for index, pattern in enumerate(patterns):
                match = _sm_re.search(pattern, self.buffer)
                if match:
                    data, self.buffer = self.buffer[:match.end()], self.buffer[match.end():]
                    return index, match, data
            if len(self.buffer) > 4_000_000:
                raise SwitchManagerError('허브 응답 크기 제한을 초과했습니다.')
            remaining = deadline - _sm_time.monotonic()
            if remaining <= 0:
                data, self.buffer = self.buffer, b''
                return -1, None, data
            self.sock.settimeout(min(remaining, 1))
            try:
                data = self.sock.recv(65536)
            except _sm_socket.timeout:
                continue
            if not data:
                raise _SwitchConnectionError('허브 관리 연결이 끊겼습니다.')
            self._feed(data)

    def close(self):
        self.sock.close()


class _SwitchCLI:
    def __init__(self, connection, password):
        self.connection, self.password = connection, password
        self.transport, self.prompt = None, None

    def __enter__(self):
        _sm_route(self.connection)
        sock = _sm_socket.socket()
        try:
            sock.settimeout(6)
            # Binding the explicit local address plus a checked direct route works
            # without root; SO_BINDTODEVICE is additionally used where permitted.
            try:
                sock.setsockopt(_sm_socket.SOL_SOCKET, _sm_socket.SO_BINDTODEVICE,
                                (self.connection['interface'] + '\0').encode())
            except (AttributeError, PermissionError):
                pass
            sock.bind((self.connection['source'], 0))
            sock.connect((self.connection['target'], 23))
            self.transport = _SwitchTelnet(sock)
            patterns = [rb'(?i)(?:username|login):\s*$', rb'(?i)password:\s*$', _SM_PROMPT]
            n, _, _ = self.transport.expect(patterns, 7)
            if n != 0:
                raise SwitchManagerError('지원하는 허브 로그인 화면이 아닙니다.')
            self.transport.write(self.connection['username'].encode() + b'\r\n')
            n, _, _ = self.transport.expect(patterns, 7)
            if n != 1:
                raise SwitchManagerError('허브 암호 입력 화면을 확인하지 못했습니다.')
            self.transport.write(self.password.encode() + b'\r\n')
            self.password = None
            n, match, _ = self.transport.expect(patterns, 7)
            if n != 2:
                raise SwitchManagerError('허브 로그인 실패. 암호를 확인하세요. 자동 로그인 재시도는 하지 않습니다.')
            self.prompt = rb'(?:^|[\r\n])[ \t]*' + _sm_re.escape(match.group(1)) + rb'(?:\([^\r\n]*\))?[>#][ \t]*$'
            return self
        except BaseException:
            sock.close()
            self.password = None
            raise

    def command(self, command, timeout=30):
        # Caller generates commands from validated typed fields, never raw UI input.
        if not _sm_allowed_command(command):
            raise SwitchManagerError('지원하지 않는 허브 명령입니다.')
        self.transport.write(command.encode() + b'\r\n')
        deadline, output, confirmed = _sm_time.monotonic() + timeout, b'', False
        while _sm_time.monotonic() < deadline:
            n, _, chunk = self.transport.expect([
                self.prompt, rb'(?i)--\s*more\s*--[^\r\n]*$',
                rb'Confirm to overwrite current startup-config configuration \[Y/N\]:[ \t]*$',
                rb'(?i)(?:\[[yY]/[nN]\]|\(yes/no\)|are you sure)[^\r\n]*$'],
                min(2, max(0.01, deadline - _sm_time.monotonic())))
            output += chunk
            cleaned = _sm_clean(output)
            if n == 0 or _sm_re.search(self.prompt, cleaned.encode()):
                error = _sm_cli_error(cleaned)
                if error:
                    raise SwitchManagerError('허브가 명령을 거부했습니다: ' + error)
                return cleaned
            if n == 1:
                self.transport.write(b' ')
            elif n in (2, 3):
                if n != 2 or command != 'write' or confirmed:
                    self.transport.write(b'n\r\n')
                    raise SwitchManagerError('예상하지 못한 허브 확인 요청을 취소했습니다.')
                self.transport.write(b'y\r\n')
                confirmed = True
            if len(output) > 4_000_000:
                raise SwitchManagerError('허브 응답 크기 제한을 초과했습니다.')
        raise _SwitchConnectionError('허브 명령 응답 시간이 초과되었습니다: ' + command)

    def close(self):
        if self.transport:
            self.transport.close()
            self.transport = None
        self.password = None

    def __exit__(self, *unused):
        self.close()


def _sm_allowed_command(command):
    if not isinstance(command, str) or command != command.strip() or any(ord(c) < 32 for c in command):
        return False
    if command in ('show version', 'show running-config', 'show startup-config', 'show vlan',
                   'show mac-address-table', 'show reload', 'config terminal', 'exit', 'write', 'shutdown', 'no shutdown'):
        return True
    for expression in (r'(?:show (?:interface|switchport interface) |interface )Ethernet1/0/([1-9]|10)',
                       r'interface vlan (\d+)', r'switchport access vlan (\d+)'):
        match = _sm_re.fullmatch(expression, command)
        if match:
            return 1 <= int(match.group(1)) <= 4094
    match = _sm_re.fullmatch(r'ip address ([0-9.]+) ([0-9.]+)', command)
    if match:
        try:
            _sm_ipaddress.IPv4Interface(match.group(1) + '/' + match.group(2))
            return True
        except ValueError:
            return False
    match = _sm_re.fullmatch(r'(?:no )?ip route 0\.0\.0\.0 0\.0\.0\.0 ([0-9.]+)', command)
    if match:
        try:
            _sm_ip(match.group(1))
            return True
        except ValueError:
            return False
    return False


class SwitchManager:
    """NEXTU management, with explicit previews, identity pins and private backups.

    config_path is a separate switches.json path, never recording.json. Passwords
    are supplied per request, retained only during that request and never saved.
    No command changes host addresses, routes, Netplan, LTE, PoE, ring or uplinks.
    """
    def __init__(self, config_path):
        self.config_path = _sm_Path(config_path)
        self._plans = {}
        self._closing = False
        self._state_lock, self._operation_lock = _sm_threading.RLock(), _sm_threading.RLock()
        self._client_factory = _SwitchCLI  # Dependency seam for offline tests.
        self._route = _sm_route
        self._collision = _sm_no_collision
        self._sleep = _sm_time.sleep

    def _begin_operation(self, busy_message):
        if not self._operation_lock.acquire(False):
            raise SwitchManagerError(busy_message)
        with self._state_lock:
            closing = self._closing
        if closing:
            self._operation_lock.release()
            raise SwitchManagerError('프로그램을 종료하고 있습니다. 새 허브 작업을 시작하지 않습니다.')

    def close(self):
        """Drain active writes/rollback before daemon HTTP handlers may exit.

        Do not hold the state lock while waiting: an active apply may need it to
        save its new management endpoint or complete recovery.
        """
        with self._state_lock:
            self._closing = True
            self._plans.clear()
        with self._operation_lock:
            pass

    def _load(self):
        if self.config_path.is_symlink():
            raise SwitchManagerError('허브 설정 파일은 심볼릭 링크를 사용할 수 없습니다.')
        if not self.config_path.exists():
            return {'switches': []}
        try:
            if self.config_path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError()
            value = _sm_json.loads(self.config_path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError()
            rows = value.get('switches')
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError()
            return {'switches': [self._validate_connection(row) for row in rows]}
        except (OSError, ValueError, TypeError):
            raise SwitchManagerError('허브 설정 파일을 읽지 못했습니다.') from None

    def get_config(self):
        with self._state_lock:
            return self._load()

    def _validate_connection(self, payload):
        if not isinstance(payload, dict):
            raise SwitchManagerError('허브 연결 설정이 올바르지 않습니다.')
        identity = str(payload.get('id') or _sm_secrets.token_hex(8))
        if not _sm_re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identity):
            raise SwitchManagerError('허브 ID 형식이 올바르지 않습니다.')
        name = str(payload.get('name', '')).strip()
        if not 1 <= len(name) <= 100 or any(ord(c) < 32 for c in name):
            raise SwitchManagerError('허브 이름을 1~100자로 입력하세요.')
        interface = str(payload.get('interface', ''))
        if not _sm_re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', interface):
            raise SwitchManagerError('Linux 관리 NIC 이름을 입력하세요. 예: enp2s0f1, nxmg100')
        username = str(payload.get('username', ''))
        if not _sm_re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
            raise SwitchManagerError('허브 관리 아이디를 입력하세요.')
        mac = payload.get('expected_cpu_mac') or ''
        if mac:
            mac = _sm_mac(mac)
        ports = payload.get('camera_ports', [])
        if isinstance(ports, str):
            ports = [item.strip() for item in ports.split(',') if item.strip()]
        if not isinstance(ports, list):
            raise SwitchManagerError('카메라 포트는 번호 목록으로 입력하세요.')
        ports = sorted(set(_sm_int(p, 1, 8, '카메라 포트') for p in ports))
        result = {'id': identity, 'name': name, 'target': _sm_ip(payload.get('target')),
                  'source': _sm_ip(payload.get('source')), 'interface': interface,
                  'username': username, 'expected_cpu_mac': mac, 'camera_ports': ports}
        if result['source'] == result['target']:
            raise SwitchManagerError('허브 IP와 ED 소스 IP가 같습니다.')
        return result

    def save_connection(self, payload):
        self._begin_operation('허브 작업 중에는 연결 설정을 저장할 수 없습니다.')
        try:
            return self._save_connection(payload)
        finally:
            self._operation_lock.release()

    def _save_connection(self, payload):
        # Only this allowlist reaches disk; input passwords are deliberately ignored.
        connection = self._validate_connection(payload)
        with self._state_lock:
            configuration = self._load()
            rows = [row for row in configuration['switches'] if row['id'] != connection['id']]
            if len(rows) >= 100:
                raise SwitchManagerError('등록 가능한 허브 수를 초과했습니다.')
            rows.append(connection)
            directory = self.config_path.parent
            directory.mkdir(parents=True, exist_ok=True)
            if self.config_path.is_symlink():
                raise SwitchManagerError('심볼릭 링크 설정 파일은 사용할 수 없습니다.')
            temporary = directory / ('.switches-' + _sm_secrets.token_hex(12) + '.tmp')
            try:
                _sm_private_write(temporary, _sm_json.dumps({'switches': rows}, ensure_ascii=False, indent=2) + '\n')
                _sm_os.replace(str(temporary), str(self.config_path))
            finally:
                if temporary.exists():
                    temporary.unlink()
            # A previous preview is invalid once connection settings are edited.
            self._plans = {key: plan for key, plan in self._plans.items() if plan['connection']['id'] != connection['id']}
        return {'switches': rows, 'connection': connection}

    def _connection(self, payload):
        if not isinstance(payload, dict):
            raise SwitchManagerError('허브 요청 형식이 올바르지 않습니다.')
        switch_id = payload.get('switch_id')
        for row in self.get_config()['switches']:
            if row['id'] == switch_id:
                return row
        raise SwitchManagerError('등록한 허브를 선택하세요.')

    def _password(self, payload):
        if not isinstance(payload, dict):
            raise SwitchManagerError('허브 요청 형식이 올바르지 않습니다.')
        password = payload.get('password')
        if not isinstance(password, str) or len(password) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in password):
            raise SwitchManagerError('허브 암호를 입력하세요. 암호는 저장하지 않습니다.')
        return password

    def _check_identity(self, identity, connection, require_pin=False):
        pin = connection['expected_cpu_mac']
        if pin and identity.get('cpu_mac') != pin:
            raise SwitchManagerError('저장한 CPU MAC과 실제 허브가 다릅니다. 다른 장비이므로 중단합니다.')
        if require_pin and not pin:
            raise SwitchManagerError('조회된 CPU MAC을 연결 설정에 저장한 뒤 변경 미리보기를 실행하세요.')
        return identity.get('model') == _SM_MODEL and identity.get('firmware') == _SM_FIRMWARE

    def _snapshot(self, client, connection, route):
        identity = _sm_identity(client.command('show version'))
        supported = self._check_identity(identity, connection)
        result = {'switch_id': connection['id'], 'connection': dict(connection), 'identity': identity,
                  'route': route, 'supported': supported, 'ports': [], 'management': None,
                  'vlans': [], 'ed_ports': [], 'can_modify': False, 'warnings': []}
        if not supported:
            result['warnings'].append('이 모델/펌웨어의 쓰기 명령은 검증되지 않았습니다. 장비 식별 정보만 조회했습니다.')
            return result, {}
        raw = {command: client.command(command) for command in (
            'show running-config', 'show startup-config', 'show vlan', 'show mac-address-table', 'show reload')}
        sections = _sm_sections(raw['show running-config'])
        result['vlans'] = sorted(set(int(value) for value in _sm_re.findall(r'^\s*(\d+)\s+\S+\s+Static\b', raw['show vlan'], _sm_re.M)))
        for row in _sm_clean(raw['show mac-address-table']).splitlines():
            match = _sm_re.search(_SM_MAC, row)
            if match and _sm_mac(match.group()) == route['local_mac']:
                result['ed_ports'].extend(int(p) for p in _sm_re.findall(r'Ethernet1/0/(\d+)\b', row, _sm_re.I))
        result['ed_ports'] = sorted(set(result['ed_ports']))
        managements = []
        for name, rows in sections.items():
            match = _sm_re.fullmatch(r'vlan(\d+)', name)
            if match:
                for row in rows:
                    ip = _sm_re.fullmatch(r'ip address ([0-9.]+) ([0-9.]+)', row)
                    if ip:
                        managements.append({'vlan': int(match.group(1)), 'ip': ip.group(1), 'netmask': ip.group(2)})
        management = [row for row in managements if row['ip'] == connection['target']]
        gateways = _sm_re.findall(r'^\s*ip route (?:0\.0\.0\.0/0|0\.0\.0\.0 0\.0\.0\.0) ([0-9.]+)\s*$',
                                 _sm_clean(raw['show running-config']), _sm_re.M)
        if len(management) == 1:
            result['management'] = dict(management[0], gateway=gateways[0] if len(gateways) == 1 else '',
                                        gateway_count=len(gateways))
        managed_vlans = {row['vlan'] for row in managements}
        control_vlans = {int(value) for value in _sm_re.findall(
            r'^\s*control-vlan\s+(\d+)\b', _sm_clean(raw['show running-config']), _sm_re.M | _sm_re.I)}
        result['protected_vlans'] = sorted(managed_vlans | control_vlans)
        result['camera_vlans'] = [vlan for vlan in result['vlans'] if vlan not in result['protected_vlans']]
        ed_known = len(result['ed_ports']) == 1 and 1 <= result['ed_ports'][0] <= 8
        for port in range(1, 11):
            interface = client.command('show interface Ethernet1/0/{}'.format(port))
            switchport = client.command('show switchport interface Ethernet1/0/{}'.format(port))
            raw['interface{}'.format(port)], raw['switchport{}'.format(port)] = interface, switchport
            rows = sections.get('ethernet1/0/{}'.format(port))
            mode_match = _sm_re.search(r'^\s*Mode\s*:\s*(\w+)', switchport, _sm_re.M | _sm_re.I)
            pvid_match = _sm_re.search(r'^\s*Port VID\s*:\s*(\d+)', switchport, _sm_re.M | _sm_re.I)
            mode = mode_match.group(1).lower() if mode_match else 'unknown'
            pvid = int(pvid_match.group(1)) if pvid_match else None
            state = _sm_re.search(r'Ethernet1/0/\d+ is ([^,\r\n]+), line protocol is (\w+)', interface, _sm_re.I)
            reasons = []
            if port >= 9:
                reasons.append('광 업링크')
            if port in result['ed_ports']:
                reasons.append('ED 관리 연결')
            if not ed_known:
                reasons.append('ED 연결 포트를 단일 구리 포트로 식별하지 못함')
            if mode != 'access':
                reasons.append('트렁크/하이브리드 또는 미확인 포트')
            if pvid is None or pvid in managed_vlans or pvid in control_vlans:
                reasons.append('관리 VLAN 또는 VLAN 미확인')
            if rows is None:
                reasons.append('포트 설정 미확인')
            elif any(_sm_re.search(r'erps|channel-group|aggregation|port-channel', row, _sm_re.I) for row in rows):
                reasons.append('링 또는 묶음 연결')
            if port not in connection['camera_ports']:
                reasons.append('카메라 포트로 등록하지 않음')
            # More than one learned MAC indicates a downstream switch or unknown role.
            macs = set()
            for line in _sm_clean(raw['show mac-address-table']).splitlines():
                if _sm_re.search(r'Ethernet1/0/{}\b'.format(port), line, _sm_re.I):
                    for value in _sm_re.findall(_SM_MAC, line):
                        macs.add(_sm_mac(value))
            if len(macs) > 1:
                reasons.append('여러 장비의 MAC이 학습된 포트')
            result['ports'].append({'port': port, 'link': state.group(2).lower() if state else 'unknown',
                                    'admin_up': None if rows is None else 'shutdown' not in rows,
                                    'mode': mode, 'pvid': pvid, 'protected': bool(reasons), 'reasons': reasons,
                                    'editable': not reasons, 'mac_count': len(macs)})
        if not connection['expected_cpu_mac']:
            result['warnings'].append('CPU MAC을 저장하기 전에는 설정을 변경할 수 없습니다.')
        if not ed_known:
            result['warnings'].append('ED 포트가 명확하지 않아 모든 설정 변경을 차단했습니다.')
        if _sm_comparable(raw['show running-config']) != _sm_comparable(raw['show startup-config']):
            result['warnings'].append('실행 설정과 저장 설정이 다릅니다. 기존 미저장 변경을 먼저 확인해야 합니다.')
        if 'not configured' not in raw['show reload'].lower():
            result['warnings'].append('기존 재부팅 예약 상태를 확인해야 합니다. 이 도구는 재부팅을 예약하거나 취소하지 않습니다.')
        result['can_modify'] = bool(connection['expected_cpu_mac'] and ed_known and not result['warnings'])
        return result, raw

    def inspect(self, payload):
        connection, password = self._connection(payload), self._password(payload)
        self._begin_operation('다른 허브 작업이 진행 중입니다. 완료 후 다시 조회하세요.')
        try:
            route = self._route(connection)
            with self._client_factory(connection, password) as client:
                return self._snapshot(client, connection, route)[0]
        finally:
            password = None
            self._operation_lock.release()

    def _build_change(self, payload, snapshot, connection):
        action = payload.get('action')
        before, after, commands, inverse = {}, {}, [], []
        warnings = ['적용 전에 원래 설정을 비공개 백업합니다. enp3/LTE·PC IP·Netplan은 변경하지 않습니다.']
        next_connection = dict(connection)
        if action in ('port', 'vlan'):
            port = _sm_int(payload.get('port'), 1, 8, '포트')
            current = next((row for row in snapshot['ports'] if row['port'] == port), None)
            if not current or not current['editable']:
                raise SwitchManagerError('보호된 포트는 변경할 수 없습니다: ' + ', '.join(current['reasons'] if current else ['포트 미확인']))
            before = {'port': port, 'admin_up': current['admin_up'], 'vlan': current['pvid']}
            if action == 'port':
                state = payload.get('state')
                if state not in ('on', 'off', 'bounce'):
                    raise SwitchManagerError('포트 상태는 on, off, bounce 중 하나여야 합니다.')
                if state == 'bounce' and not current['admin_up']:
                    raise SwitchManagerError('이미 꺼진 포트는 먼저 켜기를 선택하세요.')
                if state != 'bounce' and current['admin_up'] == (state == 'on'):
                    raise SwitchManagerError('이미 요청한 포트 상태입니다.')
                after = {'port': port, 'admin_up': state != 'off', 'state': state, 'bounce_seconds': 10 if state == 'bounce' else 0}
                commands = ['shutdown', 'no shutdown'] if state == 'bounce' else ['no shutdown' if state == 'on' else 'shutdown']
                inverse = ['no shutdown' if current['admin_up'] else 'shutdown']
                warnings.append('포트 차단 중 해당 카메라의 녹화·센터 영상이 끊깁니다. 데이터 링크 제어이며 카메라 전원 재인가/PoE 제어가 아닙니다.')
            else:
                vlan = _sm_int(payload.get('vlan'), 1, 4094, 'VLAN')
                if vlan not in snapshot['vlans']:
                    raise SwitchManagerError('이미 생성된 VLAN만 선택할 수 있습니다. 광 업링크/VLAN 생성은 이 화면에서 변경하지 않습니다.')
                if vlan == current['pvid']:
                    raise SwitchManagerError('이미 지정한 VLAN입니다.')
                if vlan in snapshot.get('protected_vlans', []):
                    raise SwitchManagerError('카메라 포트를 허브 관리 또는 링 제어 VLAN으로 옮길 수 없습니다.')
                after = {'port': port, 'vlan': vlan}
                commands, inverse = ['switchport access vlan {}'.format(vlan)], ['switchport access vlan {}'.format(current['pvid'])]
                warnings.append('대상 VLAN에 ED·센터 경로가 없으면 카메라 접속이 끊깁니다. 광 업링크와 ERPS 설정은 변경하지 않습니다.')
            prefix, suffix = ['config terminal', 'interface Ethernet1/0/{}'.format(port)], ['exit', 'exit']
            commands, inverse = prefix + commands + suffix, prefix + inverse + suffix
        elif action == 'management':
            current = snapshot.get('management')
            if not current or current['gateway_count'] > 1:
                raise SwitchManagerError('단일 관리 IP와 기본 게이트웨이를 식별하지 못했습니다.')
            address, mask = _sm_ip(payload.get('ip')), str(payload.get('netmask', ''))
            gateway = _sm_ip(payload.get('gateway')) if payload.get('gateway') else ''
            try:
                oldnet = _sm_ipaddress.IPv4Network(current['ip'] + '/' + current['netmask'], strict=False)
                newnet = _sm_ipaddress.IPv4Network(address + '/' + mask, strict=False)
            except ValueError:
                raise SwitchManagerError('연속된 IPv4 서브넷 마스크를 입력하세요.') from None
            source = _sm_ip(payload.get('new_source') or connection['source'])
            if not 1 <= newnet.prefixlen <= 30 or any(_sm_ipaddress.IPv4Address(value) in (newnet.network_address, newnet.broadcast_address) for value in (address, source)):
                raise SwitchManagerError('관리 IP·소스 IP·마스크 조합이 올바르지 않습니다.')
            if _sm_ipaddress.IPv4Address(address) not in oldnet or _sm_ipaddress.IPv4Address(current['ip']) not in newnet:
                raise SwitchManagerError('관리 연결 보호를 위해 기존 관리 대역 안에서만 IP·마스크를 변경할 수 있습니다.')
            if _sm_ipaddress.IPv4Address(source) not in newnet or _sm_ipaddress.IPv4Address(source) not in oldnet:
                raise SwitchManagerError('이미 설정된 ED 관리 소스 IP가 변경 전후 관리 대역에 모두 포함되어야 합니다.')
            if gateway and (_sm_ipaddress.IPv4Address(gateway) not in newnet or gateway in (address, source) or _sm_ipaddress.IPv4Address(gateway) in (newnet.network_address, newnet.broadcast_address)):
                raise SwitchManagerError('게이트웨이는 새 관리 대역 내의 다른 호스트 주소여야 합니다.')
            before = {key: current[key] for key in ('vlan', 'ip', 'netmask', 'gateway')}
            after = {'vlan': current['vlan'], 'ip': address, 'netmask': str(newnet.netmask), 'gateway': gateway}
            if before == after:
                raise SwitchManagerError('변경할 관리 설정이 없습니다.')
            next_connection.update(target=address, source=source)
            self._route(next_connection)
            self._collision(connection, address)
            def management_commands(old, new):
                result = ['config terminal']
                if old['gateway'] != new['gateway']:
                    if old['gateway']:
                        result.append('no ip route 0.0.0.0 0.0.0.0 ' + old['gateway'])
                    if new['gateway']:
                        result.append('ip route 0.0.0.0 0.0.0.0 ' + new['gateway'])
                result += ['interface vlan {}'.format(new['vlan']), 'ip address {} {}'.format(new['ip'], new['netmask']), 'exit', 'exit']
                return result
            commands, inverse = management_commands(before, after), management_commands(after, before)
            warnings.append('관리 IP 변경 후 새 주소로 다시 접속해 CPU MAC을 확인합니다. 재접속에 실패하면 기존 설정 복구를 시도하며, 접속 자체가 사라지면 원격 복구를 보장할 수 없습니다.')
        else:
            raise SwitchManagerError('지원하는 변경은 port, vlan, management입니다.')
        if not all(_sm_allowed_command(c) for c in commands + inverse):
            raise SwitchManagerError('검증되지 않은 명령이 포함되어 변경할 수 없습니다.')
        return before, after, commands, inverse, next_connection, warnings

    def plan(self, payload):
        connection, password = self._connection(payload), self._password(payload)
        self._begin_operation('다른 허브 작업이 진행 중입니다.')
        try:
            route = self._route(connection)
            with self._client_factory(connection, password) as client:
                snapshot, raw = self._snapshot(client, connection, route)
            self._check_identity(snapshot['identity'], connection, require_pin=True)
            if not snapshot['can_modify']:
                raise SwitchManagerError('설정을 변경할 수 없습니다: ' + '; '.join(snapshot['warnings']))
            before, after, commands, inverse, next_connection, warnings = self._build_change(payload, snapshot, connection)
            now, token = _sm_time.time(), _sm_secrets.token_urlsafe(32)
            public = {'plan_id': token, 'expires_at': now + 300, 'action': payload['action'], 'switch_id': connection['id'],
                      'before': before, 'after': after, 'commands': commands, 'warnings': warnings}
            with self._state_lock:
                self._plans = {key: plan for key, plan in self._plans.items() if plan['expires_at'] > now}
                if len(self._plans) >= 50:
                    raise SwitchManagerError('미리보기가 너무 많습니다. 잠시 후 다시 시도하세요.')
                self._plans[token] = dict(public, connection=connection, next_connection=next_connection,
                                          fingerprint=_sm_hashlib.sha256(_sm_comparable(raw['show running-config']).encode()).hexdigest(),
                                          inverse=inverse, identity=snapshot['identity'],
                                          request={key: payload[key] for key in ('action', 'port', 'state', 'vlan', 'ip', 'netmask', 'gateway', 'new_source') if key in payload})
            return public
        finally:
            password = None
            self._operation_lock.release()

    def _backup(self, snapshot, raw, plan):
        base = self.config_path.parent / 'switch_backups'
        base.mkdir(mode=0o700, exist_ok=True)
        if base.is_symlink():
            raise SwitchManagerError('백업 폴더에 심볼릭 링크를 사용할 수 없습니다.')
        _sm_os.chmod(str(base), 0o700)
        directory = base / (_sm_datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + _sm_secrets.token_hex(8))
        directory.mkdir(mode=0o700)
        for key in ('show running-config', 'show startup-config'):
            _sm_private_write(directory / (key.replace(' ', '_') + '.txt'), raw[key])
        public = {key: plan[key] for key in ('switch_id', 'action', 'before', 'after', 'commands', 'warnings')}
        _sm_private_write(directory / 'plan.json', _sm_json.dumps(public, ensure_ascii=False, indent=2) + '\n')
        _sm_private_write(directory / 'inspection.json', _sm_json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
        return directory

    def _verify_change(self, snapshot, plan, reverting=False):
        expected = plan['before'] if reverting else plan['after']
        if snapshot['identity'] != plan['identity']:
            raise SwitchManagerError('적용 후 허브 식별정보가 달라졌습니다.')
        if plan['action'] == 'management':
            current = snapshot.get('management') or {}
            if any(current.get(key) != expected.get(key) for key in ('vlan', 'ip', 'netmask', 'gateway')):
                raise SwitchManagerError('관리 설정 적용 결과가 미리보기와 다릅니다.')
        else:
            current = next(row for row in snapshot['ports'] if row['port'] == expected['port'])
            key = 'vlan' if plan['action'] == 'vlan' else 'admin_up'
            if current['pvid' if key == 'vlan' else key] != expected[key]:
                raise SwitchManagerError('포트 설정 적용 결과가 미리보기와 다릅니다.')

    def _send(self, client, commands, bounce=False):
        for command in commands:
            client.command(command)
            if bounce and command == 'shutdown':
                self._sleep(10)

    def apply(self, payload, password=None, confirm=False):
        if isinstance(payload, str):
            payload = {'plan_id': payload, 'password': password, 'confirm': confirm}
        password = self._password(payload)
        if payload.get('confirm') is not True:
            raise SwitchManagerError('미리보기 내용을 확인한 뒤 적용을 선택하세요.')
        self._begin_operation('다른 허브 작업이 진행 중입니다.')
        client, backup, changed, plan, saved = None, None, False, None, False
        try:
            with self._state_lock:
                plan = self._plans.pop(payload.get('plan_id'), None)
            if not plan or plan['expires_at'] < _sm_time.time():
                raise SwitchManagerError('미리보기가 만료되었거나 이미 사용되었습니다. 다시 조회해 미리보기를 만드세요.')
            if self._connection({'switch_id': plan['switch_id']}) != plan['connection']:
                raise SwitchManagerError('연결 설정이 변경되었습니다. 다시 미리보기를 만드세요.')
            connection = plan['connection']
            route = self._route(connection)
            client = self._client_factory(connection, password).__enter__()
            snapshot, raw = self._snapshot(client, connection, route)
            if snapshot['identity'] != plan['identity'] or not snapshot['can_modify']:
                raise SwitchManagerError('장비 또는 보호 상태가 달라졌습니다. 다시 미리보기를 만드세요.')
            fingerprint = _sm_hashlib.sha256(_sm_comparable(raw['show running-config']).encode()).hexdigest()
            if fingerprint != plan['fingerprint']:
                raise SwitchManagerError('미리보기 후 허브 설정이 바뀌었습니다. 다시 확인하세요.')
            # Re-evaluate protected ports, routes and duplicate addresses immediately before writes.
            self._build_change(plan['request'], snapshot, connection)
            backup = self._backup(snapshot, raw, plan)
            changed = True  # Partial command failure must still enter recovery.
            try:
                self._send(client, plan['commands'], plan['action'] == 'port' and plan['after'].get('state') == 'bounce')
            except (_SwitchConnectionError, OSError):
                # Changing the local SVI address can close the established TCP
                # session before its final prompt. Only a fresh, pinned login
                # and an exact settings comparison may turn that into success.
                if plan['action'] != 'management' or plan['before']['ip'] == plan['after']['ip']:
                    raise
            if plan['action'] == 'management':
                client.close()
                client = None
                client = self._client_factory(plan['next_connection'], password).__enter__()
            next_connection = plan['next_connection']
            after, after_raw = self._snapshot(client, next_connection, self._route(next_connection))
            self._verify_change(after, plan)
            if _sm_unowned_config(raw['show running-config'], plan) != _sm_unowned_config(after_raw['show running-config'], plan):
                raise SwitchManagerError('선택한 항목 외의 설정이 변경되었습니다. 저장을 중단하고 원래 항목을 복구합니다.')
            client.command('write', timeout=60)
            saved = True
            running, startup = client.command('show running-config'), client.command('show startup-config')
            if _sm_comparable(running) != _sm_comparable(startup):
                raise SwitchManagerError('실행 설정과 저장본의 일치를 확인하지 못했습니다.')
            # The pre-save snapshot intentionally reports unsaved changes. Return
            # a new post-save view so a successful apply does not disable the UI.
            after, final_raw = self._snapshot(client, next_connection, self._route(next_connection))
            self._verify_change(after, plan)
            if _sm_unowned_config(raw['show running-config'], plan) != _sm_unowned_config(final_raw['show running-config'], plan):
                raise SwitchManagerError('저장 확인 중 다른 설정 변경이 감지되었습니다.')
            if plan['action'] == 'management':
                self._save_connection(next_connection)
            _sm_private_write(backup / 'after_running.txt', running)
            result = {'status': 'applied', 'backup_path': str(backup), 'detail': '설정을 적용하고 저장본까지 확인했습니다.', 'inspection': after}
            _sm_private_write(backup / 'result.json', _sm_json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            return result
        except Exception as exc:
            if not changed or plan is None:
                if isinstance(exc, SwitchManagerError):
                    raise
                raise SwitchManagerError('허브 조회에 실패했습니다. 설정은 변경하지 않았습니다.') from None
            # Restore only the exact fields this plan owns, never replay a whole backup.
            restored, recovery_error = False, ''
            recovery_connections = [plan['next_connection']]
            if plan['connection'] != plan['next_connection']:
                recovery_connections.append(plan['connection'])
            for recovery in recovery_connections:
                try:
                    if client:
                        client.close()
                    client = None
                    self._route(recovery)
                    client = self._client_factory(recovery, password).__enter__()
                    identity = _sm_identity(client.command('show version'))
                    if identity != plan['identity']:
                        raise SwitchManagerError('복구 대상 CPU MAC 또는 장비 정보가 다릅니다.')
                    try:
                        self._send(client, plan['inverse'])
                    except (_SwitchConnectionError, OSError):
                        if plan['action'] != 'management' or plan['before']['ip'] == plan['after']['ip']:
                            raise
                    if plan['action'] == 'management':
                        client.close()
                        client = None
                        client = self._client_factory(plan['connection'], password).__enter__()
                    original, recovered_raw = self._snapshot(client, plan['connection'], self._route(plan['connection']))
                    self._verify_change(original, plan, reverting=True)
                    if _sm_comparable(recovered_raw['show running-config']) != _sm_comparable(raw['show running-config']):
                        raise SwitchManagerError('요청한 항목은 복구했지만 다른 설정 변경이 있어 전체 저장을 수행하지 않습니다.')
                    client.command('write', timeout=60)
                    if _sm_comparable(client.command('show running-config')) != _sm_comparable(client.command('show startup-config')):
                        raise SwitchManagerError('복구 저장본을 확인하지 못했습니다.')
                    if plan['action'] == 'management':
                        self._save_connection(plan['connection'])
                    restored = True
                    break
                except Exception as recovery_exc:
                    recovery_error = str(recovery_exc) if isinstance(recovery_exc, SwitchManagerError) else '복구 접속 또는 저장 실패'
            reason = str(exc) if isinstance(exc, SwitchManagerError) else '허브 접속 또는 저장 중 오류'
            result = {'status': 'rolled_back' if restored else 'failed', 'backup_path': str(backup),
                      'detail': reason + (' 원래 설정으로 복구하고 저장했습니다.' if restored else ' 원격 복구를 확인하지 못했습니다. ' + recovery_error),
                      'rollback_verified': restored, 'reboot_scheduled': False}
            if backup:
                try:
                    _sm_private_write(backup / 'result.json', _sm_json.dumps(result, ensure_ascii=False, indent=2) + '\n')
                except OSError:
                    pass
            return result
        finally:
            password = None
            try:
                if client:
                    try:
                        client.close()
                    except OSError:
                        # A disconnected socket during final cleanup must not
                        # mask a verified result or strand the operation lock.
                        pass
            finally:
                self._operation_lock.release()
# END EMBEDDED SWITCH_MANAGER

# BEGIN EMBEDDED UI_SERVER
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


def recording_folder_parts(relative):
    if (not isinstance(relative, str) or len(relative) > 4096 or "\\" in relative
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)):
        raise ValueError("영상 폴더 경로가 올바르지 않습니다.")
    parts = relative.split("/") if relative else []
    if len(parts) > 31 or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        raise ValueError("저장 폴더 안의 일반 폴더만 사용할 수 있습니다.")
    return parts


def recording_folder_id(relative, root_info, folder_info):
    recording_folder_parts(relative)
    value = {"path": relative, "root": [root_info.st_dev, root_info.st_ino],
             "folder": [folder_info.st_dev, folder_info.st_ino], "kind": "folder", "version": 1}
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")


@contextmanager
def recording_open_folder(root_fd, identifier):
    try:
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,8192}", identifier):
            raise ValueError()
        raw = base64.b64decode(identifier + "=" * (-len(identifier) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
        root_info = os.fstat(root_fd)
        if (not isinstance(value, dict) or value.get("version") != 1 or value.get("kind") != "folder"
                or value.get("root") != [root_info.st_dev, root_info.st_ino]
                or not isinstance(value.get("folder"), list) or len(value["folder"]) != 2
                or any(isinstance(number, bool) or not isinstance(number, int) for number in value["folder"])):
            raise ValueError()
        relative = value["path"]
        parts = recording_folder_parts(relative)
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise ValueError("영상 폴더가 변경되었거나 식별자가 올바르지 않습니다. 목록을 새로고침하세요.") from None
    try:
        with recording_directory(root_fd, parts) as folder_fd:
            info = os.fstat(folder_fd)
            if [info.st_dev, info.st_ino] != value["folder"]:
                raise ValueError("영상 폴더가 변경되었습니다. 목록을 새로고침하세요.")
            yield folder_fd, relative
    except (FileNotFoundError, NotADirectoryError):
        raise ValueError("영상 폴더가 없어졌거나 변경되었습니다. 목록을 새로고침하세요.") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("심볼릭 링크 폴더는 열 수 없습니다. 목록을 새로고침하세요.") from None
        raise


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


def recording_folder_walk(root_fd, prefix=()):
    """List the real directory hierarchy, including empty directories, without links."""
    yield "/".join(prefix), os.fstat(root_fd)
    if len(prefix) >= 31:
        return
    for name in sorted(os.listdir(root_fd)):
        if name.startswith(".") or "/" in name or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            continue
        try:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                with recording_directory(root_fd, (name,)) as child:
                    yield from recording_folder_walk(child, prefix + (name,))
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
    return {"mode": mode, "ips": "", "username": "", "password": "",
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
    if cameras == []:
        return camera_connection_defaults("direct")
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
    username, password = connection.get("username", ""), connection.get("password", "")
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
        ("Weather region", "날씨 지역 목록에서 시·도와 시·군·구를 선택하세요."),
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


CAMERA_SETUP_MESSAGE = "장비 설정에서 카메라 IP와 필요한 계정을 입력하거나 기존 setup.ini를 선택한 뒤 설정을 저장하세요."


def camera_setup_required(data):
    """Only the explicit empty-list draft is an unfinished first-run setup."""
    return isinstance(data, dict) and data.get("cameras") == []


class RecorderUiController:
    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser().resolve()
        self.action_lock = threading.RLock()
        self.settings_lock = threading.RLock()
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
        self.live_view = LiveViewManager(self.settings)
        self.camera_snapshots = CameraSnapshotManager(self.settings, self.config_path)
        self.switch_manager = SwitchManager(self.config_path.parent / "switches.json")
        self.camera_profiles = CameraProfileManager(self)
        self.camera_ping = CameraPingMonitor(self.settings, self.config_path)
        self.shared_browser_server = None

    def use_recording_profile(self, camera_name, profile, revision):
        if str(profile) not in ("2", "4"):
            raise ValueError("녹화 프로필은 2번 또는 4번을 선택하세요.")
        with self.action_lock:
            if self.closing:
                raise ValueError("화면을 종료하고 있습니다.")
            state = self.state()
            if state.get("running") or state.get("external_running"):
                raise ValueError("녹화를 중지한 뒤 녹화 프로필을 변경하세요. 완료 후 예약 녹화를 다시 시작하세요.")
            data, current = self.read_config()
            if revision != current:
                raise ValueError("설정이 변경되었습니다. 저장된 설정을 다시 불러오세요.")
            cameras = data.get("cameras")
            if not isinstance(cameras, list):
                raise ValueError("장비 설정에서 카메라 IP 직접 입력으로 저장한 뒤 변경하세요.")
            matches = [c for c in cameras if isinstance(c, dict) and c.get("name") == camera_name]
            if len(matches) != 1:
                raise ValueError("저장된 카메라를 하나 선택하세요.")
            camera = matches[0]
            details = _direct_camera_details(camera)
            if not details or not re.fullmatch(r"/profile[1-9][0-9]*/media\.smp", details["path"]):
                raise ValueError("Wisenet 프로필 주소를 사용하는 직접 IP 설정에서 변경할 수 있습니다.")
            snapshot = self.camera_profiles.read(camera_name)
            profiles = snapshot.get("profiles", {})
            if str(profile) not in profiles:
                raise ValueError("카메라에 선택한 프로필이 없습니다. 먼저 카메라 프로필을 생성하세요.")
            parsed = urlsplit(camera["url"])
            camera["url"] = parsed._replace(path="/profile" + str(profile) + "/media.smp").geturl()
            settings_from_data(data, self.config_path.parent)
            if self.read_config()[1] != current:
                raise ValueError("검사 중 설정이 변경되었습니다. 다시 불러오세요.")
            original, _ = self.read_config()
            backup = self.config_path.with_name(".recording-profile-backup_" + uuid.uuid4().hex + ".json")
            atomic_private_json(backup, original)
            atomic_private_json(self.config_path, data)
            return {"message": "선택한 카메라의 녹화 주소를 프로필 " + str(profile) + "번으로 저장했습니다. 예약 녹화를 다시 시작하세요.",
                    "revision": self.read_config()[1]}

    def open_browser_controls(self):
        with self.action_lock:
            self.camera_ping.start()
            if self.shared_browser_server is None:
                for port in range(8765, 8775):
                    try:
                        handler = make_ui_handler(self, secrets.token_urlsafe(32), port)
                        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
                        break
                    except OSError as exc:
                        if exc.errno != errno.EADDRINUSE:
                            raise
                else:
                    raise ValueError("관리 화면 포트가 사용 중입니다.")
                server.daemon_threads = True
                self.shared_browser_server = server
                threading.Thread(target=server.serve_forever, daemon=True).start()
            port = self.shared_browser_server.server_address[1]
            webbrowser.open("http://127.0.0.1:" + str(port) + "/#live")
            return {"message": "실시간 보기 · 프로필 관리 화면을 브라우저에서 열었습니다."}

    def read_config(self):
        if self.config_path.is_symlink() or self.config_path.stat().st_size > 1024 * 1024:
            raise ValueError("설정 파일은 1MB 이하의 일반 JSON 파일이어야 합니다.")
        raw = self.config_path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("설정 파일은 JSON 객체여야 합니다.")
        return data, hashlib.sha256(raw).hexdigest()

    def settings(self, refresh=False):
        # Ping/history requests run concurrently with UI operations. Publish one
        # complete settings snapshot without an older parse replacing a newer one.
        # Keep this separate from action_lock: close() joins the ping thread.
        with self.settings_lock:
            data, revision = self.read_config()
            if camera_setup_required(data):
                raise ValueError(CAMERA_SETUP_MESSAGE)
            if refresh or self.cached_settings is None or revision != self.cached_revision:
                self.cached_settings = settings_from_data(data, self.config_path.parent)
                self.cached_revision = revision
                self.preview = None
            return self.cached_settings

    def configuration(self):
        data, revision = self.read_config()
        safe = {key: data[key] for key in ("setup_ini", "output_dir", "intersection_name", "weather", "recording_dates",
                 "time_ranges", "recording_mode", "segment_minutes", "retention_days", "min_free_gb", "weather_condition") if key in data}
        safe.setdefault("weather_condition", default_weather_config())
        if isinstance(safe["weather_condition"], dict):
            safe["weather_condition"] = {key: value for key, value in safe["weather_condition"].items()
                                         if key in ("enabled", "region", "conditions", "provider", "api_key_file", "poll_minutes", "met_contact")}
        safe.setdefault("setup_ini", "auto")
        safe.setdefault("output_dir", "auto")
        safe.setdefault("segment_minutes", data.get("segment_seconds", 1800) / 60)
        safe.setdefault("recording_mode", "split" if "segment_minutes" in data or "segment_seconds" in data else "window")
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
        required = camera_setup_required(data)
        return {"config": safe, "revision": revision, "path": str(self.config_path), "regions": region_data(),
                "setup_required": required, "setup_message": CAMERA_SETUP_MESSAGE if required else ""}

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

    @staticmethod
    def recording_folders(root_fd, candidates):
        root_info = os.fstat(root_fd)
        folders = []
        by_path = {}
        for relative, info in recording_folder_walk(root_fd):
            parts = recording_folder_parts(relative)
            parent = by_path.get("/".join(parts[:-1])) if parts else None
            item = {"id": recording_folder_id(relative, root_info, info),
                    "name": parts[-1] if parts else "저장 폴더", "path": relative,
                    "parent_id": parent["id"] if parent else None, "depth": len(parts),
                    "count": 0, "size": 0, "exists": True}
            folders.append(item)
            by_path[relative] = item
        for relative, info in candidates:
            parts = recording_relative_parts(relative)
            for length in range(len(parts)):
                parent = by_path.get("/".join(parts[:length]))
                if parent is not None:
                    parent["count"] += 1
                    parent["size"] += info.st_size
        return folders

    def recordings(self, camera="", day="", offset=0, limit=50, trash=False, folder=""):
        try:
            if isinstance(offset, bool) or isinstance(limit, bool):
                raise ValueError()
            offset, limit = int(offset), int(limit)
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("영상 목록 범위가 올바르지 않습니다. 한 번에 1~100개를 조회하세요.") from None
        if not isinstance(camera, str) or not isinstance(day, str) or not isinstance(folder, str):
            raise ValueError("영상 검색 조건이 올바르지 않습니다.")
        if trash and folder:
            raise ValueError("휴지통에서는 폴더를 선택할 수 없습니다.")
        if day:
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError()
            except ValueError:
                raise ValueError("영상 날짜는 YYYY-MM-DD 형식으로 선택하세요.") from None
        folders, breadcrumbs = [], []
        selected_path = ""
        current_folder = None if trash else {"id": "", "name": "저장 폴더", "path": "", "exists": False}
        try:
            with self.recording_root() as root_fd:
                if trash:
                    items = self.recording_trash_items(root_fd)
                else:
                    if folder:
                        with recording_open_folder(root_fd, folder) as (_, selected_path):
                            pass
                    candidates = list(recording_walk(root_fd))
                    activity = self.recording_activity(root_fd, candidates)
                    items = [self.recording_item(relative, info, activity) for relative, info in candidates]
                    root_info = os.fstat(root_fd)
                    for item, (relative, info) in zip(items, candidates):
                        item["id"] = recording_scoped_id(relative, root_info, info)
                    folders = self.recording_folders(root_fd, candidates)
                    by_path = {entry["path"]: entry for entry in folders}
                    if selected_path not in by_path or (folder and by_path[selected_path]["id"] != folder):
                        raise ValueError("영상 폴더가 변경되었습니다. 목록을 새로고침하세요.")
                    current_folder = by_path[selected_path]
                    parts = recording_folder_parts(selected_path)
                    breadcrumbs = [by_path["/".join(parts[:length])] for length in range(len(parts) + 1)]
        except FileNotFoundError:
            if folder:
                raise ValueError("저장 폴더가 없어졌거나 변경되었습니다. 목록을 새로고침하세요.") from None
            items = []
        # A current URL is not evidence of the source of a historical recording.
        # Keep it separate from the host saved in new recording filenames.
        current_hosts = {entry.name: urlsplit(entry.url).hostname for entry in self.settings().cameras}
        for item in items:
            item["current_camera_host"] = current_hosts.get(item["camera"], "")
        if selected_path:
            items = [item for item in items if item["relative_path"].startswith(selected_path + "/")]
        cameras, dates = sorted({item["camera"] for item in items}), sorted({item["date"] for item in items}, reverse=True)
        selected = [item for item in items if (not camera or item["camera"] == camera) and (not day or item["date"] == day)]
        selected.sort(key=lambda item: (item.get("trashed_at", item["modified_at"]), item["relative_path"]), reverse=True)
        return {"items": selected[offset:offset + limit], "total": len(selected), "offset": offset, "limit": limit,
                "cameras": cameras, "dates": dates, "trash": bool(trash),
                "total_size": sum(item["size"] for item in selected), "folders": folders,
                "current_folder": current_folder, "breadcrumbs": breadcrumbs}

    def open_recording_folder(self, identifier):
        with self.action_lock, self.recording_root() as root_fd:
            with recording_open_folder(root_fd, identifier) as (folder_fd, relative):
                if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                    raise ValueError("이 ED 프로그램에 연결된 데스크톱 화면이 없습니다. 웹에서 폴더와 영상을 확인하세요.")
                executable = shutil.which("xdg-open")
                if not executable:
                    raise ValueError("이 ED에서 파일 관리자를 찾을 수 없습니다. 웹에서 폴더와 영상을 확인하세요.")
                # Pin cwd to the opened inode. Never pass an arbitrary URL/path to the opener,
                # or reopen the selected directory through a replaceable path or symlink.
                environment = os.environ.copy()
                environment.pop("PWD", None)
                process = subprocess.Popen([executable, "."], cwd="/proc/self/fd/{}".format(folder_fd),
                                           pass_fds=(folder_fd,), env=environment,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
                try:
                    code = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    threading.Thread(target=process.wait, daemon=True).start()
                else:
                    if code:
                        raise ValueError("이 ED의 파일 관리자에서 폴더를 열지 못했습니다. 데스크톱 로그인 상태를 확인하세요.")
        return {"message": "프로그램이 실행 중인 ED의 파일 관리자에 폴더 열기를 요청했습니다.", "path": relative}

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
                  "forecast_busy": self.preview_busy, "now": datetime.now(KST).isoformat(),
                  "setup_required": False, "setup_message": ""}
        result["exit_code"] = self.process.poll() if self.process is not None else None
        result["once_camera"] = getattr(self, "once_camera", None) if self.mode == "once" else None
        with self.log_lock:
            result["logs"] = list(self.logs)
        try:
            data, _ = self.read_config()
            required = camera_setup_required(data)
            result["setup_required"] = required
            result["setup_message"] = CAMERA_SETUP_MESSAGE if required else ""
            settings = (settings_from_data(data, self.config_path.parent, allow_unconfigured=True)
                        if required else self.settings())
            status = safe_read_status(settings.output_dir)
            result["output_dir"] = str(settings.output_dir)
            result["cameras"] = [{"name": camera.name, "client_no": camera.client_no,
                                  "intersection": camera.intersection, "direction": camera.direction,
                                  "ip": urlsplit(camera.url).hostname or ""}
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
                   "recording_mode", "segment_minutes", "retention_days", "min_free_gb", "weather_condition", "camera_connection"}
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
            required = camera_setup_required(data)
            if required and self._running():
                raise ValueError("녹화 중에는 카메라가 없는 설정을 적용할 수 없습니다. 녹화를 중지한 뒤 다시 설정하세요.")
            proposed = settings_from_data(data, self.config_path.parent, allow_unconfigured=required)
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
            message = ("설정을 저장하고 예약 녹화를 다시 시작했습니다." if resume else
                       "기본 설정을 저장했습니다. 녹화 전 카메라를 등록하세요." if required else "설정을 저장했습니다.")
            return {"message": message, "revision": self.read_config()[1], "setup_required": required}

    def check(self):
        settings = self.settings(refresh=True)
        check_output_access(settings)
        check_ffmpeg(settings)
        return {"message": "설정 검사를 통과했습니다. 실제 영상은 5분 시험 녹화 후 확인하세요."}

    def start(self, mode, minutes=1, start_at=None, camera=None):
        with self.action_lock:
            if self.closing:
                raise ValueError("화면을 종료하고 있습니다.")
            if self._running():
                raise ValueError("이미 녹화가 실행 중입니다. 먼저 중지하세요.")
            if mode not in ("scheduled", "once"):
                raise ValueError("녹화 실행 방식이 올바르지 않습니다.")
            settings = self.settings(refresh=True)
            if camera not in (None, ""):
                if mode != "once":
                    raise ValueError("카메라 선택은 한 번만 녹화에서 사용할 수 있습니다.")
                settings = select_once_camera(settings, camera)
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
                if camera not in (None, ""):
                    command += ["--record-camera", camera]
                if start_at:
                    command += ["--start-at", start_at]
            with self.log_lock:
                self.logs.clear()
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                            start_new_session=True)
            self.mode = mode
            self.once_camera = camera or None if mode == "once" else None
            self.reader = threading.Thread(target=self._read_log, args=(self.process,), daemon=True)
            self.reader.start()
            return {"message": "예약 녹화를 시작했습니다. 시간표와 날씨 조건에 따라 대기할 수 있습니다." if mode == "scheduled"
                    else ("{} · {}분 녹화를 시작했습니다. 지정한 시간이 끝나면 자동 종료합니다.".format(
                        camera or "모든 카메라", minutes))}

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
            self.switch_manager.close()
            self.camera_ping.close()
            self.live_view.close()
            self.camera_snapshots.close()
            if self.shared_browser_server is not None:
                self.shared_browser_server.shutdown()
                self.shared_browser_server.server_close()
                self.shared_browser_server = None
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
                             + "'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

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
                media_token = self._query_value(query, "token") if path in ("/api/recordings/media", "/api/snapshots/media") else None
                if not self._allowed(path != "/", media_token):
                    return
                if path == "/":
                    page = embedded_text(EMBEDDED_UI).replace("__RECORDER_TOKEN__", token)
                    self._send(page.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/config":
                    self._json(controller.configuration())
                elif path == "/api/state":
                    self._json(controller.state())
                elif path == "/api/live/cameras":
                    self._json({"cameras": controller.live_view.cameras()})
                elif path == "/api/snapshots":
                    self._json({"items": controller.camera_snapshots.list()})
                elif path == "/api/snapshots/media":
                    download = self._query_value(query, "download")
                    if download not in ("", "0", "1"):
                        raise ValueError("이미지 다운로드 조건이 올바르지 않습니다.")
                    item, content = controller.camera_snapshots.media(self._query_value(query, "id"))
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(content)))
                    disposition = "attachment" if download == "1" else "inline"
                    self.send_header("Content-Disposition", disposition + "; filename=\"camera.png\"; filename*=UTF-8''" + quote(item["filename"], safe=""))
                    self._security_headers()
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(content)
                elif path == "/api/switch/config":
                    self._json(controller.switch_manager.get_config())
                elif path == "/api/ping/history":
                    self._json(controller.camera_ping.history(self._query_value(query, "camera") or None,
                               int(self._query_value(query, "hours", "24"))))
                elif path == "/api/live/stream":
                    if self.command == "HEAD":
                        raise ValueError("실시간 영상은 GET 요청으로 받으세요.")
                    stream = controller.live_view.stream(self._query_value(query, "session"))
                    try:
                        first = next(stream)
                    except StopIteration:
                        raise ValueError("재생 연결이 종료되었습니다. 다시 재생하세요.") from None
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Connection", "close")
                        self._security_headers()
                        self.end_headers()
                        self.close_connection = True
                        self.wfile.write(first)
                        self.wfile.flush()
                        for chunk in stream:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (OSError, ValueError):
                        pass
                    finally:
                        stream.close()
                elif path == "/api/recordings":
                    trash = self._query_value(query, "trash", "0")
                    if trash not in ("0", "1"):
                        raise ValueError("휴지통 조회 조건이 올바르지 않습니다.")
                    self._json(controller.recordings(self._query_value(query, "camera"), self._query_value(query, "date"),
                               self._query_value(query, "offset", "0"), self._query_value(query, "limit", "50"), trash == "1",
                               self._query_value(query, "folder")))
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
                    result = controller.start(data.get("mode"), data.get("minutes", 1), data.get("start_at") or None, data.get("camera"))
                elif self.path == "/api/stop":
                    result = controller.stop()
                elif self.path == "/api/live/start":
                    result = controller.live_view.start(data.get("camera"), data.get("profile", "current"))
                elif self.path == "/api/live/stop":
                    result = controller.live_view.stop(data.get("session"))
                elif self.path == "/api/snapshots/capture":
                    result = controller.camera_snapshots.capture(data.get("camera"), data.get("profile", "current"))
                elif self.path == "/api/switch/config":
                    result = controller.switch_manager.save_connection(data)
                elif self.path == "/api/switch/inspect":
                    result = controller.switch_manager.inspect(data)
                elif self.path == "/api/switch/plan":
                    result = controller.switch_manager.plan(data)
                elif self.path == "/api/switch/apply":
                    result = controller.switch_manager.apply(data)
                elif self.path == "/api/ping/check":
                    result = controller.camera_ping.request_check()
                elif self.path == "/api/camera-profiles/read":
                    result = controller.camera_profiles.read(data.get("camera"))
                elif self.path == "/api/camera-profiles/plan":
                    result = controller.camera_profiles.plan(data.get("camera"), default_profile4=True)
                elif self.path == "/api/camera-profiles/apply":
                    controller.live_view.stop()
                    result = controller.camera_profiles.apply(data.get("token"))
                elif self.path == "/api/camera-profiles/recording":
                    result = controller.use_recording_profile(data.get("camera"), data.get("profile"), data.get("revision"))
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
                elif self.path == "/api/recordings/open-folder":
                    result = controller.open_recording_folder(data.get("folder"))
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
    controller.camera_ping.start()
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
# END EMBEDDED UI_SERVER


# BEGIN EMBEDDED DESKTOP_LAUNCHER
# Embedded into record.py. No desktop libraries are imported by CLI recording.
DESKTOP_ENTRY_NAME = "ed-camera-recorder.desktop"
DESKTOP_ENTRY_MARKER = "X-ED-Recorder-Managed=true"


def require_desktop_tk():
    try:
        import tkinter
    except ImportError:
        raise RuntimeError("데스크톱 화면에 필요한 Tk가 없습니다. Ubuntu에서 sudo apt install python3-tk ffmpeg 실행 후 다시 여세요.") from None
    return tkinter


def show_desktop_error(message):
    # A launcher has no terminal. Report startup failures with a desktop helper
    # if available; the normal log still contains the same actionable message.
    if sys.platform != "linux" or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    for program, arguments in (
            ("zenity", ["--error", "--title=ED 카메라 녹화", "--text=" + str(message)]),
            ("kdialog", ["--error", str(message), "--title", "ED 카메라 녹화"]),
            ("notify-send", ["--", "ED 카메라 녹화", str(message)])):
        executable = shutil.which(program)
        if executable:
            try:
                subprocess.Popen([executable] + arguments, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue


def desktop_value(value):
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("실행 아이콘 경로에 줄바꿈이나 제어 문자를 사용할 수 없습니다.")
    return value.replace("\\", "\\\\")


def desktop_exec_arg(value):
    # Desktop Entry string escaping is applied before Exec quote parsing.
    value = str(value)
    desktop_value(value)
    value = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    return '"' + desktop_value(value) + '"'


def render_desktop_entry(config_path, script_path=None, python_path=None):
    script = Path(script_path or __file__).resolve()
    config = Path(config_path).expanduser().absolute()
    executable = Path(python_path or sys.executable).resolve()
    if "=" in str(executable):
        raise ValueError("Python 실행 경로에는 = 문자를 사용할 수 없습니다.")
    command = " ".join(desktop_exec_arg(arg) for arg in
                       (executable, script, "--config", config, "--gui"))
    return "\n".join([
        "[Desktop Entry]", DESKTOP_ENTRY_MARKER, "Version=1.0", "Type=Application",
        "Name=ED 카메라 녹화", "Name[en]=ED Camera Recorder",
        "Comment=시간표와 날씨 조건으로 카메라 영상을 저장합니다",
        "Exec=" + command, "Path=" + desktop_value(config.parent),
        "Icon=camera-video", "Terminal=false", "StartupNotify=false",
        "Categories=AudioVideo;Video;", "Keywords=ED;camera;recording;녹화;", "",
    ])


def install_desktop_shortcut(config_path):
    if sys.platform != "linux":
        raise ValueError("실행 아이콘은 Linux 데스크톱에서 만들 수 있습니다.")
    if os.geteuid() == 0:
        raise ValueError("실행 아이콘은 sudo 없이 로그인한 일반 사용자로 만드세요.")
    config = Path(config_path).expanduser().absolute()
    if config.suffix.lower() != ".json":
        raise ValueError("데스크톱 화면에는 recording.json 설정을 사용하세요.")
    if not config.parent.is_dir():
        raise ValueError("설정 파일을 둘 폴더를 먼저 만들어 주세요: " + str(config.parent))
    require_desktop_tk()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    if not data_home.is_absolute():
        raise ValueError("XDG_DATA_HOME은 절대 경로여야 합니다.")
    target = data_home / "applications" / DESKTOP_ENTRY_NAME
    if target.is_symlink():
        raise ValueError("기존 실행 아이콘이 심볼릭 링크이므로 변경하지 않았습니다.")
    if target.exists():
        if not target.is_file() or target.stat().st_size > 65536:
            raise ValueError("기존 실행 아이콘을 확인하세요.")
        if DESKTOP_ENTRY_MARKER not in target.read_text(encoding="utf-8").splitlines():
            raise ValueError("다른 프로그램의 실행 아이콘이 있어 변경하지 않았습니다.")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_private_text(target, render_desktop_entry(config))
    target.chmod(0o644)
    return {"message": "앱 메뉴에 ‘ED 카메라 녹화’를 등록했습니다. 이 코드와 설정 파일은 현재 위치에 보관하세요.",
            "path": str(target)}
# END EMBEDDED DESKTOP_LAUNCHER

# BEGIN EMBEDDED DESKTOP_GUI
# Embedded into record.py. Tk is imported only when the desktop GUI is requested.
import queue


DESKTOP_PROVIDERS = (
    ("open_meteo", "Open-Meteo · 개인용 / 키 없음"),
    ("met_no", "MET Norway · 연락처 필요"),
    ("data_go_kr", "기상청 · 공공데이터포털"),
    ("kma_api_hub", "기상청 · API허브"),
)

DESKTOP_CAMERA_MODES = (
    ("setup", "기존 setup.ini 사용"),
    ("direct", "카메라 IP 직접 입력"),
    ("custom", "기존 개별 설정 유지"),
)


def desktop_number(value, label, integer=False):
    try:
        result = int(str(value).strip()) if integer else float(str(value).strip())
    except (ValueError, TypeError):
        raise ValueError(label + ("은 정수로 입력하세요." if integer else "은 숫자로 입력하세요.")) from None
    if not math.isfinite(result):
        raise ValueError(label + "에 올바른 숫자를 입력하세요.")
    return result


def desktop_dates(text):
    values = [value for value in re.split(r"[\s,;]+", text.strip()) if value]
    parse_recording_dates(values)
    return values


def desktop_config_patch(baseline, current):
    """Only send fields edited in this window; preserve hidden/legacy settings."""
    return {name: value for name, value in current.items() if value != baseline.get(name)}


def desktop_forecast_text(weather, busy=False):
    if busy:
        return "예보를 확인하고 있습니다. 잠시 기다려 주세요."
    if weather.get("error"):
        return weather["error"]
    if not weather.get("enabled"):
        result = "날씨 조건이 꺼져 있습니다. 날짜와 시간표에 따라 녹화합니다."
    elif weather.get("allowed"):
        result = "오늘은 날씨 조건에 맞습니다. 오늘의 예약 시간표를 실행합니다."
    elif weather.get("known"):
        result = "확인한 예보에는 선택한 날씨가 없습니다. 조건을 기다립니다."
    else:
        result = "아직 유효한 예보가 없습니다. 예보 조회를 눌러 확인하세요."
    detail = " · ".join(str(weather.get(key, "")) for key in ("region", "label", "source") if weather.get(key))
    if detail:
        result += "\n" + detail
    if weather.get("checked_at"):
        result += "\n최근 확인: " + weather["checked_at"].replace("T", " ")[:19] + " KST"
    network = weather.get("network") or {}
    if network:
        result += "\n오늘 수신 {:.1f} KB · 요청 {}회 (응답 본문 기준){}".format(
            network.get("body_bytes_today", 0) / 1024, network.get("requests_today", 0),
            " · 캐시 사용" if network.get("cached") else "")
    return result


class RecorderDesktopApp:
    """Native forms over the existing controller; all controller work is serialized."""

    def __init__(self, root, tk, ttk, dialogs, controller):
        self.root, self.tk, self.ttk = root, tk, ttk
        self.filedialog, self.messagebox = dialogs
        self.controller = controller
        self.jobs, self.results = queue.Queue(), queue.Queue()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()
        self.pending_action, self.poll_pending, self.closing, self.destroyed = False, False, False, False
        self.loading, self.loaded, self.dirty = False, False, False
        self.revision, self.baseline, self.last_state = None, {}, {}
        self.rows, self.form, self.condition_vars = [], {}, {}
        self.region_rows, self.disabled_widgets, self.tab_canvases = [], [], {}
        self.wrap_labels, self.wrap_job = [], None
        self.last_logs = None
        self.root.title("ED 카메라 녹화")
        self.root.geometry("1120x820")
        self.root.minsize(860, 640)
        self.root.configure(background="#edf1f6")
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self._style()
        self._build()
        self.root.after(75, self._pump)
        self.root.after(2000, self._poll)
        self.reload(initial=True)

    def _style(self):
        from tkinter import font as tkfont
        families = {name.casefold(): name for name in tkfont.families(self.root)}
        preferred = ("Noto Sans CJK KR", "Noto Sans KR", "Malgun Gothic", "맑은 고딕", "NanumGothic", "나눔고딕")
        self.font_family = next((families[name.casefold()] for name in preferred if name.casefold() in families),
                                tkfont.nametofont("TkDefaultFont", root=self.root).actual("family"))
        style = self.ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(self.font_family, 11), background="#edf1f6", foreground="#1d304a")
        style.configure("TFrame", background="#edf1f6")
        style.configure("TLabel", background="#edf1f6")
        style.configure("Heading.TLabel", font=(self.font_family, 22, "bold"))
        style.configure("Section.TLabel", font=(self.font_family, 14, "bold"))
        style.configure("Muted.TLabel", foreground="#536783")
        style.configure("TButton", padding=(12, 8))
        style.configure("Primary.TButton", background="#2d5182", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#223e63"), ("disabled", "#a3b0c3")])
        style.configure("TNotebook", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 12))
        style.map("TNotebook.Tab", background=[("selected", "#2d5182")],
                  foreground=[("selected", "#ffffff")], padding=[("selected", (20, 12))])
        style.configure("Treeview", rowheight=30, fieldbackground="#f9fbfd", background="#f9fbfd")
        style.configure("Treeview.Heading", font=(self.font_family, 11, "bold"), padding=(6, 8))
        style.configure("TEntry", fieldbackground="#ffffff", padding=6)
        style.configure("TCombobox", padding=6)

    def _var(self, name, value="", boolean=False, track=True):
        var = self.tk.BooleanVar(self.root, value=value) if boolean else self.tk.StringVar(self.root, value=value)
        self.form[name] = var
        if track:
            var.trace_add("write", self._changed)
        return var

    def _label(self, parent, text, row, column=0, **kwargs):
        label = self.ttk.Label(parent, text=text, **kwargs)
        label.grid(row=row, column=column, sticky="w", padx=(0, 14), pady=5)
        return label

    def _entry(self, parent, name, row, value="", column=1, width=38, **kwargs):
        entry = self.ttk.Entry(parent, textvariable=self._var(name, value), width=width, **kwargs)
        entry.grid(row=row, column=column, sticky="ew", pady=5)
        return entry

    def _scroll_tab(self, title):
        holder = self.ttk.Frame(self.notebook)
        self.notebook.add(holder, text=title)
        canvas = self.tk.Canvas(holder, borderwidth=0, highlightthickness=0, background="#edf1f6")
        bar = self.ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = self.ttk.Frame(canvas, padding=(22, 20, 22, 26))
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.tab_canvases[str(holder)] = canvas
        content.columnconfigure(0, weight=1)
        return content

    def _build(self):
        top = self.ttk.Frame(self.root, padding=(24, 20, 24, 14))
        top.pack(fill="x")
        self.ttk.Label(top, text="카메라 녹화 관리", style="Heading.TLabel").pack(side="left")
        self.ttk.Label(top, text="Linux 데스크톱  /  한국 시간", style="Muted.TLabel").pack(side="right")
        self.notice = self.tk.StringVar(self.root, "설정을 불러오고 있습니다.")
        self.ttk.Label(self.root, textvariable=self.notice, wraplength=1000, padding=(24, 8),
                       style="Muted.TLabel").pack(fill="x")
        footer = self.ttk.Frame(self.root, padding=(20, 12))
        footer.pack(side="bottom", fill="x")
        self.dirty_text = self.tk.StringVar(self.root, "")
        self.ttk.Label(footer, textvariable=self.dirty_text).pack(side="left")
        self.ttk.Button(footer, text="설정 저장 · 적용", style="Primary.TButton", command=self.save).pack(side="right")
        self.ttk.Button(footer, text="설정 검사", command=lambda: self.with_settings(self.controller.check)).pack(side="right", padx=8)
        self.ttk.Button(footer, text="다시 불러오기", command=self.reload).pack(side="right")
        self.notebook = self.ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=18)
        self.status_tab = self._scroll_tab("녹화 현황")
        self.schedule_tab = self._scroll_tab("시간표 · 날씨")
        self.device_tab = self._scroll_tab("장비 · 저장 위치")
        self._build_status()
        self._build_schedule()
        self._build_device()
        self.root.bind("<MouseWheel>", self._wheel, add="+")
        self.root.bind("<Button-4>", self._wheel, add="+")
        self.root.bind("<Button-5>", self._wheel, add="+")
        self._prepare_wrap(self.root)
        self.root.bind("<Configure>", self._queue_wrap, add="+")
        self._queue_wrap()

    def _prepare_wrap(self, parent):
        for child in parent.winfo_children():
            if isinstance(child, self.ttk.Label):
                length = self.root.winfo_pixels(child.cget("wraplength") or 0)
                if length or len(str(child.cget("text"))) >= 38:
                    child.configure(wraplength=min(length or 520, 520), justify="left")
                    self.wrap_labels.append(child)
            self._prepare_wrap(child)

    def _queue_wrap(self, _event=None):
        if self.wrap_job is None and not self.destroyed:
            self.wrap_job = self.root.after(90, self._wrap_content)

    def _wrap_content(self):
        self.wrap_job = None
        if self.destroyed:
            return
        for label in self.wrap_labels:
            if not label.winfo_exists() or not label.winfo_ismapped():
                continue
            parent = label.master
            if parent.winfo_width() < 150:
                continue
            available = max(120, min(950, parent.winfo_width() - label.winfo_x() - 40))
            current = self.root.winfo_pixels(label.cget("wraplength"))
            if abs(current - available) > 3:
                label.configure(wraplength=available)

    def _wheel(self, event):
        if isinstance(event.widget, (self.tk.Text, self.ttk.Treeview, self.ttk.Combobox)):
            return
        canvas = self.tab_canvases.get(self.notebook.select())
        if canvas is not None:
            step = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
            canvas.yview_scroll(step * 3, "units")

    def _build_status(self):
        parent = self.status_tab
        self.state_text = self.tk.StringVar(self.root, "녹화 중지")
        self.ttk.Label(parent, textvariable=self.state_text, style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        controls = self.ttk.Frame(parent)
        controls.grid(row=1, column=0, sticky="ew", pady=(14, 8))
        self.start_button = self.ttk.Button(controls, text="예약 녹화 시작", style="Primary.TButton",
                                           command=lambda: self.with_settings(lambda: self.controller.start("scheduled")))
        self.start_button.pack(side="left")
        self.stop_button = self.ttk.Button(controls, text="녹화 중지", command=lambda: self.action(self.controller.stop))
        self.stop_button.pack(side="left", padx=8)
        self.ttk.Button(controls, text="저장 폴더 열기", command=self.open_output).pack(side="right")
        self.ttk.Button(controls, text="실시간 보기 · 프로필 관리",
                        command=lambda: self.action(self.controller.open_browser_controls)).pack(side="right", padx=8)
        self.summary_text = self.tk.StringVar(self.root, "")
        self.ttk.Label(parent, textvariable=self.summary_text, wraplength=950).grid(row=2, column=0, sticky="w", pady=8)
        once = self.ttk.LabelFrame(parent, text="한 번만 녹화", padding=14)
        once.grid(row=3, column=0, sticky="ew", pady=(12, 16))
        self.ttk.Label(once, text="녹화 길이").grid(row=0, column=0, sticky="w")
        self.ttk.Entry(once, textvariable=self._var("once_minutes", "5", track=False), width=7).grid(row=0, column=1, padx=8)
        self.ttk.Label(once, text="분").grid(row=0, column=2)
        self.ttk.Checkbutton(once, text="오늘 시작 시각 지정", variable=self._var("once_later", False, boolean=True, track=False),
                             command=self._once_mode).grid(row=0, column=3, padx=(22, 8))
        self.once_time = self.ttk.Entry(once, textvariable=self._var("once_time", "18:00", track=False), width=8, state="disabled")
        self.once_time.grid(row=0, column=4)
        self.once_button = self.ttk.Button(once, text="시험 녹화 시작", command=self.start_once)
        self.once_button.grid(row=0, column=5, padx=(18, 0))
        self.ttk.Label(once, text="날짜·시간표·날씨 조건과 관계없이 녹화하며, 정한 시간이 끝나면 자동 종료합니다.",
                       style="Muted.TLabel", wraplength=900).grid(row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))
        self.ttk.Label(parent, text="연결할 카메라", style="Section.TLabel").grid(row=4, column=0, sticky="w", pady=(0, 8))
        self.camera_tree = self.ttk.Treeview(parent, columns=("client", "name", "direction", "state"), show="headings", height=4)
        for column, title, width in (("client", "Client 번호", 130), ("name", "교차로명", 330),
                                     ("direction", "방향", 140), ("state", "상태", 160)):
            self.camera_tree.heading(column, text=title)
            self.camera_tree.column(column, width=width, minwidth=80)
        self.camera_tree.grid(row=5, column=0, sticky="ew")
        weather_head = self.ttk.Frame(parent)
        weather_head.grid(row=6, column=0, sticky="ew", pady=(20, 6))
        self.ttk.Label(weather_head, text="날씨와 녹화 조건", style="Section.TLabel").pack(side="left")
        self.ttk.Button(weather_head, text="예보 조회", command=lambda: self.with_settings(self.controller.forecast)).pack(side="right")
        self.weather_text = self.tk.StringVar(self.root, "")
        self.ttk.Label(parent, textvariable=self.weather_text, wraplength=950).grid(row=7, column=0, sticky="w", pady=5)
        self.ttk.Label(parent, text="최근 실행 기록", style="Section.TLabel").grid(row=8, column=0, sticky="w", pady=(20, 8))
        log_frame = self.ttk.Frame(parent)
        log_frame.grid(row=9, column=0, sticky="ew")
        self.log_box = self.tk.Text(log_frame, height=8, font=(self.font_family, 10), background="#f9fbfd",
                                    foreground="#1d304a", wrap="word", borderwidth=1, relief="solid", state="disabled")
        self.log_box.pack(side="left", fill="both", expand=True)
        log_scroll = self.ttk.Scrollbar(log_frame, command=self.log_box.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=log_scroll.set)
        self.ttk.Label(parent, text="이 창을 종료하면 이 창에서 시작한 녹화도 정상 종료합니다. 계속 녹화하려면 창을 최소화하세요.",
                       style="Muted.TLabel", wraplength=950).grid(row=10, column=0, sticky="w", pady=(16, 0))

    def _build_schedule(self):
        parent = self.schedule_tab
        self.ttk.Label(parent, text="녹화할 시간", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Label(parent, text="모든 시각은 한국 시간입니다. 종료가 시작보다 이르면 다음 날까지 녹화합니다.",
                       style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 12))
        self.schedule_note = self.tk.StringVar(self.root, "")
        self.ttk.Label(parent, textvariable=self.schedule_note, wraplength=950).grid(row=2, column=0, sticky="w")
        self.range_frame = self.ttk.Frame(parent)
        self.range_frame.grid(row=3, column=0, sticky="ew")
        self.ttk.Button(parent, text="시간 구간 추가", command=self.add_range).grid(row=4, column=0, sticky="w", pady=8)
        dates = self.ttk.Frame(parent)
        dates.grid(row=5, column=0, sticky="ew", pady=(12, 16))
        dates.columnconfigure(1, weight=1)
        self._label(dates, "녹화 날짜", 0)
        self._entry(dates, "recording_dates", 0)
        self.ttk.Label(dates, text="비우면 매일 반복. 예: 2026-10-28, 2026-10-30", style="Muted.TLabel").grid(row=1, column=1, sticky="w")
        self.ttk.Separator(parent).grid(row=6, column=0, sticky="ew", pady=(0, 18))
        self.ttk.Label(parent, text="날씨 조건", style="Section.TLabel").grid(row=7, column=0, sticky="w")
        self.ttk.Checkbutton(parent, text="선택한 날씨가 예보된 날에만 예약 녹화", variable=self._var("weather_enabled", False, boolean=True)).grid(row=8, column=0, sticky="w", pady=10)
        self.ttk.Label(parent, text="하루 중 한 번이라도 조건이 맞으면 그날의 시간표를 그대로 실행합니다. 실제 관측 날씨가 아닌 예보 기준입니다.",
                       style="Muted.TLabel", wraplength=950).grid(row=9, column=0, sticky="w", pady=(0, 12))
        weather = self.ttk.Frame(parent)
        weather.grid(row=10, column=0, sticky="ew")
        weather.columnconfigure(1, weight=1)
        self._label(weather, "예보 제공처", 0)
        self.provider = self.ttk.Combobox(weather, textvariable=self._var("provider"), state="readonly", values=[label for _, label in DESKTOP_PROVIDERS])
        self.provider.grid(row=0, column=1, sticky="ew", pady=5)
        self.provider.bind("<<ComboboxSelected>>", lambda _event: self._provider_fields())
        region = self.ttk.Frame(weather)
        region.grid(row=1, column=1, sticky="ew", pady=5)
        self._label(weather, "녹화 지역", 1)
        self.region_boxes = []
        for index, (key, width) in enumerate((("sido", 18), ("sigungu", 19), ("eupmyeondong", 18))):
            box = self.ttk.Combobox(region, textvariable=self._var(key), state="readonly", width=width)
            box.grid(row=0, column=index, padx=(0, 6), sticky="ew")
            region.columnconfigure(index, weight=1)
            self.region_boxes.append(box)
            box.bind("<<ComboboxSelected>>", lambda _event, level=index: self._region_changed(level))
        self.ttk.Label(weather, text="읍·면·동은 비워 두면 시·군·구 대표 지점을 사용합니다.", style="Muted.TLabel").grid(row=2, column=1, sticky="w")
        self._label(weather, "녹화할 날씨", 3)
        choices = self.ttk.Frame(weather)
        choices.grid(row=3, column=1, sticky="w", pady=8)
        for name, label in WEATHER_LABELS.items():
            var = self._var("condition_" + name, False, boolean=True)
            self.condition_vars[name] = var
            self.ttk.Checkbutton(choices, text=label, variable=var).pack(side="left", padx=(0, 18))
        self._label(weather, "예보 조회 간격", 4)
        interval = self.ttk.Frame(weather)
        interval.grid(row=4, column=1, sticky="w")
        self.ttk.Entry(interval, textvariable=self._var("poll_minutes", "30"), width=7).pack(side="left")
        self.ttk.Label(interval, text="분 (10~180분, 유효한 캐시 재사용)").pack(side="left", padx=8)
        self.met_frame = self.ttk.Frame(weather)
        self.met_frame.grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)
        self.met_frame.columnconfigure(1, weight=1)
        self._label(self.met_frame, "연락처", 0)
        self._entry(self.met_frame, "met_contact", 0)
        self.ttk.Label(self.met_frame, text="MET Norway에 전송할 개인 이메일 또는 프로젝트 홈페이지 주소", style="Muted.TLabel").grid(row=1, column=1, sticky="w")
        self.key_frame = self.ttk.Frame(weather)
        self.key_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=8)
        self.key_frame.columnconfigure(1, weight=1)
        self._label(self.key_frame, "인증키 파일", 0)
        self._entry(self.key_frame, "api_key_file", 0, "kma_service.key")
        self._label(self.key_frame, "새 인증키", 1)
        self.key_input = self.ttk.Entry(self.key_frame, show="*", width=38)
        self.key_input.grid(row=1, column=1, sticky="ew", pady=5)
        self.ttk.Button(self.key_frame, text="인증키 저장", command=self.save_key).grid(row=1, column=2, padx=8)
        self.ttk.Label(self.key_frame, text="저장된 키는 다시 표시하지 않습니다. 새 키를 등록할 때만 입력하세요.", style="Muted.TLabel").grid(row=2, column=1, sticky="w")
        self.provider_note = self.tk.StringVar(self.root, "")
        self.ttk.Label(weather, textvariable=self.provider_note, wraplength=900, style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.ttk.Button(weather, text="제공처 안내 열기", command=self.provider_info).grid(row=8, column=1, sticky="w", pady=8)

    def _build_device(self):
        parent = self.device_tab
        self.ttk.Label(parent, text="카메라와 저장 위치", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        connection = self.ttk.Frame(parent)
        connection.grid(row=1, column=0, sticky="ew", pady=14)
        connection.columnconfigure(1, weight=1)
        self._label(connection, "카메라 지정 방법", 0)
        self.camera_mode_box = self.ttk.Combobox(
            connection, textvariable=self._var("camera_mode", DESKTOP_CAMERA_MODES[0][1]),
            values=[label for _, label in DESKTOP_CAMERA_MODES], state="readonly", width=35)
        self.camera_mode_box.grid(row=0, column=1, sticky="w", pady=5)
        self.camera_mode_box.bind("<<ComboboxSelected>>", lambda _event: self._camera_mode_fields())
        self.setup_frame = self.ttk.Frame(connection)
        self.setup_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.setup_frame.columnconfigure(1, weight=1)
        self._label(self.setup_frame, "카메라 setup.ini", 0)
        self.setup_box = self.ttk.Combobox(self.setup_frame, textvariable=self._var("setup_ini", "auto"), width=55)
        self.setup_box.grid(row=0, column=1, sticky="ew", pady=5)
        self.ttk.Button(self.setup_frame, text="파일 선택", command=self.choose_setup).grid(row=0, column=2, padx=(8, 0))
        self.ttk.Button(self.setup_frame, text="자동 찾기", command=self.find_setup).grid(row=1, column=1, sticky="w", pady=5)
        self.setup_note = self.tk.StringVar(self.root, "여러 설정이 나오면 실제 운영 중인 파일을 직접 선택하세요.")
        self.ttk.Label(self.setup_frame, textvariable=self.setup_note, style="Muted.TLabel", wraplength=700).grid(row=2, column=1, sticky="w", pady=5)
        self.direct_frame = self.ttk.Frame(connection)
        self.direct_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.direct_frame.columnconfigure(1, weight=1)
        self._label(self.direct_frame, "녹화할 카메라 IP", 0)
        self._var("camera_ips")
        self.camera_ips = self.tk.Text(self.direct_frame, height=4, width=48, wrap="word",
                                      font=(self.font_family, 11), background="#ffffff", foreground="#1d304a")
        self.camera_ips.grid(row=0, column=1, sticky="ew", pady=5)
        self.camera_ips.bind("<<Modified>>", self._camera_ips_changed)
        self.ttk.Label(self.direct_frame, text="카메라의 실제 IPv4 주소를 한 줄에 하나씩 또는 쉼표로 구분해 입력하세요. 입력한 카메라를 모두 녹화합니다.",
                       style="Muted.TLabel", wraplength=700).grid(row=1, column=1, sticky="w", pady=5)
        self._label(self.direct_frame, "공통 아이디", 2)
        self._entry(self.direct_frame, "camera_username", 2, "")
        self._label(self.direct_frame, "공통 비밀번호", 3)
        self._entry(self.direct_frame, "camera_password", 3, show="•")
        self.camera_password_note = self.tk.StringVar(self.root, "카메라에 설정된 비밀번호를 입력하세요.")
        self.ttk.Label(self.direct_frame, textvariable=self.camera_password_note, style="Muted.TLabel", wraplength=700).grid(row=4, column=1, sticky="w", pady=5)
        self._label(self.direct_frame, "RTSP 포트", 5)
        self._entry(self.direct_frame, "camera_port", 5, "554", width=12).grid_configure(sticky="w")
        self._label(self.direct_frame, "영상 경로", 6)
        self._entry(self.direct_frame, "camera_path", 6, "/profile2/media.smp")
        self.ttk.Label(self.direct_frame, text="직접 입력할 때는 아래에서 영상 저장 폴더도 지정하세요.",
                       style="Muted.TLabel", wraplength=700).grid(row=7, column=1, sticky="w", pady=5)
        self.custom_note = self.ttk.Label(connection, text="카메라마다 다른 연결 설정을 그대로 사용합니다. 새 IP 목록을 지정하려면 ‘카메라 IP 직접 입력’을 선택하세요.",
                                          style="Muted.TLabel", wraplength=780)
        self.custom_note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.ttk.Separator(parent).grid(row=2, column=0, sticky="ew", pady=12)
        fields = self.ttk.Frame(parent)
        fields.grid(row=3, column=0, sticky="ew", pady=14)
        fields.columnconfigure(1, weight=1)
        self._label(fields, "영상 저장 폴더", 3)
        self._entry(fields, "output_dir", 3, "auto")
        self.ttk.Button(fields, text="폴더 선택", command=self.choose_output).grid(row=3, column=2, padx=(8, 0))
        self.output_note = self.tk.StringVar(self.root)
        self.ttk.Label(fields, textvariable=self.output_note, style="Muted.TLabel", wraplength=700).grid(row=4, column=1, sticky="w", pady=5)
        self._label(fields, "교차로명", 5)
        self._entry(fields, "intersection_name", 5)
        self.ttk.Label(fields, text="비우면 카메라 설정의 이름을 사용합니다.", style="Muted.TLabel").grid(row=6, column=1, sticky="w")
        self._label(fields, "파일명 날씨 표시", 7)
        self._entry(fields, "weather", 7)
        self.ttk.Label(fields, text="수동 표시용 이름입니다. 자동 예보 조건은 시간표·날씨에서 설정하세요.", style="Muted.TLabel").grid(row=8, column=1, sticky="w")
        self._label(fields, "녹화 파일 저장 방식", 9)
        mode_frame = self.ttk.Frame(fields)
        mode_frame.grid(row=9, column=1, columnspan=2, sticky="w", pady=5)
        mode_box = self.ttk.Combobox(mode_frame, textvariable=self._var("recording_mode", "예약 구간 전체 한 파일"),
                                    values=("예약 구간 전체 한 파일", "일정 간격으로 분할"), state="readonly", width=25)
        mode_box.pack(side="left")
        mode_box.bind("<<ComboboxSelected>>", lambda _event: self._recording_mode_fields())
        self.segment_box = self.ttk.Combobox(mode_frame, textvariable=self._var("segment_minutes", "30"),
                                             values=("5", "10", "30", "60"), width=6)
        self.segment_box.pack(side="left", padx=(12, 4))
        self.ttk.Label(mode_frame, text="분 (분할 모드만)", style="Muted.TLabel").pack(side="left")
        self._recording_mode_fields()
        self._label(fields, "자동 삭제 기준 (일)", 10)
        self._entry(fields, "retention_days", 10, "0", width=12).grid_configure(sticky="w")
        self.ttk.Label(fields, text="0이면 자동 삭제하지 않습니다. 1 이상이면 보관 기간이 지난 녹화 파일을 삭제합니다.", style="Muted.TLabel", wraplength=740).grid(row=11, column=1, sticky="w")
        self._label(fields, "최소 여유 공간 (GiB)", 12)
        self._entry(fields, "min_free_gb", 12, "5", width=12).grid_configure(sticky="w")
        self.ttk.Separator(parent).grid(row=4, column=0, sticky="ew", pady=16)
        self.ttk.Label(parent, text="다음에도 간단히 실행", style="Section.TLabel").grid(row=5, column=0, sticky="w")
        self.ttk.Label(parent, text="앱 메뉴에 ‘ED 카메라 녹화’ 아이콘을 만듭니다. 이 작업은 녹화를 시작하거나 부팅 서비스를 등록하지 않습니다.",
                       style="Muted.TLabel", wraplength=950).grid(row=6, column=0, sticky="w", pady=8)
        self.ttk.Button(parent, text="실행 아이콘 만들기", command=lambda: self.action(
            lambda: install_desktop_shortcut(self.controller.config_path))).grid(row=7, column=0, sticky="w")
        self.ttk.Label(parent, text="현재 설정 파일\n" + str(self.controller.config_path), wraplength=950,
                       style="Muted.TLabel").grid(row=8, column=0, sticky="w", pady=(20, 0))
        self._camera_mode_fields()

    def _recording_mode_fields(self):
        mode = self.form["recording_mode"].get()
        self.segment_box.configure(state="normal" if mode == "일정 간격으로 분할" else "disabled")

    def _camera_mode_id(self):
        value = self.form["camera_mode"].get()
        return next((key for key, label in DESKTOP_CAMERA_MODES if value == label), "setup")

    def _camera_mode_fields(self):
        mode = self._camera_mode_id()
        self.setup_frame.grid() if mode == "setup" else self.setup_frame.grid_remove()
        self.direct_frame.grid() if mode == "direct" else self.direct_frame.grid_remove()
        self.custom_note.grid() if mode == "custom" else self.custom_note.grid_remove()
        self.output_note.set("폴더 선택으로 저장할 위치를 지정하세요. 직접 입력 모드에서는 auto를 사용할 수 없습니다."
                             if mode == "direct" else "auto이면 프로젝트의 runtime/scheduled_recordings 폴더에 저장합니다.")

    def _camera_ips_changed(self, _event=None):
        if not self.camera_ips.edit_modified():
            return
        value = self.camera_ips.get("1.0", "end-1c")
        self.camera_ips.edit_modified(False)
        if value != self.form["camera_ips"].get():
            self.form["camera_ips"].set(value)

    def _changed(self, *_args):
        if not self.loading and self.loaded:
            self.dirty = True
            self.dirty_text.set("저장하지 않은 변경 사항이 있습니다.")

    def _once_mode(self):
        self.once_time.configure(state="normal" if self.form["once_later"].get() else "disabled")

    def add_range(self, value=DEFAULT_TIME_RANGES[0]):
        if not isinstance(value, str):
            value = DEFAULT_TIME_RANGES[0]
        start, _, end = value.partition("-")
        row = self.ttk.Frame(self.range_frame)
        row.pack(fill="x", pady=4)
        start_var, end_var = self.tk.StringVar(self.root, start), self.tk.StringVar(self.root, end)
        for var in (start_var, end_var):
            var.trace_add("write", self._changed)
        self.ttk.Label(row, text="시작").pack(side="left", padx=(0, 8))
        self.ttk.Entry(row, textvariable=start_var, width=9).pack(side="left")
        self.ttk.Label(row, text="종료").pack(side="left", padx=(18, 8))
        self.ttk.Entry(row, textvariable=end_var, width=9).pack(side="left")
        record = (row, start_var, end_var)
        self.rows.append(record)
        self.ttk.Button(row, text="삭제", command=lambda: self.remove_range(record)).pack(side="left", padx=18)
        self._changed()

    def remove_range(self, record):
        self.rows.remove(record)
        record[0].destroy()
        self._changed()

    def _region_changed(self, level):
        rows = self.region_rows
        if level <= 0:
            cities = sorted({row[1] for row in rows if row[0] == self.form["sido"].get()})
            self.region_boxes[1].configure(values=cities)
            if self.form["sigungu"].get() not in cities:
                self.form["sigungu"].set("")
        towns = sorted({row[2] for row in rows if row[:2] == [self.form["sido"].get(), self.form["sigungu"].get()]})
        self.region_boxes[2].configure(values=towns)
        if self.form["eupmyeondong"].get() not in towns:
            self.form["eupmyeondong"].set("")

    def _provider_id(self):
        value = self.form["provider"].get()
        return next((key for key, label in DESKTOP_PROVIDERS if value == label), "open_meteo")

    def _provider_fields(self):
        provider = self._provider_id()
        self.met_frame.grid() if provider == "met_no" else self.met_frame.grid_remove()
        self.key_frame.grid() if provider in ("data_go_kr", "kma_api_hub") else self.key_frame.grid_remove()
        notes = {
            "open_meteo": "Open-Meteo: 개인·비상업용 무료 API입니다. API 키와 연락처가 필요 없습니다. 예보 데이터 출처: Open-Meteo / CC BY 4.0.",
            "met_no": "MET Norway: API 키는 없지만 실제 연락처를 요청에 포함해야 합니다. 예보 데이터 출처: MET Norway / CC BY 4.0.",
            "data_go_kr": "기상청 공공데이터포털의 단기예보 서비스 인증키가 필요합니다.",
            "kma_api_hub": "기상청 API허브의 단기예보 서비스 인증키가 필요합니다.",
        }
        self.provider_note.set(notes[provider])

    def provider_info(self):
        urls = {"open_meteo": "https://open-meteo.com/en/pricing", "met_no": "https://api.met.no/doc/TermsOfService",
                "data_go_kr": "https://www.data.go.kr/data/15084084/openapi.do", "kma_api_hub": "https://apihub.kma.go.kr/"}
        url = urls[self._provider_id()]
        self.action(lambda: webbrowser.open(url))

    def choose_setup(self):
        value = self.filedialog.askopenfilename(parent=self.root, title="운영 중인 setup.ini 선택",
                                              filetypes=(("카메라 설정", "*.ini"), ("모든 파일", "*")))
        if value:
            self.form["setup_ini"].set(value)

    def choose_output(self):
        value = self.filedialog.askdirectory(parent=self.root, title="영상 저장 폴더 선택")
        if value:
            self.form["output_dir"].set(value)

    def find_setup(self):
        def show(values):
            self.setup_box.configure(values=values)
            self.setup_note.set("{}개를 찾았습니다. 위 목록에서 실제 운영 파일을 선택하세요.".format(len(values)) if values
                                else "자동 검색 결과가 없습니다. ‘파일 선택’으로 setup.ini를 지정하세요.")
        self.action(self.controller.setup_candidates, show, "카메라 설정을 찾고 있습니다.")

    def collect(self):
        values = {name: self.form[name].get().strip() for name in
                  ("setup_ini", "output_dir", "intersection_name", "weather")}
        mode = self._camera_mode_id()
        values["camera_connection"] = {"mode": mode}
        if mode == "direct":
            values["camera_connection"].update({
                "ips": self.camera_ips.get("1.0", "end-1c").strip(),
                "username": self.form["camera_username"].get().strip(),
                "password": self.form["camera_password"].get(),
                "port": desktop_number(self.form["camera_port"].get(), "RTSP 포트", True),
                "path": self.form["camera_path"].get().strip(),
            })
        values["time_ranges"] = [start.get().strip() + "-" + end.get().strip() for _, start, end in self.rows]
        values["recording_dates"] = desktop_dates(self.form["recording_dates"].get())
        values["recording_mode"] = "split" if self.form["recording_mode"].get() == "일정 간격으로 분할" else "window"
        for name, label, integer in (("segment_minutes", "파일 분할 길이", False), ("retention_days", "보관 일수", True),
                                     ("min_free_gb", "최소 여유 공간", False)):
            values[name] = desktop_number(self.form[name].get(), label, integer)
        values["weather_condition"] = {
            "enabled": self.form["weather_enabled"].get(), "provider": self._provider_id(),
            "region": {key: self.form[key].get() for key in ("sido", "sigungu", "eupmyeondong")},
            "conditions": [key for key, var in self.condition_vars.items() if var.get()],
            "poll_minutes": desktop_number(self.form["poll_minutes"].get(), "예보 조회 간격"),
            "api_key_file": self.form["api_key_file"].get().strip(), "met_contact": self.form["met_contact"].get().strip(),
        }
        return values

    def _load(self, result):
        config = result["config"]
        self.loading = True
        try:
            for key, default in (("setup_ini", "auto"), ("output_dir", "auto"), ("intersection_name", ""), ("weather", ""),
                                 ("segment_minutes", 30), ("retention_days", 0), ("min_free_gb", 5)):
                self.form[key].set(str(config.get(key, default)))
            recording_mode = config.get("recording_mode", "split" if "segment_minutes" in config or "segment_seconds" in config else "window")
            self.form["recording_mode"].set("일정 간격으로 분할" if recording_mode == "split" else "예약 구간 전체 한 파일")
            self._recording_mode_fields()
            connection = config.get("camera_connection") or {"mode": "setup"}
            mode = connection.get("mode", "setup")
            self.camera_mode_box.configure(values=[label for key, label in DESKTOP_CAMERA_MODES
                                                   if key != "custom" or mode == "custom"])
            self.form["camera_mode"].set(next((label for key, label in DESKTOP_CAMERA_MODES if key == mode),
                                              DESKTOP_CAMERA_MODES[0][1]))
            for key, default in (("ips", ""), ("username", ""), ("port", 554), ("path", "/profile2/media.smp")):
                self.form["camera_" + key].set(str(connection.get(key, default)))
            self.form["camera_password"].set("")
            self.camera_ips.delete("1.0", "end")
            self.camera_ips.insert("1.0", self.form["camera_ips"].get())
            self.camera_ips.edit_modified(False)
            self.camera_password_note.set("비밀번호가 저장되어 있습니다. 빈칸이면 저장된 비밀번호를 유지합니다."
                                          if connection.get("password_saved") else "카메라에 설정된 비밀번호를 입력하세요.")
            self._camera_mode_fields()
            self.form["recording_dates"].set(", ".join(config.get("recording_dates", [])))
            for row, _, _ in self.rows:
                row.destroy()
            self.rows = []
            for value in config.get("time_ranges", DEFAULT_TIME_RANGES):
                self.add_range(value)
            self.schedule_note.set("기존 설정에 이 화면에서 표현할 수 없는 시간표가 있습니다. 시간 구간을 추가하면 기존 시간표를 대체합니다."
                                   if config.get("legacy_schedule") else "")
            weather = config.get("weather_condition") or default_weather_config()
            self.form["weather_enabled"].set(weather.get("enabled", False))
            # An existing weather object without provider is the legacy KMA mode.
            provider = weather.get("provider", "data_go_kr")
            self.form["provider"].set(next((label for key, label in DESKTOP_PROVIDERS if key == provider), DESKTOP_PROVIDERS[0][1]))
            self.region_rows = result["regions"]["rows"]
            self.region_boxes[0].configure(values=sorted({row[0] for row in self.region_rows}))
            for key in ("sido", "sigungu", "eupmyeondong"):
                self.form[key].set(weather.get("region", {}).get(key, ""))
            self._region_changed(0)
            for name, var in self.condition_vars.items():
                var.set(name in weather.get("conditions", ["rain"]))
            for key, default in (("poll_minutes", 30), ("api_key_file", "kma_service.key"), ("met_contact", "")):
                self.form[key].set(str(weather.get(key, default)))
            self._provider_fields()
            self.revision = result["revision"]
            try:
                self.baseline = self.collect()
            except ValueError:
                # Let an existing invalid numeric/date value be corrected in the
                # form instead of trapping the user behind a failed first load.
                self.baseline = {key: value for key, value in config.items() if key in (
                    "setup_ini", "output_dir", "intersection_name", "weather", "recording_dates", "time_ranges",
                    "recording_mode", "segment_minutes", "retention_days", "min_free_gb", "weather_condition", "camera_connection")}
            self.loaded, self.dirty = True, False
            self.dirty_text.set("변경 사항을 저장했습니다.")
        finally:
            self.loading = False

    def reload(self, initial=False):
        if self.dirty and not self.messagebox.askyesno("설정 다시 불러오기", "저장하지 않은 변경을 버리고 파일에서 다시 불러올까요?", parent=self.root):
            return
        def done(value):
            self._load(value)
            if value.get("setup_required"):
                self.notice.set(value.get("setup_message") or "카메라 연결 정보를 먼저 등록하세요. 날씨 기능을 사용할 때는 지역도 직접 선택하세요.")
                if initial:
                    self.notebook.select(self.device_tab.master.master)
            else:
                self.notice.set("카메라 설정과 저장 위치를 확인하고 시험 녹화를 시작하세요." if initial else "저장된 설정을 불러왔습니다.")
            self._request_state()
        self.action(self.controller.configuration, done, "설정을 불러오고 있습니다.")

    def _patch(self):
        if not self.loaded:
            raise ValueError("설정을 먼저 불러오세요. 현재 설정 파일의 경로와 JSON 형식을 확인하세요.")
        return desktop_config_patch(self.baseline, self.collect())

    def save(self):
        self.with_settings(None, explicit=True)

    def with_settings(self, operation, explicit=False):
        if self.pending_action or self.closing:
            return
        try:
            patch = self._patch()
        except (ValueError, TypeError) as exc:
            self._error(exc)
            return
        if patch and not explicit and not self.messagebox.askyesno("설정 저장", "화면에서 변경한 설정을 저장하고 계속할까요?", parent=self.root):
            return
        revision = self.revision
        def work():
            saved = self.controller.save(patch, revision) if patch else {"message": "변경된 설정이 없습니다."}
            config = self.controller.configuration() if patch else None
            # Preserve a successful save even if the following check/start fails.
            try:
                result = operation() if operation is not None else saved
                return {"config": config, "result": result, "operation_error": None}
            except Exception as exc:
                return {"config": config, "result": saved, "operation_error": exc}
        def done(value):
            if value["config"]:
                self._load(value["config"])
            else:
                self.dirty = False
                self.dirty_text.set("저장된 설정을 사용합니다.")
            result = value["result"]
            if isinstance(result, dict) and result.get("message"):
                self.notice.set(result["message"])
            if value["operation_error"] is not None:
                self._error(value["operation_error"])
            self._request_state()
        self.action(work, done, "설정을 적용하고 있습니다." if patch else "요청을 처리하고 있습니다.")

    def start_once(self):
        try:
            minutes = desktop_number(self.form["once_minutes"].get(), "녹화 길이")
            start = self.form["once_time"].get().strip() if self.form["once_later"].get() else None
            make_once_window(minutes, start)
        except ValueError as exc:
            self._error(exc)
            return
        self.with_settings(lambda: self.controller.start("once", minutes, start))

    def save_key(self):
        key = self.key_input.get()
        if not key.strip():
            self._error(ValueError("등록할 인증키를 입력하세요."))
            return
        self.with_settings(lambda: self.controller.save_key(key))
        self.key_input.delete(0, "end")

    def open_output(self):
        value = self.last_state.get("output_dir")
        if not value:
            self._error(ValueError("저장 위치를 먼저 설정하고 저장하세요."))
            return
        def open_folder():
            path = Path(value)
            if not path.is_dir():
                raise ValueError("아직 저장 폴더가 없습니다. 녹화를 시작하면 생성합니다.")
            executable = shutil.which("xdg-open")
            if not executable:
                raise ValueError("파일 관리자를 찾을 수 없습니다. 저장 위치: " + str(path))
            subprocess.Popen([executable, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"message": "저장 폴더를 열었습니다."}
        self.action(open_folder)

    def _worker(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            kind, function, callback = job
            try:
                self.results.put((kind, callback, function(), None))
            except Exception as exc:
                self.results.put((kind, callback, None, exc))

    def _set_busy(self, busy):
        self.camera_ips.configure(state="disabled" if busy else "normal")
        if busy:
            self.disabled_widgets = []
            def visit(parent):
                for child in parent.winfo_children():
                    if isinstance(child, (self.ttk.Entry, self.ttk.Button, self.ttk.Combobox, self.ttk.Checkbutton)):
                        self.disabled_widgets.append((child, child.state()))
                        child.state(["disabled"])
                    visit(child)
            visit(self.root)
        else:
            for widget, states in self.disabled_widgets:
                if widget.winfo_exists():
                    widget.state(["!disabled", "!readonly"])
                    widget.state(states)
            self.disabled_widgets = []
            self._update_buttons()

    def action(self, function, callback=None, message="요청을 처리하고 있습니다."):
        if self.pending_action or self.closing:
            return
        self.pending_action = True
        self._set_busy(True)
        self.notice.set(message)
        self.jobs.put(("action", function, callback))

    def _request_state(self):
        if not self.poll_pending and not self.closing:
            self.poll_pending = True
            self.jobs.put(("state", self.controller.state, self._show_state))

    def _poll(self):
        if self.closing:
            return
        if not self.pending_action and self.loaded:
            self._request_state()
        self.root.after(2000, self._poll)

    def _pump(self):
        try:
            while True:
                kind, callback, value, error = self.results.get_nowait()
                if kind == "state":
                    self.poll_pending = False
                if kind == "action":
                    self.pending_action = False
                    self._set_busy(False)
                if error is not None:
                    if kind == "close":
                        self.closing = False
                        self.controller.closing = False
                        self.pending_action = False
                        self._set_busy(False)
                        self.root.after(2000, self._poll)
                    if kind == "state":
                        self.notice.set(friendly_ui_error(error))
                    else:
                        self._error(error)
                elif callback is not None:
                    callback(value)
                    if self.destroyed:
                        return
                elif isinstance(value, dict) and value.get("message"):
                    self.notice.set(value["message"])
                    self._request_state()
        except queue.Empty:
            pass
        except Exception as exc:
            self._error(exc)
        if not self.destroyed and self.root.winfo_exists():
            self.root.after(75, self._pump)

    def _error(self, exc):
        message = friendly_ui_error(exc)
        self.notice.set(message)
        self.messagebox.showerror("확인이 필요합니다", message, parent=self.root)

    def _update_buttons(self):
        if self.pending_action or self.closing:
            return
        running = self.last_state.get("running", False)
        external = self.last_state.get("external_running", False)
        for button in (self.start_button, self.once_button):
            button.state(["disabled"] if running or external or self.last_state.get("setup_required") else ["!disabled"])
        self.stop_button.state(["!disabled"] if running else ["disabled"])
        self._once_mode()

    def _show_state(self, state):
        self.last_state = state
        running, external, active = state.get("running"), state.get("external_running"), state.get("active_cameras", [])
        if external:
            label = "별도 서비스가 녹화 중입니다"
        elif running and active:
            label = "녹화 중 · {}대".format(len(active))
        elif running:
            label = "한 번 녹화 준비 · 대기" if state.get("mode") == "once" else "예약 녹화 대기 중"
        elif state.get("setup_required"):
            label = "카메라 등록 필요"
        else:
            label = "녹화 중지"
        self.state_text.set(label)
        cameras = state.get("cameras", [])
        text = "대상 카메라 {}대  ·  남은 공간 {} GiB".format(len(cameras), state.get("disk", {}).get("free_gb", "—"))
        if state.get("once_end") and running:
            text += "  ·  종료 " + state["once_end"].replace("T", " ")[:19] + " KST"
        if state.get("output_dir"):
            text += "\n저장 위치: " + state["output_dir"]
        if external:
            text += "\n기존 서비스를 먼저 중지하면 이 창에서 녹화를 시작할 수 있습니다. 이 창은 별도 서비스를 종료하지 않습니다."
        if state.get("error"):
            text += "\n" + state["error"]
        self.summary_text.set(text)
        rows = [(camera.get("client_no", ""), camera.get("intersection", ""), camera.get("direction", ""),
                 "녹화 중" if camera.get("name") in active else "대기" if running or external else "중지") for camera in cameras]
        old_rows = [tuple(self.camera_tree.item(item, "values")) for item in self.camera_tree.get_children()]
        if old_rows != [tuple(str(value) for value in row) for row in rows]:
            self.camera_tree.delete(*self.camera_tree.get_children())
            for row in rows:
                self.camera_tree.insert("", "end", values=row)
        weather = dict(state.get("weather") or {})
        # A manual forecast returns an enabled preview even with the saved rule
        # off; the UI must describe the saved recording rule, not that preview.
        weather["enabled"] = bool((self.baseline.get("weather_condition") or {}).get("enabled"))
        self.weather_text.set(desktop_forecast_text(weather, state.get("forecast_busy")))
        logs = "\n".join(state.get("logs", []))
        if logs != self.last_logs:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.insert("1.0", logs or "실행 기록이 없습니다. 녹화를 시작하면 여기에 표시합니다.")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
            self.last_logs = logs
        self._update_buttons()

    def request_close(self, force=False):
        if self.closing:
            return
        if self.pending_action:
            self.notice.set("진행 중인 작업이 끝난 뒤 창을 종료해 주세요.")
            return
        running = self.last_state.get("running", False)
        messages = []
        if running:
            messages.append("이 창에서 실행한 녹화를 중지하고 현재 영상 파일을 마무리합니다.")
        if self.dirty:
            messages.append("저장하지 않은 설정 변경은 버립니다.")
        if messages and not force and not self.messagebox.askyesno("녹화 도구 종료", "\n\n".join(messages) + "\n\n종료할까요?", parent=self.root):
            return
        self.closing = True
        self._set_busy(True)
        self.notice.set("녹화를 정상 종료하고 있습니다. 영상 파일을 마무리할 때까지 기다려 주세요.")
        self.jobs.put(("close", self.controller.close, self._closed))

    def _closed(self, _result):
        self.jobs.put(None)
        self.destroyed = True
        # Tk retains scheduled Tcl commands after widgets are destroyed. Cancel
        # this dedicated interpreter's timers before removing their callbacks.
        for pending in self.root.tk.call("after", "info"):
            self.root.after_cancel(pending)
        self.root.destroy()


def desktop_signal_watch(root, app):
    """Relay terminal/session termination to the Tk thread and finalize owned video."""
    stop_requested = threading.Event()
    previous = {}

    def receive(_number, _frame):
        # Python handlers may interrupt a Tk call; defer every GUI operation.
        stop_requested.set()

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            value = getattr(signal, name, None)
            if value is not None and value not in previous:
                previous[value] = signal.getsignal(value)
                signal.signal(value, receive)

    def poll():
        if app.destroyed:
            return
        if stop_requested.is_set() and not app.pending_action:
            stop_requested.clear()
            app.request_close(force=True)
        if not app.destroyed:
            root.after(100, poll)

    root.after(100, poll)

    def restore():
        for number, handler in previous.items():
            signal.signal(number, handler)

    return restore


def run_recorder_gui(config_path):
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        raise RuntimeError("데스크톱 GUI에 필요한 Tk가 없습니다. Ubuntu에서 sudo apt install python3-tk 명령으로 설치하세요. 브라우저 방식은 --ui를 사용하세요.") from None
    try:
        root = tk.Tk()
    except tk.TclError:
        raise RuntimeError("데스크톱 화면에 연결하지 못했습니다. Linux 데스크톱에서 실행하세요. SSH 접속에서는 --ui와 SSH 포트 전달을 사용하세요.") from None
    controller = RecorderUiController(config_path)
    app = None
    restore_signals = lambda: None
    try:
        app = RecorderDesktopApp(root, tk, ttk, (filedialog, messagebox), controller)
        controller.camera_ping.start()
        restore_signals = desktop_signal_watch(root, app)
        root.mainloop()
    finally:
        restore_signals()
        # A keyboard interruption must also finalize owned MP4 files.
        if app is None or not app.closing:
            controller.close()
# END EMBEDDED DESKTOP_GUI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        help="INI or JSON configuration (default: recording.ini beside this script if present)")
    parser.add_argument("--setup-ini", type=Path,
                        help="Camera setup.ini (default: nearby project, then search /home/dis)")
    parser.add_argument("--cameras", nargs="+", metavar="CAM", help="Camera sections, e.g. CAM1 CAM2 (default: INUSE=True sections)")
    parser.add_argument("--time-ranges", nargs="+", metavar="HH:MM-HH:MM",
                        help="Daily Korean-time windows (default: 06:30-07:30 10:00-11:00 12:00-13:00 17:00-18:00 21:00-22:00)")
    parser.add_argument("--start-times", nargs="+", metavar="HH:MM",
                        help="Alternative shared-duration mode: daily starts in Korean time")
    parser.add_argument("--duration-hours", type=float,
                        help="Shared hours per start (default: 2 with custom --start-times)")
    parser.add_argument("--record-minutes", type=float, metavar="MINUTES",
                        help="Record once for this many minutes, then exit; overrides configured dates/time ranges")
    parser.add_argument("--record-camera", metavar="CAMERA_NAME",
                        help="With --record-minutes, record only this saved camera; leaves configuration unchanged")
    parser.add_argument("--start-at", metavar="HH:MM",
                        help="With --record-minutes, start once at this future Korean time today (default: now)")
    parser.add_argument("--output-dir", type=Path, help="Storage path (default: project's runtime/scheduled_recordings)")
    parser.add_argument("--retention-days", type=int, help="Days to keep recordings; 0 disables deletion (default: 0)")
    parser.add_argument("--min-free-gb", type=float, help="Pause below this free GiB (default: 5)")
    parser.add_argument("--ffmpeg", help="FFmpeg executable (default: ffmpeg)")
    parser.add_argument("--user", help="Service account for --install/--print-unit (default: sudo caller)")
    parser.add_argument("--ui", action="store_true", help="Open the local browser control panel (JSON settings)")
    parser.add_argument("--ui-port", type=int, default=8765, help="Local UI port (default: 8765)")
    parser.add_argument("--ui-autostart", action="store_true", help="Start the configured recording when the UI starts")
    parser.add_argument("--gui", action="store_true", help="Open the native desktop window (requires python3-tk)")
    parser.add_argument("--camera-profiles", metavar="CAMERA_NAME", help="Preview HD profile2 / 4K profile4 setup for one saved camera")
    parser.add_argument("--apply-camera-profiles", action="store_true", help="With --camera-profiles, back up and apply the camera profile plan")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--init-config", action="store_true",
                           help="Create recording.ini, or JSON with --config PATH.json; never overwrite existing settings")
    operation.add_argument("--check", action="store_true",
                        help="Validate configuration and FFmpeg; no connections, writes or recording")
    operation.add_argument("--install", action="store_true",
                           help="Install/start the boot service; create recording.ini with defaults if missing")
    operation.add_argument("--print-unit", action="store_true", help="Preview the service unit without installing")
    operation.add_argument("--uninstall", action="store_true", help="Stop/remove the managed service; preserve recordings")
    operation.add_argument("--desktop-install", action="store_true",
                           help="Add ED Camera Recorder to the current user's Linux application menu; no sudo")
    parser.add_argument("--recording-mode", choices=("window", "split"),
                        help="window: one MP4 per recording window (new default); split: periodic MP4 files")
    parser.add_argument("--segment-minutes", type=float,
                        help="Set split interval and opt into split mode unless --recording-mode is also given")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    storage, recorder, weather_gate = None, None, None
    exit_status = 0
    try:
        if args.record_camera is not None:
            if args.record_minutes is None:
                raise ValueError("--record-camera requires --record-minutes")
            if any((args.gui, args.ui, args.ui_autostart, args.install, args.uninstall, args.print_unit,
                    args.init_config, args.desktop_install, args.camera_profiles, args.apply_camera_profiles)):
                raise ValueError("개별 녹화는 웹 화면의 '한 번만 녹화 → 녹화 대상'에서 선택하세요. "
                                 "명령줄에서는 실행.sh 대신 python3 record.py --config recording.json "
                                 "--record-minutes 1 --record-camera 카메라명으로 실행하세요.")
        if args.apply_camera_profiles and not args.camera_profiles:
            raise ValueError("--apply-camera-profiles에는 --camera-profiles 카메라명이 필요합니다.")
        if args.camera_profiles:
            if any((args.gui, args.ui, args.ui_autostart, args.install, args.uninstall, args.print_unit,
                    args.init_config, args.check, args.desktop_install)) or any(value is not None for value in
                    (args.setup_ini, args.cameras, args.time_ranges, args.start_times, args.duration_hours,
                     args.record_minutes, args.start_at, args.output_dir, args.retention_days,
                     args.min_free_gb, args.ffmpeg, args.user, args.segment_minutes, args.recording_mode)):
                raise ValueError("카메라 프로필 명령은 --config와 함께 별도로 실행하세요.")
            camera_config = args.config or Path(__file__).resolve().with_name("recording.json")
            result = camera_profiles_cli(camera_config, args.camera_profiles, apply=args.apply_camera_profiles)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.gui or args.desktop_install:
            conflicts = (args.ui, args.ui_autostart, args.install, args.uninstall, args.print_unit,
                         args.init_config, args.check)
            overrides = (args.setup_ini, args.cameras, args.time_ranges, args.start_times, args.duration_hours,
                         args.record_minutes, args.start_at, args.output_dir, args.retention_days,
                         args.min_free_gb, args.ffmpeg, args.user, args.segment_minutes, args.recording_mode)
            if any(conflicts) or any(value is not None for value in overrides):
                raise ValueError("--gui/--desktop-install은 --config recording.json과 함께 사용하세요. 녹화 옵션은 화면에서 설정합니다.")
            gui_path = (Path(args.config).expanduser().absolute() if args.config
                        else Path(__file__).resolve().with_name("recording.json"))
            if gui_path.suffix.lower() != ".json":
                raise ValueError("데스크톱 화면에는 recording.json 설정을 사용하세요. 기존 INI 파일은 유지됩니다.")
            if args.desktop_install:
                print(install_desktop_shortcut(gui_path)["message"])
                if not args.gui:
                    return 0
            if not gui_path.exists():
                create_json_config(gui_path)
            run_recorder_gui(gui_path)
            return 0
        if args.ui_autostart and not args.ui:
            raise ValueError("--ui-autostart requires --ui")
        if args.ui:
            if args.record_minutes is not None or args.start_at is not None or args.init_config or args.uninstall:
                raise ValueError("Use recording controls in the UI; --ui cannot be combined with this operation")
            number(args.ui_port, "ui-port", 1024, 65535, True)
            if (args.install or args.print_unit) and (args.config is None or Path(args.config).suffix.lower() != ".json"):
                raise ValueError("Install the UI with --config recording.json --ui --install")
            if not (args.install or args.print_unit or args.check):
                ui_path = Path(args.config) if args.config else Path(__file__).resolve().with_name("recording.json")
                if ui_path.suffix.lower() != ".json":
                    raise ValueError("The browser UI requires a JSON configuration")
                if not ui_path.exists():
                    create_json_config(ui_path, overrides=args)
                run_recorder_ui(ui_path, args.ui_port, args.ui_autostart)
                return 0
        once_window = ()
        if args.start_at is not None and args.record_minutes is None:
            raise ValueError("--start-at requires --record-minutes")
        if args.record_minutes is not None:
            if args.install or args.print_unit or args.uninstall or args.init_config:
                raise ValueError("--record-minutes is for a one-time run or --check, not service/config operations")
            if any(value is not None for value in (args.time_ranges, args.start_times, args.duration_hours)):
                raise ValueError("--record-minutes cannot be combined with recurring schedule options; use --start-at")
            once_window = make_once_window(args.record_minutes, args.start_at)
        if args.init_config:
            init_path = args.config if args.config is not None else default_ini_path()
            suffix = Path(init_path).suffix.lower()
            if suffix not in (".ini", ".json"):
                raise ValueError("--init-config requires an .ini or .json file path")
            owner = None
            if sys.platform == "linux" and os.geteuid() == 0 and (args.user or os.environ.get("SUDO_USER")):
                owner = resolve_service_user(args.user)
            create = create_json_config if suffix == ".json" else create_ini_config
            created = create(init_path, overrides=args, owner=owner)
            print(("Created " if created else "Kept existing ") + str(Path(init_path).expanduser().absolute()))
            return 0
        if args.install or args.print_unit or args.uninstall:
            if sys.platform != "linux":
                raise ValueError("Service management requires Linux with systemd")
        if args.uninstall:
            if os.geteuid() != 0:
                raise ValueError("Use sudo for --uninstall")
            uninstall_service()
            return 0
        standalone_flags = (args.setup_ini, args.cameras, args.time_ranges, args.start_times, args.duration_hours,
                            args.output_dir, args.retention_days, args.min_free_gb, args.ffmpeg)
        has_runtime_options = (any(value is not None for value in standalone_flags)
                               or args.segment_minutes is not None or args.recording_mode is not None)
        config_path = Path(args.config).expanduser().absolute() if args.config else default_ini_path()
        ini_mode = (config_path.suffix.lower() == ".ini" and
                    (args.config is not None or config_path.exists() or config_path.is_symlink() or args.install))
        install_user = None
        if args.install and ini_mode:
            if os.geteuid() != 0:
                raise ValueError("Use sudo for --install")
            install_user = resolve_service_user(args.user)
            exists = config_path.exists() or config_path.is_symlink()
            if has_runtime_options and (exists or args.config is not None):
                raise ValueError("Edit recording.ini for service settings; do not pass runtime overrides with --install")
            created = create_ini_config(config_path, overrides=args, owner=install_user)
            if created:
                LOG.info("Created recorder settings: %s", config_path)
            # First-install CLI choices were written into the new INI. The unit
            # must only reference the file so future edits are read on restart.
            for key in ("setup_ini", "cameras", "time_ranges", "start_times", "duration_hours",
                        "output_dir", "retention_days", "min_free_gb", "ffmpeg", "segment_minutes", "recording_mode"):
                setattr(args, key, None)
            has_runtime_options = False
        if ini_mode:
            if args.print_unit and has_runtime_options:
                raise ValueError("Edit recording.ini for service settings; omit runtime overrides with --print-unit")
            LOG.info("Recorder settings: %s", config_path)
            settings, setup_path = load_ini_settings(config_path, overrides=args)
            args.config = config_path.resolve()
            LOG.info("Camera setup: %s", setup_path)
        elif args.config:
            if any(value is not None for value in standalone_flags):
                raise ValueError("--config cannot be combined with standalone camera/schedule/storage options")
            LOG.info("Recorder settings: %s", config_path)
            settings, setup_path = load_settings(args.config), None
        else:
            settings, setup_path = standalone_settings(args)
            LOG.info("Camera setup: %s", setup_path)
        if args.segment_minutes is not None:
            settings = replace(settings, segment_seconds=minutes_to_seconds(args.segment_minutes),
                               recording_mode=args.recording_mode or "split")
        if args.recording_mode is not None:
            settings = replace(settings, recording_mode=args.recording_mode)
        if once_window:
            settings = replace(settings, once_window=once_window)
        if args.record_camera is not None:
            settings = select_once_camera(settings, args.record_camera)
        if args.install or args.print_unit:
            user = install_user or resolve_service_user(args.user)
            runtime_args = (["--config", str(config_path.resolve())] if ini_mode
                            else service_arguments(args, settings, setup_path))
            if args.ui:
                if ini_mode or not args.config or Path(args.config).suffix.lower() != ".json":
                    raise ValueError("Install the UI with --config recording.json --ui --install")
                runtime_args += ["--ui", "--ui-port", str(args.ui_port), "--ui-autostart"]
            if args.print_unit:
                print(render_service_unit(runtime_args, user), end="")
            else:
                if os.geteuid() != 0:
                    raise ValueError("Use sudo for --install")
                install_service(runtime_args, user)
            return 0
        check_output_access(settings)
        executable, timeout_option = check_ffmpeg(settings)
        if args.check:
            LOG.info("Configuration OK: cameras=%s, daily schedules=%d, recording_dates=%s, timezone=Asia/Seoul, "
                     "format=MP4, recording_mode=%s, segment_minutes=%g, output=%s",
                     ",".join(camera.name for camera in settings.cameras), len(settings.schedules),
                     ",".join(day.isoformat() for day in settings.recording_dates) or "every day",
                     settings.recording_mode, settings.segment_seconds / 60, settings.output_dir)
            for camera in settings.cameras:
                LOG.info("%s: filename prefix=%s", camera.name, camera_filename_prefix(camera))
            if settings.once_window:
                log_once_window(settings.once_window)
            return 0
        if sys.platform != "linux":
            raise ValueError("Recording service requires Linux; --check is available on other systems")
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        storage = Storage(settings)
        storage.acquire()
        if once_window:
            # Immediate runs count from readiness, not from config/FFmpeg checks.
            # An explicitly requested start stays on the originally chosen day.
            now = datetime.now(KST)
            if args.start_at is None:
                once_window = make_once_window(args.record_minutes, now=now)
            elif once_window[0] < now:
                raise ValueError("--start-at passed during startup; choose a later time or omit it to start now")
            settings = replace(settings, once_window=once_window)
            storage.settings = settings
            log_once_window(once_window)
        if settings.weather_condition is not None and settings.weather_condition.enabled and not once_window:
            weather_gate = WeatherGate(settings.weather_condition, settings.output_dir)
            signal.signal(signal.SIGUSR1, lambda *_: weather_gate.wake.set())
            weather_gate.start()
        recorder = Recorder(settings, executable, timeout_option, storage, weather_gate=weather_gate)
        LOG.info("Recorder started: %d cameras; all schedules use Asia/Seoul; "
                 "recording_dates=%s; format=MP4, recording_mode=%s, segment_minutes=%g", len(settings.cameras),
                 "one-time window" if settings.once_window else (
                     ",".join(day.isoformat() for day in settings.recording_dates) or "every day"),
                 settings.recording_mode, settings.segment_seconds / 60)
        while not stop.is_set():
            recorder.tick()
            write_recorder_status(settings, recorder, weather_gate)
            if settings.once_window and datetime.now(KST) >= settings.once_window[1]:
                LOG.info("One-time recording window ended; shutting down")
                break
            stop.wait(1)
    except (OSError, ValueError, RuntimeError, configparser.Error, subprocess.SubprocessError) as exc:
        # ConfigParser errors may contain an entire URL line. Do not echo them.
        if isinstance(exc, configparser.Error):
            detail = "Invalid setup.ini structure"
        elif isinstance(exc, subprocess.SubprocessError):
            detail = "External process failed ({})".format(type(exc).__name__)
        else:
            detail = str(exc)
        LOG.error("Recorder failed: %s", detail)
        if args.gui:
            show_desktop_error(friendly_ui_error(exc))
        exit_status = 1
    finally:
        if weather_gate is not None:
            weather_gate.close()
        try:
            if recorder:
                recorder.close()
                write_recorder_status(settings, recorder, weather_gate, finished=True)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            LOG.error("Recorder shutdown failed; check remaining FFmpeg processes")
            exit_status = 1
        finally:
            if storage:
                storage.close()
    return exit_status


if __name__ == "__main__":
    sys.exit(main())
