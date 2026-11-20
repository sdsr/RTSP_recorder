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
    "+8yJ/wWRyo6i"
)

EMBEDDED_SWITCH_NETWORK = (
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
import socket as _live_socket
import subprocess
import threading
import time
import re
from types import SimpleNamespace as _LiveSettings
from urllib.parse import urlsplit


class LiveViewError(ValueError):
    """Safe to display: never includes a camera URL or subprocess stderr."""


def live_view_port_settings(config, base):
    """The first camera can be previewed before the recorder has a camera list."""
    executable = config.get("ffmpeg", "ffmpeg")
    if not isinstance(executable, str) or not executable or any(ord(c) < 32 for c in executable):
        raise LiveViewError("녹화기의 FFmpeg 실행 파일 설정을 확인하세요.")
    if "/" in executable or "\\" in executable:
        executable = str(resolve_path(executable, base, "ffmpeg"))
    return _LiveSettings(ffmpeg=executable)


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
                self.error = "카메라 인증에 실패했습니다. 카메라 계정·비밀번호 또는 로그인 잠금을 확인하세요. 자동 재시도하지 않습니다."
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
    def __init__(self, settings_callback, port_settings_callback=None):
        self._settings = settings_callback
        self._port_settings = port_settings_callback or settings_callback
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
            info = live_view_camera_info(camera)
            if profile != "current":
                info["profile"] = profile
            return self._start_url(settings, url, info)

    def start_port(self, observation, username, password, profile="2"):
        """Preview the one camera identified by the controller's fresh switch-port lookup."""
        if not isinstance(observation, dict):
            raise LiveViewError("카메라를 연결한 포트를 다시 조회하세요.")
        port = observation.get("direct_port")
        if type(port) is not int or not 1 <= port <= 8:
            raise LiveViewError("확인된 카메라 LAN 포트를 선택하세요.")
        if (not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,8}", username)
                or not isinstance(password, str) or not 1 <= len(password) <= 64
                or any(ord(char) < 32 or ord(char) == 127 for char in password)):
            raise LiveViewError("카메라에 설정한 현재 아이디와 비밀번호를 입력하세요.")
        # Factory profiles differ by model. Accept a profile number from the
        # camera's list, never a client-supplied URL, hostname, path or port.
        if type(profile) not in (str, int) or not re.fullmatch(r"[1-9][0-9]?", str(profile)):
            raise LiveViewError("미리볼 영상 프로파일 번호를 선택하세요.")
        profile = str(profile)
        ip, mac = _cai_ip(observation.get("ip")), _cai_mac(observation.get("mac"))
        source, interface = _cai_ip(observation.get("source")), observation.get("interface")

        def direct_route():
            # FFmpeg opens its own connection using the kernel's normal route.
            route = _cai_route(ip)
            if route["dev"] != interface or route["source"] != source:
                raise LiveViewError("카메라 경로가 조회 때와 다릅니다. 포트를 다시 조회하세요.")
            return route

        def verify_neighbor():
            if _cai_neighbor(ip, direct_route()) != mac:
                raise LiveViewError("현재 IP의 장치가 선택한 포트의 MAC과 다릅니다. 포트를 다시 조회하세요.")

        with self._start_lock:
            route = direct_route()
            _camera_admin_duplicate_probe(ip, dict(route, expected_mac=mac))
            # A raw ARP broadcast need not create a kernel neighbor. Opening
            # RTSP TCP without credentials first lets us verify the MAC FFmpeg
            # will actually use before forwarding the operator's credentials.
            try:
                with _live_socket.create_connection((ip, 554), timeout=3, source_address=(source, 0)):
                    pass
            except OSError:
                raise LiveViewError("카메라의 RTSP 포트 554에 연결할 수 없습니다.") from None
            verify_neighbor()
            settings = self._port_settings()
            url = _ca_url("rtsp://{}:554/profile{}/media.smp".format(ip, profile),
                          username=username, password=password)
            info = {"name": "LAN {} 카메라".format(port), "ip": ip, "port": port, "mac": mac,
                    "profile": profile, "can_select_profile": True}
            return self._start_url(settings, url, info, verify_ready=verify_neighbor)

    def _start_url(self, settings, url, info, verify_ready=None):
        try:
            executable, timeout_option = check_ffmpeg(settings)
        except (ValueError, OSError, subprocess.SubprocessError):
            raise LiveViewError("FFmpeg 실행 파일과 RTSP 지원 설정을 확인하세요.") from None
        command = [executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-rtsp_transport", "tcp", timeout_option, "10000000", "-i", url,
                   "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "copy",
                   "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                   "-avoid_negative_ts", "make_zero", "-flush_packets", "1", "pipe:1"]
        with self._lock:
            if self._closed:
                raise LiveViewError("미리보기 서비스가 종료되었습니다.")
            self.stop()
            session = _LiveViewSession(command, info)
            self._session = session
        try:
            result = session.ready()
            if verify_ready is not None:
                verify_ready()
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
import sys as _sm_sys
import threading as _sm_threading
import time as _sm_time
from contextlib import contextmanager as _sm_contextmanager


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
    if len(routes) != 1 or routes[0].get('dev') != interface or routes[0].get('gateway') or routes[0].get('type') in ('local', 'broadcast', 'unreachable', 'blackhole', 'prohibit'):
        raise SwitchManagerError('허브로 가는 직접 enp2 경로가 없습니다. LTE 기본 경로를 사용하지 않습니다.')
    if target == source:
        raise SwitchManagerError('허브 주소와 ED 소스 주소는 달라야 합니다.')
    return {'dev': interface, 'source': source, 'parent': current['ifname'], 'local_mac': _sm_mac(nic['address'])}


def _sm_no_collision(connection, target):
    if target == connection['target']:
        return
    probe = globals().get('_camera_admin_address_available')
    if not callable(probe):
        raise SwitchManagerError('새 관리 IP 중복 검사 구성요소가 없습니다. 카메라 관리 업데이트를 전체 적용한 뒤 다시 시도하세요.')
    try:
        probe(target, {'dev': connection['interface'], 'source': connection['source']})
    except ValueError as exc:
        # The shared parser understands both Habets and iputils arping and
        # distinguishes missing raw-packet permission from an unused address.
        if type(exc).__name__ == 'CameraActivationError':
            raise SwitchManagerError(str(exc)) from None
        raise SwitchManagerError('새 관리 IP 중복 검사 응답을 확인하지 못했습니다. 변경하지 않습니다.') from None
    except (OSError, _sm_subprocess.SubprocessError):
        raise SwitchManagerError('새 관리 IP 중복 검사를 완료하지 못했습니다. 변경하지 않습니다.') from None


def _sm_fdb_entries(raw):
    """Parse observed forwarding entries; never infer cable endpoints from them."""
    entries = set()
    for row in _sm_clean(raw).splitlines():
        mac = _sm_re.search(_SM_MAC, row)
        if not mac:
            continue
        vlan = _sm_re.match(r'^\s*(\d+)\s+', row)
        vlan_id = int(vlan.group(1)) if vlan and 1 <= int(vlan.group(1)) <= 4094 else None
        for port in _sm_re.findall(r'Ethernet1/0/(\d+)\b', row, _sm_re.I):
            if 1 <= int(port) <= 10:
                entries.add((_sm_mac(mac.group()), int(port), vlan_id))
    return [{'mac': mac, 'port': port, 'vlan': vlan}
            for mac, port, vlan in sorted(entries, key=lambda entry: (entry[0], entry[1], entry[2] or 0))]


def _sm_link_parent(interface, links):
    by_name = {row['ifname'].split('@')[0]: row for row in links}
    by_index = {row['ifindex']: row for row in links}
    current, seen = by_name.get(interface), set()
    while current:
        if _sm_re.fullmatch(r'enp2[0-9A-Za-z]*', current.get('ifname', '')):
            return current['ifname']
        if current['ifindex'] in seen or current.get('linkinfo', {}).get('info_kind') != 'vlan':
            return None
        seen.add(current['ifindex'])
        parent = current.get('link')
        current = by_name.get(parent) if isinstance(parent, str) else by_index.get(current.get('link_index', parent))
    return None


def _sm_local_interfaces():
    """Offer only already configured enp2 addresses; never create a route/IP."""
    links = _sm_ip_json('-d', 'link', 'show')
    by_name = {row['ifname'].split('@')[0]: row for row in links}
    result = []
    for row in _sm_ip_json('-4', 'address', 'show'):
        interface = row.get('ifname', '').split('@')[0]
        parent = _sm_link_parent(interface, links)
        if not parent:
            continue
        for address in row.get('addr_info', []):
            if address.get('family') != 'inet':
                continue
            try:
                source = _sm_ip(address.get('local'))
                prefixlen = int(address['prefixlen'])
                network = str(_sm_ipaddress.IPv4Network(source + '/' + str(prefixlen), strict=False))
                mac = _sm_mac(row.get('address') or by_name.get(interface, {}).get('address'))
            except (SwitchManagerError, ValueError, KeyError):
                continue
            info = by_name.get(interface, {}).get('linkinfo', {})
            vlan = info.get('info_data', {}).get('id') if info.get('info_kind') == 'vlan' else None
            result.append({'interface': interface, 'source': source, 'prefixlen': prefixlen,
                           'network': network, 'parent': parent, 'mac': mac, 'vlan': vlan,
                           'parent_mac': by_name[parent].get('address'),
                           'active': 'UP' in by_name.get(interface, {}).get('flags', [])})
    return result


def _sm_neighbor_devices(parent, excluded_ips):
    """Bounded existing cache inventory; these rows are observations, not scans."""
    links = _sm_ip_json('-d', 'link', 'show')
    candidates, seen = [], set(excluded_ips)
    for link in links:
        interface = link.get('ifname', '').split('@')[0]
        if _sm_link_parent(interface, links) != parent:
            continue
        for row in _sm_ip_json('-4', 'neigh', 'show', 'dev', interface):
            try:
                ip = _sm_ip(row.get('dst'))
                _sm_mac(row.get('lladdr'))
            except SwitchManagerError:
                continue
            if ip in seen:
                continue
            seen.add(ip)
            candidates.append({'id': 'neighbor-' + ip, 'name': '이웃 장치', 'ip': ip, 'kind': 'neighbor'})
            if len(candidates) >= 128:
                return _sm_camera_neighbors(candidates, parent)
    return _sm_camera_neighbors(candidates, parent) if candidates else []


def _sm_candidate_ips(payload):
    values = payload.get('candidate_ips', [])
    if not isinstance(values, list) or len(values) > 4:
        raise SwitchManagerError('찾을 카메라 IP는 최대 4개까지 개별 주소로 입력하세요.')
    return list(dict.fromkeys(_sm_ip(value) for value in values))


def _sm_discover_candidates(ips, parent):
    """ARP only named targets after validating their direct enp2 path twice."""
    cameras = [{'id': 'candidate-' + ip, 'name': '직접 찾은 장치', 'ip': ip, 'kind': 'candidate'} for ip in ips]
    observations = _sm_camera_neighbors(cameras, parent) if cameras else []
    for item in observations:
        # Cached entries must never masquerade as a successful fresh lookup.
        item.update(neighbors=[], probe_fresh=False, probe_checked=False, probe_error='')
        if item.get('route_error'):
            continue
        try:
            route = _sm_route({'target': item['ip'], 'source': item.get('source'), 'interface': item['interface']})
            if route['parent'] != parent:
                raise SwitchManagerError('선택한 허브와 같은 enp2 NIC의 경로가 아닙니다.')
            probe = globals().get('_cai_arp_probe')
            if not callable(probe):
                raise SwitchManagerError('ARP 검사 구성요소가 없습니다. 프로그램을 전체 업데이트하세요.')
            item['probe_checked'] = True
            reply = probe(item['ip'], route)
            if reply.get('replies', 0) < 2 or not reply.get('mac'):
                item['probe_error'] = '동일 MAC의 ARP 응답을 2회 이상 받지 못했습니다. 이전 캐시로 포트를 확정하지 않습니다.'
                continue
            item.update(probe_fresh=True, source=route['source'],
                        neighbors=[{'mac': _sm_mac(reply['mac']), 'state': ['ARP_REPLY']}])
        except ValueError as exc:
            # Shared ARP helper contains only interface/probe errors, not credentials.
            item['probe_error'] = str(exc) if type(exc).__name__ in ('SwitchManagerError', 'CameraActivationError') else 'ARP 검사를 완료하지 못했습니다.'
        except (OSError, _sm_subprocess.SubprocessError):
            item['probe_error'] = 'ARP 검사를 완료하지 못했습니다.'
    return observations


def _sm_camera_neighbors(cameras, parent):
    """Read routes and existing neighbor cache only: no ping, ARP or IP changes."""
    links = _sm_ip_json('-d', 'link', 'show')
    cache, result = {}, []
    for camera in cameras:
        item = dict(camera, interface=None, source=None, neighbors=[], route_error='')
        if not camera['ip']:
            item['route_error'] = '등록된 카메라 주소가 IPv4 주소가 아닙니다.'
            result.append(item)
            continue
        try:
            routes = _sm_ip_json('-4', 'route', 'get', camera['ip'])
            if len(routes) != 1:
                raise SwitchManagerError('카메라 경로가 단일 경로가 아닙니다.')
            route, interface = routes[0], routes[0].get('dev')
            item['interface'] = interface
            item['source'] = route.get('prefsrc') or route.get('src')
            if route.get('gateway') or route.get('type') in ('local', 'broadcast', 'unreachable', 'blackhole', 'prohibit') or _sm_link_parent(interface, links) != parent:
                raise SwitchManagerError('이 허브와 같은 enp2 NIC의 직접 경로가 아닙니다. 다른 NIC·LTE·게이트웨이 경로는 대조하지 않습니다.')
            if interface not in cache:
                cache[interface] = _sm_ip_json('-4', 'neigh', 'show', 'dev', interface)
            for row in cache[interface]:
                if row.get('dst') != camera['ip']:
                    continue
                state = row.get('state', [])
                states = state if isinstance(state, list) else str(state).replace(',', ' ').split()
                try:
                    mac = _sm_mac(row.get('lladdr', ''))
                except SwitchManagerError:
                    mac = None
                item['neighbors'].append({'mac': mac, 'state': sorted(set(str(value).upper() for value in states))})
        except SwitchManagerError as exc:
            item['route_error'] = str(exc)
        result.append(item)
    return result


def _sm_camera_matches(observations, fdb, ports, ed_ports):
    """Correlate cache observations; ambiguous results never acquire a port."""
    by_port = {row['port']: row for row in ports}
    matches, ip_counts = [], {}
    for camera in observations:
        if camera['ip']:
            ip_counts[camera['ip']] = ip_counts.get(camera['ip'], 0) + 1
    for camera in observations:
        match = {key: camera[key] for key in ('id', 'name', 'ip', 'interface')}
        match.update(source=camera.get('source'), kind=camera.get('kind', 'camera'),
                     probe_fresh=bool(camera.get('probe_fresh')), probe_checked=bool(camera.get('probe_checked')))
        match.update(mac=None, mac_candidates=[], neighbor_state=[], ports=[], direct_port=None,
                     status='unresolved', reason='', fdb_entries=[])
        neighbors = camera.get('neighbors', [])
        match['neighbor_state'] = sorted({state for row in neighbors for state in row['state']})
        known_states = {'REACHABLE', 'STALE', 'DELAY', 'PROBE', 'PERMANENT', 'NOARP', 'ARP_REPLY'}
        valid = [row for row in neighbors if row['mac'] and row['state'] and set(row['state']).issubset(known_states)]
        macs = sorted({row['mac'] for row in valid})
        match['mac_candidates'] = macs
        if len(macs) == 1:
            match['mac'] = macs[0]
            match['fdb_entries'] = [row for row in fdb if row['mac'] == macs[0]]
            match['ports'] = sorted({row['port'] for row in match['fdb_entries']})
        if camera.get('route_error'):
            match.update(status='off_network', reason=camera['route_error'])
        elif camera.get('probe_error'):
            match.update(status='probe_failed', reason=camera['probe_error'])
        elif ip_counts.get(camera['ip'], 0) > 1:
            match.update(status='ambiguous', reason='같은 IP가 여러 카메라 항목에 등록되어 있습니다. 어느 실제 카메라인지 구분할 수 없습니다.')
        elif len(macs) > 1:
            match.update(status='ambiguous', reason='같은 IP에서 여러 MAC 관측값이 있습니다. IP 중복 또는 변경 이력을 확인해야 합니다.')
        elif not macs:
            match['reason'] = '유효한 ARP/이웃 캐시가 없습니다. 자동 통신 검사나 포트 조작은 하지 않았습니다.'
        elif len(match['ports']) > 1:
            match.update(status='ambiguous', reason='같은 MAC이 여러 허브 포트에서 보입니다. 포트를 확정할 수 없습니다.')
        elif not match['ports']:
            match['reason'] = 'ARP 캐시의 MAC이 현재 허브 MAC 학습표에 없습니다.'
        else:
            port = by_port.get(match['ports'][0])
            if not port:
                match['reason'] = 'MAC 학습 포트의 현재 상태를 확인하지 못했습니다.'
                matches.append(match)
                continue
            if port['port'] >= 9 or port['mode'] != 'access' or port['port'] in ed_ports:
                match.update(status='uplink', reason='광·업링크·ED 경로에서 학습된 MAC입니다. 이 포트를 카메라 직결 포트로 판단하지 않습니다.')
            elif len(port.get('macs', [])) != 1:
                match.update(status='ambiguous', reason='해당 포트에서 여러 MAC이 학습되었습니다. 하위 허브 또는 공유 연결일 수 있습니다.')
            elif port['link'] != 'up' or port['admin_up'] is not True:
                match['reason'] = '학습 기록은 있지만 현재 포트 링크 또는 사용 상태가 켜짐으로 확인되지 않습니다.'
            elif len({row['vlan'] for row in match['fdb_entries']}) != 1 or match['fdb_entries'][0]['vlan'] != port['pvid']:
                match.update(status='ambiguous', reason='MAC 학습 VLAN과 포트 VLAN이 단일 값으로 일치하지 않습니다.')
            else:
                recent = camera.get('probe_fresh') or (bool(valid) and all(row['state'] == ['REACHABLE'] for row in valid))
                match.update(direct_port=port['port'], status='matched' if recent else 'cached',
                             reason=('지정한 enp2 경로에서 새 ARP 응답을 받고 같은 MAC이 단일 Access 포트에서 학습되었습니다.' if camera.get('probe_fresh') else
                                     'ARP 캐시가 REACHABLE이고 같은 MAC이 단일 Access 포트에서 학습되었습니다.' if recent else
                                     '이전 ARP/정적 캐시와 현재 MAC 학습표가 일치합니다. 현재 카메라 응답을 확인한 결과는 아닙니다.') +
                                    ' 학습 포트이며, 물리적 직결·IP 중복 부재를 보장하지 않습니다.')
        matches.append(match)
    # One MAC shared by different configured IPs may be an alias or proxy. It is
    # not sufficient evidence to assign either camera to a physical port.
    mac_ips = {}
    for match in matches:
        if match['mac']:
            mac_ips.setdefault(match['mac'], set()).add(match['ip'])
    for match in matches:
        if match['mac'] and len(mac_ips[match['mac']]) > 1:
            match.update(status='ambiguous', direct_port=None,
                         reason='서로 다른 카메라 IP가 같은 MAC으로 관측됩니다. 주소 별칭·중복 등록·프록시 여부를 확인해야 합니다.')
    return matches


def _sm_connected_devices(snapshot, matches, local_interfaces):
    """Attach learned devices to ports without calling every MAC a camera."""
    for port in snapshot['ports']:
        devices = []
        for mac in port.get('macs', []):
            if mac == snapshot['route']['local_mac']:
                addresses = sorted({row['source'] for row in local_interfaces
                                    if row['parent'] == snapshot['route']['parent'] and row['mac'] == mac})
                devices.append({'kind': 'ed', 'name': '이 ED / PC', 'ip': snapshot['route']['source'],
                                'ips': addresses or [snapshot['route']['source']], 'mac': mac,
                                'interface': snapshot['route']['dev'], 'source': snapshot['route']['source'],
                                'status': 'local', 'learned_port': port['port'], 'direct_port': None,
                                'reason': '이 PC의 NIC MAC이 학습된 포트입니다. 관리 연결 보호 대상입니다.'})
                continue
            related = [match for match in matches if match['mac'] == mac and port['port'] in match['ports']]
            for match in related:
                devices.append(dict(match, learned_port=port['port'], ips=[match['ip']]))
            if not related:
                devices.append({'kind': 'unknown', 'name': 'IP 미확인 장치', 'ip': '', 'ips': [], 'mac': mac,
                                'interface': None, 'source': None, 'status': 'learned',
                                'learned_port': port['port'], 'direct_port': None,
                                'reason': 'MAC만 학습되었습니다. IP나 카메라 여부는 확인되지 않았으며 하위 장비일 수 있습니다.'})
        port['connected_devices'] = devices


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
    ED management-network setup is a separate, explicit operation on the VLAN
    child of a camera NIC. Switch reads and writes never configure the host.
    """
    def __init__(self, config_path, cameras_provider=None):
        self.config_path = _sm_Path(config_path)
        self._plans = {}
        self._closing = False
        self._state_lock, self._operation_lock = _sm_threading.RLock(), _sm_threading.RLock()
        self._client_factory = _SwitchCLI  # Dependency seam for offline tests.
        self._route = _sm_route
        self._collision = _sm_no_collision
        self._sleep = _sm_time.sleep
        self._cameras_provider = cameras_provider
        self._camera_neighbors = _sm_camera_neighbors
        self._neighbor_devices = _sm_neighbor_devices
        self._discover_candidates = _sm_discover_candidates
        self._local_interfaces = _sm_local_interfaces

    def _map_cameras(self, snapshot, raw, candidate_observations=None):
        snapshot['camera_matches'] = []
        snapshot['candidate_matches'] = []
        snapshot['device_matches'] = []
        snapshot['camera_mapping_warnings'] = []
        snapshot['discovery_warnings'] = []
        snapshot['camera_mapping_note'] = ('등록 카메라·기존 이웃 캐시·허브 MAC 학습표를 대조합니다. '
                                           '별도로 찾기를 요청한 IP만 enp2에서 새 ARP 검사를 합니다. '
                                           '학습 포트는 물리적 직결이나 IP 중복 부재를 보장하지 않습니다. '
                                           '카메라 포트 등록·제어 대상은 자동 변경하지 않습니다.')
        candidate_observations = candidate_observations or []
        cameras, observations = [], []
        try:
            registered = self._cameras_provider() if self._cameras_provider else []
            if not isinstance(registered, (list, tuple)) or len(registered) > 256:
                raise SwitchManagerError('카메라 목록은 최대 256개의 IP 항목이어야 합니다.')
            for index, camera in enumerate(registered):
                if not isinstance(camera, dict):
                    raise SwitchManagerError('카메라 목록 형식을 확인하지 못했습니다.')
                try:
                    ip = _sm_ip(camera.get('ip'))
                except SwitchManagerError:
                    ip = ''
                cameras.append({'id': str(camera.get('id', index))[:200],
                                'name': str(camera.get('name') or camera.get('id') or index)[:200], 'ip': ip})
            observations = self._camera_neighbors(cameras, snapshot['route']['parent']) if cameras else []
        except Exception:
            # Mapping is supplementary evidence. A camera settings error must not
            # conceal switch state or leak RTSP credential-bearing exceptions.
            snapshot['camera_mapping_warnings'].append('카메라 목록 또는 이웃 캐시를 읽지 못해 카메라-포트 대조를 완료하지 못했습니다.')
        candidate_ips = {row['ip'] for row in candidate_observations}
        # A registered address explicitly probed remains one observation. Do not
        # manufacture duplicate-IP ambiguity by adding it a second time.
        for candidate in candidate_observations:
            registered = [row for row in observations if row['ip'] == candidate['ip']]
            if registered:
                for row in registered:
                    row.update({key: value for key, value in candidate.items() if key not in ('id', 'name', 'kind')})
            else:
                observations.append(candidate)
            if candidate.get('probe_error') or candidate.get('route_error'):
                snapshot['discovery_warnings'].append(candidate['ip'] + ': ' + (candidate.get('probe_error') or candidate['route_error']))
        try:
            observations.extend(self._neighbor_devices(snapshot['route']['parent'], {row['ip'] for row in observations}))
        except Exception:
            snapshot['camera_mapping_warnings'].append('추가 이웃 장치 목록을 읽지 못했습니다.')
        fdb = _sm_fdb_entries(raw['show mac-address-table'])
        matches = _sm_camera_matches(observations, fdb, snapshot['ports'], snapshot['ed_ports'])
        snapshot['device_matches'] = matches
        snapshot['camera_matches'] = [row for row in matches if row['kind'] == 'camera']
        snapshot['candidate_matches'] = [row for row in matches if row['ip'] in candidate_ips]
        for match in matches:
            if match['direct_port'] is not None and match['kind'] == 'camera':
                port = next(row for row in snapshot['ports'] if row['port'] == match['direct_port'])
                port['cameras'].append({key: match[key] for key in ('id', 'name', 'ip', 'status', 'reason')})
        try:
            interfaces = self._local_interfaces()
        except Exception:
            interfaces = []
        _sm_connected_devices(snapshot, matches, interfaces)

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

    def get_config(self, include_network=False):
        with self._state_lock:
            result = self._load()
        if include_network:
            result.update(local_interfaces=[], connection_health=[], network_warnings=[])
            try:
                result['local_interfaces'] = self._local_interfaces()
            except SwitchManagerError:
                result['network_warnings'].append('현재 enp2 주소를 읽지 못했습니다.')
            for connection in result['switches']:
                health = {'switch_id': connection['id'], 'route_ready': False, 'route_error': ''}
                try:
                    health.update(route_ready=True, route=self._route(connection))
                except SwitchManagerError as exc:
                    health['route_error'] = str(exc)
                result['connection_health'].append(health)
        return result

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

    def setup_network(self, payload):
        """Configure this ED's NetworkManager profile, then bind the saved row to it."""
        self._begin_operation('다른 허브 작업이 진행 중입니다. 완료 후 다시 시도하세요.')
        try:
            saved = self._connection(payload)
            parent = payload.get('parent')
            if not isinstance(parent, str) or not _sm_re.fullmatch(r'enp2[0-9A-Za-z]*', parent):
                raise SwitchManagerError('카메라용 enp2 물리 포트를 선택하세요.')
            interfaces = [n for n in self._local_interfaces() if n['parent'] == parent and n.get('parent_mac')]
            if not interfaces:
                raise SwitchManagerError('선택한 카메라 NIC가 이 ED에 있는지 확인하세요.')
            mac = _sm_mac(interfaces[0]['parent_mac'])
            vlan = _sm_int(payload.get('vlan'), 1, 4094, '관리 VLAN')
            source = _sm_ip(payload.get('source'))
            if source == saved['target']:
                raise SwitchManagerError('ED 관리 접속 IP와 허브 IP는 서로 달라야 합니다.')
            # Run the bundled helper as this user. NetworkManager owns system
            # authorization. Never run an elevated Python/web server or pass a password.
            command = [_sm_sys.executable, '-I', '-c', embedded_text(EMBEDDED_SWITCH_NETWORK),
                       '--apply', '--parent', parent, '--parent-mac', mac, '--vlan', str(vlan),
                       '--source', source, '--target', saved['target']]
            proc = _sm_subprocess.run(command, stdin=_sm_subprocess.DEVNULL, capture_output=True, text=True)
            try:
                report = _sm_json.loads(proc.stdout)
                if not isinstance(report, dict):
                    raise ValueError()
            except ValueError:
                raise SwitchManagerError('ED 관리 경로 설정 도구에서 결과를 받지 못했습니다. 접속 상태를 다시 확인하세요.') from None
            if proc.returncode or report.get('status') != 'ready':
                raise SwitchManagerError(str(report.get('message') or 'ED 관리 경로를 구성하지 못했습니다.')[:600])
            if report.get('target') != saved['target'] or report.get('source') != source:
                raise SwitchManagerError('관리 경로 확인 결과와 요청한 주소가 일치하지 않습니다.')
            connection = self._validate_connection(dict(saved, source=source, interface=report.get('interface')))
            if self._route(connection)['parent'] != parent:
                raise SwitchManagerError('준비된 관리 경로의 물리 포트를 확인하지 못했습니다.')
            if connection != saved:
                self._save_connection(connection)
            return {'connection': connection, 'management_mac': report.get('management_mac'), 'status': 'ready'}
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

    def _snapshot(self, client, connection, route, candidate_observations=None):
        identity = _sm_identity(client.command('show version'))
        supported = self._check_identity(identity, connection)
        result = {'switch_id': connection['id'], 'connection': dict(connection), 'identity': identity,
                  'route': route, 'supported': supported, 'ports': [], 'management': None,
                  'vlans': [], 'ed_ports': [], 'can_modify': False, 'warnings': [],
                  'camera_matches': [], 'candidate_matches': [], 'device_matches': [],
                  'camera_mapping_warnings': [], 'discovery_warnings': []}
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
                                    'editable': not reasons, 'mac_count': len(macs), 'macs': sorted(macs),
                                    'cameras': [], 'connected_devices': []})
        if not connection['expected_cpu_mac']:
            result['warnings'].append('CPU MAC을 저장하기 전에는 설정을 변경할 수 없습니다.')
        if not ed_known:
            result['warnings'].append('ED 포트가 명확하지 않아 모든 설정 변경을 차단했습니다.')
        if _sm_comparable(raw['show running-config']) != _sm_comparable(raw['show startup-config']):
            result['warnings'].append('실행 설정과 저장 설정이 다릅니다. 기존 미저장 변경을 먼저 확인해야 합니다.')
        if 'not configured' not in raw['show reload'].lower():
            result['warnings'].append('기존 재부팅 예약 상태를 확인해야 합니다. 이 도구는 재부팅을 예약하거나 취소하지 않습니다.')
        result['can_modify'] = bool(connection['expected_cpu_mac'] and ed_known and not result['warnings'])
        self._map_cameras(result, raw, candidate_observations)
        return result, raw

    def inspect(self, payload):
        connection, password = self._connection(payload), self._password(payload)
        candidate_ips = _sm_candidate_ips(payload)
        self._begin_operation('다른 허브 작업이 진행 중입니다. 완료 후 다시 조회하세요.')
        try:
            route = self._route(connection)
            with self._client_factory(connection, password) as client:
                candidates = self._discover_candidates(candidate_ips, route['parent']) if candidate_ips else []
                return self._snapshot(client, connection, route, candidates)[0]
        finally:
            password = None
            self._operation_lock.release()

    @_sm_contextmanager
    def observing_port(self, payload):
        """Keep local switch operations serialized during camera identification or handover."""
        self._begin_operation('다른 허브 작업이 진행 중입니다. 완료 후 포트를 다시 확인하세요.')
        try:
            yield self.inspect(payload)
        finally:
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

# BEGIN EMBEDDED CAMERA_ACTIVATION
# Embedded before camera_admin.py. Importing this component performs no I/O.
# Protocol evidence: the device's public ChangePasswordCtrl, 2026-11-17.
import base64 as _cai_base64
import hashlib as _cai_hashlib
import http.client as _cai_httpclient
import ipaddress as _cai_ipaddress
import json as _cai_json
import os as _cai_os
from pathlib import Path as _cai_Path
import re as _cai_re
import secrets as _cai_secrets
import shutil as _cai_shutil
import ssl as _cai_ssl
import subprocess as _cai_subprocess
import tempfile as _cai_tempfile
import threading as _cai_threading
import time as _cai_time
import urllib.parse as _cai_urlparse


class CameraActivationError(ValueError):
    """Safe user-facing failure; never includes credentials or HTTP response bodies."""


_CAI_STATUS = '/init-cgi/pw_init.cgi?msubmenu=statuscheck&action=view'
_CAI_ACTIVATE = '/init-cgi/pw_init.cgi?msubmenu=setinitpassword&action=set&IsPasswordEncrypted=True'
_CAI_MAC_RE = r'(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}'
_CAI_ARP_PRIVILEGE = ('카메라 IP 중복 검사를 실행하지 못했습니다. arping 설치와 원시 패킷 권한을 '
                      '확인하세요. 이 프로그램은 sudo나 PC 네트워크 설정을 변경하지 않습니다.')


def _cai_ip(value):
    if not isinstance(value, str):
        raise CameraActivationError('카메라의 IPv4 주소를 입력하세요.')
    try:
        address = _cai_ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise CameraActivationError('카메라의 IPv4 주소를 입력하세요.') from None
    if address.is_unspecified or address.is_loopback or address.is_multicast or int(address) == 0xffffffff:
        raise CameraActivationError('카메라의 실제 IPv4 주소를 입력하세요.')
    return str(address)


def _cai_mac(value):
    if not isinstance(value, str) or not _cai_re.fullmatch(_CAI_MAC_RE, value):
        raise CameraActivationError('카메라의 MAC 주소를 확인하지 못했습니다.')
    value = value.replace('-', ':').lower()
    if value == '00:00:00:00:00:00' or int(value[:2], 16) & 1:
        raise CameraActivationError('카메라의 유니캐스트 MAC 주소가 필요합니다.')
    return value


def _cai_ip_json(*args):
    try:
        result = _cai_subprocess.run(['ip', '-j'] + list(args), capture_output=True,
                                     text=True, timeout=5, check=True)
        value = _cai_json.loads(result.stdout)
        if not isinstance(value, list):
            raise ValueError()
        return value
    except (OSError, ValueError, _cai_subprocess.SubprocessError):
        raise CameraActivationError('카메라 NIC의 직접 연결 경로를 확인하지 못했습니다.') from None


def _cai_route(ip, source=None):
    """Require a directly attached, assigned enp2 source; never use LTE/default routes."""
    ip = _cai_ip(ip)
    args = ['-4', 'route', 'get', ip]
    if source:
        source = _cai_ip(source)
        args += ['from', source]
    routes = _cai_ip_json(*args)
    if len(routes) != 1:
        raise CameraActivationError('카메라로 가는 경로가 명확하지 않습니다.')
    route = routes[0]
    interface = route.get('dev')
    if (not isinstance(interface, str) or route.get('gateway') or route.get('multipath') or
            route.get('nhid') or route.get('type') in ('local', 'broadcast', 'unreachable', 'blackhole', 'prohibit')):
        raise CameraActivationError('직접 연결된 enp2 카메라망에서만 설정할 수 있습니다. LTE는 사용하지 않습니다.')
    source = source or route.get('prefsrc') or route.get('src')
    source = _cai_ip(source)
    links = _cai_ip_json('-d', 'link', 'show')
    by_name = {r.get('ifname', '').split('@')[0]: r for r in links}
    by_index = {r.get('ifindex'): r for r in links}
    current = original = by_name.get(interface)
    visited = set()
    while current and not _cai_re.fullmatch(r'enp2[0-9A-Za-z]*', current.get('ifname', '').split('@')[0]):
        if current.get('ifindex') in visited or current.get('linkinfo', {}).get('info_kind') != 'vlan':
            current = None
            break
        visited.add(current.get('ifindex'))
        parent = current.get('link')
        current = by_name.get(parent) if isinstance(parent, str) else by_index.get(current.get('link_index', parent))
    if current is None or original is None or 'UP' not in original.get('flags', []):
        raise CameraActivationError('활성 enp2 또는 그 VLAN 인터페이스에서만 카메라 설정을 허용합니다.')
    all_addresses = _cai_ip_json('-4', 'address', 'show')
    assigned = [(r.get('ifname', '').split('@')[0], a) for r in all_addresses for a in r.get('addr_info', [])
                if a.get('family') == 'inet']
    own = [a for dev, a in assigned if dev == interface and a.get('local') == source]
    if len(own) != 1 or sum(a.get('local') == source for _, a in assigned) != 1 or any(a.get('local') == ip for _, a in assigned):
        raise CameraActivationError('소스 IP가 카메라 NIC에 유일하게 할당되어 있어야 하며 대상은 ED 자신의 주소일 수 없습니다.')
    # Host-specific /32 routes are allowed; target still must be unicast and on-link.
    if own[0].get('prefixlen', 32) < 31:
        subnet = _cai_ipaddress.ip_network(source + '/' + str(own[0]['prefixlen']), strict=False)
        if _cai_ipaddress.IPv4Address(ip) in (subnet.network_address, subnet.broadcast_address):
            raise CameraActivationError('카메라 주소로 네트워크/브로드캐스트 주소를 사용할 수 없습니다.')
    return {'dev': interface, 'source': source, 'parent': current['ifname'].split('@')[0],
            'local_mac': _cai_mac(original.get('address'))}


def _cai_neighbor(ip, route):
    rows = _cai_ip_json('neigh', 'show', 'to', ip, 'dev', route['dev'])
    valid = [r for r in rows if r.get('dst') == ip and r.get('lladdr') and
             not set(r.get('state', [])) & {'FAILED', 'INCOMPLETE'}]
    if len(valid) != 1:
        raise CameraActivationError('카메라의 ARP/MAC 응답을 확인하지 못했습니다.')
    return _cai_mac(valid[0]['lladdr'])


def _camera_admin_password(password, confirm_password, username='admin', maximum=64, *, tta=False):
    """Conservative documented Wisenet policy; validation only, no side effects."""
    if not isinstance(password, str) or not isinstance(confirm_password, str) or password != confirm_password:
        raise CameraActivationError('새 비밀번호와 확인 입력이 일치해야 합니다.')
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 8 <= maximum <= 64:
        raise CameraActivationError('카메라의 비밀번호 길이 정책을 확인하지 못했습니다.')
    minimum = 9 if tta else 8
    if not minimum <= len(password) <= maximum or not all(33 <= ord(c) <= 126 for c in password):
        raise CameraActivationError('비밀번호는 공백 없이 영문·숫자·특수문자로 {}~{}자를 입력하세요.'.format(minimum, maximum))
    categories = sum(bool(_cai_re.search(p, password)) for p in (r'[A-Z]', r'[a-z]', r'[0-9]', r'[^A-Za-z0-9]'))
    if categories < (3 if tta or len(password) < 10 else 2):
        raise CameraActivationError('비밀번호에 영문 대문자·소문자·숫자·특수문자를 더 조합하세요.')
    if password.lower() == str(username).lower() or _cai_re.search(r'(.)\1{3}', password):
        raise CameraActivationError('사용자명과 같은 비밀번호나 동일한 문자 4개 반복은 사용할 수 없습니다.')
    lower = password.lower()
    for sequence in ('0123456789', 'abcdefghijklmnopqrstuvwxyz'):
        for order in (sequence, sequence[::-1]):
            if any(order[i:i + 4] in lower for i in range(len(order) - 3)):
                raise CameraActivationError('숫자나 알파벳 4개가 순서대로 이어지는 비밀번호는 사용할 수 없습니다.')


def _camera_admin_encrypt(public_key, password):
    """JSEncrypt-compatible RSA PKCS#1 v1.5; plaintext enters OpenSSL on stdin only."""
    if (not isinstance(public_key, str) or not 100 <= len(public_key) <= 16384 or
            not _cai_re.fullmatch(r'\s*-----BEGIN (?:RSA )?PUBLIC KEY-----[\sA-Za-z0-9+/=]+-----END (?:RSA )?PUBLIC KEY-----\s*', public_key)):
        raise CameraActivationError('카메라가 정상 RSA 공개키를 제공하지 않았습니다.')
    if not isinstance(password, str) or not 1 <= len(password.encode('utf-8')) <= 1024:
        raise CameraActivationError('암호화할 비밀번호 형식이 올바르지 않습니다.')
    openssl = _cai_shutil.which('openssl')
    if not openssl:
        raise CameraActivationError('비밀번호 암호화에 OpenSSL이 필요합니다. Ubuntu: sudo apt install openssl')
    try:
        with _cai_tempfile.TemporaryDirectory(prefix='camera-public-key-') as directory:
            path = _cai_Path(directory) / 'public.pem'
            fd = _cai_os.open(path, _cai_os.O_WRONLY | _cai_os.O_CREAT | _cai_os.O_EXCL, 0o600)
            with _cai_os.fdopen(fd, 'w', encoding='ascii') as out:
                out.write(public_key)
            check = _cai_subprocess.run([openssl, 'pkey', '-pubin', '-in', str(path), '-text', '-noout'],
                stdin=_cai_subprocess.DEVNULL, capture_output=True, timeout=5)
            match = _cai_re.search(rb'Public-Key:\s*\((\d+) bit\)', check.stdout)
            if check.returncode or not match or not 2048 <= int(match.group(1)) <= 8192:
                raise CameraActivationError('2048~8192비트 RSA 공개키를 확인하지 못했습니다.')
            size = (int(match.group(1)) + 7) // 8
            plaintext = password.encode('utf-8')
            if len(plaintext) > size - 11:
                raise CameraActivationError('카메라 공개키로 암호화할 수 있는 비밀번호 길이를 초과했습니다.')
            encrypted = _cai_subprocess.run([openssl, 'pkeyutl', '-encrypt', '-pubin', '-inkey', str(path),
                '-pkeyopt', 'rsa_padding_mode:pkcs1'], input=plaintext, capture_output=True, timeout=5)
            if encrypted.returncode or len(encrypted.stdout) != size:
                raise CameraActivationError('카메라 비밀번호 암호화에 실패했습니다.')
            return _cai_base64.b64encode(encrypted.stdout).decode('ascii')
    except (OSError, UnicodeError, _cai_subprocess.SubprocessError):
        raise CameraActivationError('카메라 비밀번호 암호화를 완료하지 못했습니다.') from None


def _cai_arp_probe(ip, route, *, dad=False):
    """Read-only broadcast ARP requests, both supported arping implementations."""
    ip = _cai_ip(ip)
    source = '0.0.0.0' if dad else _cai_ip(route.get('source'))
    interface = route.get('dev', '')
    if not _cai_re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise CameraActivationError('ARP 검사용 NIC가 올바르지 않습니다.')
    binary = _cai_shutil.which('arping') or _cai_shutil.which('arping', path='/usr/sbin:/sbin')
    if not binary:
        raise CameraActivationError('IP 중복 검사에 arping이 필요합니다. Ubuntu: sudo apt install arping')
    try:
        env = dict(_cai_os.environ, LC_ALL='C', LANG='C')
        help_result = _cai_subprocess.run([binary, '--help'], capture_output=True, text=True, timeout=3, env=env)
        help_text = help_result.stdout + help_result.stderr
        if 'Thomas Habets' in help_text:
            flavor = 'habets'
            args = ['-d', '-t', 'ff:ff:ff:ff:ff:ff', '-i', interface, '-S', source, '-c', '3', '-w', '5', ip]
        elif ('iputils' in help_text.lower() or
              (_cai_re.search(r'-I\s+<?interface', help_text) and _cai_re.search(r'-b\s+.*broadcast', help_text))):
            flavor = 'iputils'
            args = ['-b', '-I', interface, '-s', source, '-c', '3', '-w', '5', ip]
            if dad:
                args.insert(0, '-D')  # iputils DAD mode explicitly permits source 0.0.0.0.
        else:
            raise CameraActivationError('지원하지 않는 arping 형식입니다. Thomas Habets 또는 iputils arping이 필요합니다.')
        result = _cai_subprocess.run([binary] + args, capture_output=True, text=True, timeout=8, env=env)
    except (OSError, _cai_subprocess.SubprocessError):
        raise CameraActivationError(_CAI_ARP_PRIVILEGE) from None
    output = result.stdout
    if result.stderr.strip() or result.returncode not in (0, 1) or len(output) > 65536:
        raise CameraActivationError(_CAI_ARP_PRIVILEGE)
    if flavor == 'habets':
        summary = _cai_re.search(r'(\d+) packets transmitted,\s*(\d+) packets received', output)
        replies = [_cai_mac(m) for m in _cai_re.findall(r'\bfrom\s+(' + _CAI_MAC_RE + r')\s+\(' + _cai_re.escape(ip) + r'\)', output)]
        sent, received = (int(summary.group(1)), int(summary.group(2))) if summary else (-1, -1)
    else:
        sent_match = _cai_re.search(r'Sent\s+(\d+)\s+probes?', output)
        received_match = _cai_re.search(r'Received\s+(\d+)\s+responses?', output)
        sent = int(sent_match.group(1)) if sent_match else -1
        received = int(received_match.group(1)) if received_match else -1
        replies = [_cai_mac(m) for m in _cai_re.findall(r'\breply from\s+' + _cai_re.escape(ip) + r'\s+\[(' + _CAI_MAC_RE + r')\]', output, _cai_re.I)]
    minimum_sent = 1 if dad and received > 0 else 3
    if sent < minimum_sent or received < 0 or received != len(replies):
        raise CameraActivationError(_CAI_ARP_PRIVILEGE)
    if len(set(replies)) > 1:
        raise CameraActivationError('같은 IP에서 서로 다른 MAC 응답이 있습니다. 중복 IP 장치를 분리한 뒤 다시 시도하세요.')
    # Habets -d returns 0 for no duplicate (including zero replies); iputils returns 1 for zero.
    expected_code = 0 if flavor == 'habets' or received else 1
    if flavor == 'iputils' and dad:
        expected_code = 1 if received else 0
    if result.returncode != expected_code:
        raise CameraActivationError(_CAI_ARP_PRIVILEGE)
    return {'mac': replies[0] if replies else None, 'replies': received, 'probe_flavor': flavor}


def _camera_admin_duplicate_probe(ip, route):
    result = _cai_arp_probe(ip, route)
    if result['replies'] < 2:
        raise CameraActivationError('카메라에서 동일 MAC의 ARP 응답을 2회 이상 받지 못했습니다. 변경하지 않습니다.')
    expected = route.get('expected_mac') or _cai_neighbor(ip, route)
    if result['mac'] != _cai_mac(expected):
        raise CameraActivationError('카메라의 MAC이 변경되었습니다. 중복 IP 또는 배선을 확인하세요.')
    return result


def _camera_admin_address_available(ip, route):
    result = _cai_arp_probe(ip, route)
    if result['replies']:
        raise CameraActivationError('새 IP에서 ARP 응답을 받았습니다. 사용하지 않는 주소를 지정하세요.')
    return None


def _camera_admin_handover_available(ip, old_route):
    """Probe the proposed address on the old camera's Ethernet link, without IP routing."""
    for dev in _cai_ip_json('-4', 'address', 'show'):
        for address in dev.get('addr_info', []):
            if ip in (address.get('local'), address.get('broadcast')):
                raise CameraActivationError('새 IP가 이 ED의 주소 또는 브로드캐스트 주소입니다.')
    if _cai_arp_probe(ip, old_route, dad=True)['replies']:
        raise CameraActivationError('현재 카메라 LAN에서 새 IP에 ARP 응답이 있습니다. 다른 주소를 지정하세요.')


def _cai_request(ip, route, method, path, body=None, expected_cert=None):
    if (method, path) not in (('GET', _CAI_STATUS), ('POST', _CAI_ACTIVATE)):
        raise CameraActivationError('허용하지 않은 초기화 요청입니다.')
    if method == 'POST' and (not expected_cert or not isinstance(body, bytes)):
        raise CameraActivationError('인증서 확인 및 암호화된 초기 비밀번호가 필요합니다.')
    context = _cai_ssl.SSLContext(_cai_ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = _cai_ssl.CERT_NONE
    connection = _cai_httpclient.HTTPSConnection(ip, 443, timeout=6, context=context,
                                                source_address=(route['source'], 0))
    try:
        if _cai_route(ip, route['source']) != route:
            raise CameraActivationError('카메라 접속 경로가 바뀌었습니다. 다시 점검하세요.')
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True)
        if not certificate:
            raise CameraActivationError('카메라 HTTPS 인증서를 확인하지 못했습니다.')
        fingerprint = _cai_hashlib.sha256(certificate).hexdigest()
        if expected_cert and not _cai_secrets.compare_digest(fingerprint, expected_cert):
            raise CameraActivationError('카메라 HTTPS 인증서가 변경되었습니다. 초기 비밀번호를 전송하지 않습니다.')
        headers = {'Accept': 'application/json', 'Cache-Control': 'no-cache'}
        if method == 'POST':
            headers['Content-Type'] = 'text/plain;charset=UTF-8'
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read(65537)
        if 300 <= response.status < 400:
            raise CameraActivationError('다른 주소로 이동하는 카메라 응답은 따르지 않습니다.')
        if response.status != 200 or len(payload) > 65536:
            raise CameraActivationError('카메라 초기 설정 API가 응답하지 않거나 지원되지 않습니다.')
        if method == 'GET':
            try:
                data = _cai_json.loads(payload.decode('utf-8'))
            except (ValueError, UnicodeError):
                raise CameraActivationError('카메라의 초기 비밀번호 상태를 해석하지 못했습니다.') from None
            if not isinstance(data, dict):
                raise CameraActivationError('카메라의 초기 비밀번호 응답이 올바르지 않습니다.')
        else:
            data = {}  # A 200 response alone is NOT success; status must be re-read.
        return data, fingerprint
    except CameraActivationError:
        raise
    except (OSError, _cai_httpclient.HTTPException, ValueError):
        raise CameraActivationError('카메라 HTTPS 연결을 완료하지 못했습니다.') from None
    finally:
        try:
            connection.close()
        except OSError:
            # A peer/socket close race must not replace a verified response or
            # the safe error explaining that a password request was uncertain.
            pass


def _cai_policy(status):
    if not isinstance(status.get('SpecialType', ''), str) or type(status.get('NewPasswordPolicy')) is not bool:
        raise CameraActivationError('카메라가 초기 비밀번호 정책을 제공하지 않았습니다.')
    tta = 'tta' in status.get('SpecialType', '').lower()
    maximum = status.get('MaxPasswordLength') if status['NewPasswordPolicy'] and not tta else 15
    if type(maximum) is not int or not 8 <= maximum <= 64:
        raise CameraActivationError('카메라의 최대 비밀번호 길이가 지원 범위를 벗어났습니다.')
    return {'minimum': 9 if tta else 8, 'maximum': maximum, 'tta': tta,
            'description': '{}~{}자, 공백 없는 영문·숫자·특수문자 조합'.format(9 if tta else 8, maximum)}


class CameraActivationManager:
    """Explicit initial-admin-password registration only; no reset or account guessing.

    Inspect is read-only. Preview requires the user to isolate a single camera and
    pins observed MAC and TLS identity for five minutes. Only apply sends one POST.
    No stored config, password, camera IP, NIC or switch setting is changed here.
    """
    TOKEN_SECONDS = 300

    def __init__(self, config_path):
        self._plans = {}
        self._lock = _cai_threading.Lock()
        self._closed = False

    def discard_plan(self, token):
        with self._lock:
            self._plans.pop(token, None)

    def close(self):
        with self._lock:
            self._closed = True
            self._plans.clear()

    def _inspect(self, payload, expected_cert=None):
        if self._closed:
            raise CameraActivationError('카메라 초기 설정 서비스가 종료되었습니다.')
        if not isinstance(payload, dict):
            raise CameraActivationError('초기 설정 입력 형식이 올바르지 않습니다.')
        ip = _cai_ip(payload.get('ip'))
        expected_mac = _cai_mac(payload['expected_mac']) if 'expected_mac' in payload else None
        if expected_cert is None and 'expected_cert_sha256' in payload:
            expected_cert = payload['expected_cert_sha256']
            if not isinstance(expected_cert, str) or not _cai_re.fullmatch(r'[0-9a-fA-F]{64}', expected_cert):
                raise CameraActivationError('조회한 카메라의 인증서 정보를 확인하세요.')
            expected_cert = expected_cert.lower()
        route = _cai_route(ip, payload.get('source'))
        status, cert = _cai_request(ip, route, 'GET', _CAI_STATUS, expected_cert=expected_cert)
        if type(status.get('Initialized')) is not bool:
            raise CameraActivationError('카메라 초기화 여부를 확인하지 못했습니다. 카메라 웹 화면에서 확인하세요.')
        mac = _cai_neighbor(ip, route)
        if expected_mac is not None and mac != expected_mac:
            raise CameraActivationError('선택한 포트의 MAC과 현재 카메라 MAC이 다릅니다. 중복 IP·포트 연결을 다시 확인하세요.')
        policy = _cai_policy(status)
        key = status.get('PublicKey')
        if status['Initialized'] is False and (not isinstance(key, str) or 'PUBLIC KEY' not in key):
            raise CameraActivationError('초기 설정용 RSA 공개키가 없습니다. 카메라 웹 화면에서 설정하세요.')
        public = {'ip': ip, 'source': route['source'], 'interface': route['dev'], 'parent': route['parent'],
                  'mac': mac, 'cert_sha256': cert, 'initialized': status['Initialized'],
                  'activation_available': not status['Initialized'], 'password_policy': policy,
                  'browser_url': 'https://' + ip + '/', 'identity_verified': False,
                  'notice': '초기 등록 전에는 모델을 인증할 수 없습니다. 해당 카메라 한 대만 연결했는지 확인하세요.'}
        return public, route, key

    def inspect(self, payload):
        return self._inspect(payload)[0]

    def preview(self, payload):
        with self._lock:
            if not isinstance(payload, dict) or payload.get('isolated') is not True:
                raise CameraActivationError('설정할 카메라 한 대만 분리 연결했음을 확인해야 합니다.')
            expected_mac = _cai_mac(payload.get('expected_mac'))
            cert = payload.get('expected_cert_sha256')
            if not isinstance(cert, str) or not _cai_re.fullmatch(r'[0-9a-f]{64}', cert):
                raise CameraActivationError('먼저 초기 상태 점검으로 HTTPS 인증서를 확인하세요.')
            public, route, key = self._inspect(payload, cert)
            if public['initialized']:
                raise CameraActivationError('이미 초기 비밀번호가 등록되어 있습니다. 기존 비밀번호 변경 기능을 사용하세요.')
            if public['mac'] != expected_mac:
                raise CameraActivationError('점검 후 카메라 MAC이 변경되었습니다. 중복 IP를 확인하세요.')
            probe = _camera_admin_duplicate_probe(public['ip'], dict(route, expected_mac=expected_mac))
            # Validate RSA key support without a camera write or any real password.
            _camera_admin_encrypt(key, 'KeyValidationOnly')
            now = _cai_time.monotonic()
            self._plans = {t: p for t, p in self._plans.items() if p['deadline'] > now}
            if len(self._plans) >= 20:
                raise CameraActivationError('초기 설정 계획이 너무 많습니다. 잠시 후 다시 시도하세요.')
            token = _cai_secrets.token_urlsafe(32)
            plan = dict(public, arp_replies=probe['replies'], probe_flavor=probe['probe_flavor'],
                        isolated=True, action='initial_admin_password', username='admin')
            self._plans[token] = {'public': plan, 'route': route, 'deadline': now + self.TOKEN_SECONDS}
            return {'token': token, 'expires_in': self.TOKEN_SECONDS, 'plan': plan,
                    'notice': 'ARP 점검은 응답을 관측한 결과입니다. 물리적으로 한 대만 연결되었음을 보증하지 않습니다.'}

    def apply(self, payload):
        with self._lock:
            if self._closed or not isinstance(payload, dict):
                raise CameraActivationError('초기 설정 요청을 처리할 수 없습니다.')
            token = payload.get('token')
            saved = self._plans.pop(token, None) if isinstance(token, str) else None
            if not saved or saved['deadline'] <= _cai_time.monotonic():
                raise CameraActivationError('초기 설정 계획이 만료되었거나 이미 사용됐습니다. 다시 점검하세요.')
            before, route = saved['public'], saved['route']
            password = payload.get('new_password')
            confirm = payload.get('confirm_password')
            policy = before['password_policy']
            _camera_admin_password(password, confirm, maximum=policy['maximum'], tta=policy['tta'])
            fresh, fresh_route, public_key = self._inspect({'ip': before['ip'], 'source': route['source']}, before['cert_sha256'])
            if (fresh['initialized'] or fresh_route != route or fresh['mac'] != before['mac'] or
                    fresh['password_policy'] != policy):
                raise CameraActivationError('카메라 상태·경로·정책이 바뀌었습니다. 초기 비밀번호를 전송하지 않았습니다.')
            _camera_admin_duplicate_probe(before['ip'], dict(route, expected_mac=before['mac']))
            ciphertext = _camera_admin_encrypt(public_key, password)
            body = _cai_urlparse.quote(ciphertext, safe='').encode('ascii')
            # No retries: a timeout could mean the device accepted the new password.
            try:
                _cai_request(before['ip'], route, 'POST', _CAI_ACTIVATE, body, before['cert_sha256'])
            except CameraActivationError:
                raise CameraActivationError('초기 비밀번호 요청 이후 결과를 확인하지 못했습니다. 다시 전송하지 않았습니다. '
                    '카메라 웹 화면에서 초기 등록 여부를 먼저 확인하세요.') from None
            try:
                # A short wait follows the camera's own UI; status reads do not attempt login.
                _cai_time.sleep(1)
                after, after_route, _ = self._inspect({'ip': before['ip'], 'source': route['source']}, before['cert_sha256'])
                verified = after['initialized'] and after['mac'] == before['mac'] and after_route == route
            except CameraActivationError:
                verified = False
            if not verified:
                raise CameraActivationError('초기 비밀번호 요청을 보냈으나 등록 완료를 확인하지 못했습니다. '
                    '자동 재전송하지 않았습니다. 카메라 웹 화면에서 확인하세요.')
            return {'activated': True, 'initialized': True, 'ip': before['ip'], 'source': route['source'],
                    'interface': route['dev'], 'mac': before['mac'], 'cert_sha256': before['cert_sha256'],
                    'username': 'admin', 'identity_verified': False, 'registration_required': True,
                    'message': '초기 비밀번호 등록을 확인했습니다. 새 비밀번호로 장비 정보를 확인한 뒤 카메라를 등록하세요.'}
# END EMBEDDED CAMERA_ACTIVATION

# BEGIN EMBEDDED CAMERA_ADMIN
# Embedded after camera_profiles, camera_activation and switch_manager.
# Reading/previewing never changes camera or host networking.
import contextlib as _ca_contextlib
import hashlib as _ca_hashlib
import http.client as _ca_http
import ipaddress as _ca_ipaddress
import json as _ca_json
import os as _ca_os
from pathlib import Path as _ca_Path
import re as _ca_re
import secrets as _ca_secrets
import subprocess as _ca_subprocess
import threading as _ca_threading
import time as _ca_time
from urllib.parse import urlsplit as _ca_urlsplit, unquote as _ca_unquote, quote as _ca_quote, urlencode as _ca_urlencode


class CameraAdminError(ValueError):
    """Safe administrative error, never containing credentials or response bodies."""


class _CameraAdminAuthError(CameraAdminError):
    pass


class _CameraAdminWriteRejected(CameraAdminError):
    """The camera explicitly refused the request."""


def _ca_write_rejection(status, text):
    """Return only protocol codes and known field names; never copy arbitrary camera text."""
    http_refusal = status in (400, 403, 404, 405, 409, 422, 429)
    first = _ca_re.search(r'^(?:\ufeff)?[ \t]*(?:NG\b|(?:Error|ErrorCode)[ \t]*=)', text, _ca_re.I | _ca_re.M)
    if not http_refusal and (status != 200 or not first):
        return None
    errors = [] if not first else [line.strip() for line in text[first.start():first.start()+4096].lstrip('\ufeff').splitlines()
                                  if _ca_re.match(r'^\s*(?:NG\b|(?:Error|ErrorCode)\s*=)', line, _ca_re.I)]
    parts = ['HTTP ' + str(status)]
    known_codes = ('InvalidParameter', 'InvalidValue', 'InvalidRequest', 'InvalidOperation',
                   'MissingParameter', 'UnsupportedParameter', 'NotSupported', 'OutOfRange',
                   'BadRequest', 'PermissionDenied', 'Unauthorized', 'InternalError')
    known_fields = ('InterfaceName', 'IPv4Type', 'IPv4Address', 'IPv4SubnetMask', 'IPv4PrefixLength',
                    'IPv4Gateway', 'Index', 'UserID', 'Password', 'IsPasswordEncrypted')
    for line in errors:
        match = _ca_re.match(r'(?:NG\s*\[|(?:Error|ErrorCode)\s*=)\s*([^\]:,;]+)', line, _ca_re.I)
        if match:
            value = match[1].strip()
            normalized = _ca_re.sub(r'[\s_-]', '', value).lower()
            code = next((item for item in known_codes if item.lower() == normalized), None)
            if _ca_re.fullmatch(r'-?[0-9]{1,5}|0x[0-9a-fA-F]{1,4}', value):
                code = value
            if code and ('CGI ' + code) not in parts:
                parts.append('CGI ' + code)
        for name in known_fields:
            if name not in parts and _ca_re.search(r'\b' + name + r'\b', line, _ca_re.I):
                parts.append(name)
    if errors and len(parts) == 1:
        parts.append('CGI 오류')
    return _CameraAdminWriteRejected('카메라가 변경 요청을 거부했습니다 (' + ', '.join(parts) + ').')


def _ca_route(host):
    try:
        return _cai_route(host)
    except (ValueError, OSError, KeyError, TypeError):
        raise CameraAdminError('카메라 IP로 가는 직접 enp2 경로를 확인하지 못했습니다. ED의 IP·경로를 먼저 준비하세요. LTE는 사용하지 않습니다.') from None


def _ca_url(url, host=None, username=None, password=None):
    parsed = _ca_urlsplit(url)
    user = _ca_unquote(parsed.username or '') if username is None else username
    secret = _ca_unquote(parsed.password or '') if password is None else password
    target = host or parsed.hostname
    userinfo = (parsed.netloc.rsplit('@', 1)[0] if username is None and password is None
                else _ca_quote(user, safe='') + ':' + _ca_quote(secret, safe=''))
    netloc = userinfo + '@' + target
    if parsed.port is not None:
        netloc += ':' + str(parsed.port)
    return parsed._replace(netloc=netloc).geturl()


def _ca_digest(challenge, username, password, uri, method, nc):
    if method == 'GET':
        return _camera_digest_header(challenge, username, password, uri, nc)
    if not challenge.lower().startswith('digest '):
        raise CameraAdminError('카메라 Digest 인증 방식이 지원되지 않습니다.')
    fields = _cp_parse_pairs(_cp_parse_list(challenge[7:]))
    algorithm = fields.get('algorithm', 'MD5').upper()
    name = {'MD5': 'md5', 'SHA-256': 'sha256', 'SHA-512-256': 'sha512_256'}.get(algorithm.replace('-SESS', ''))
    qops = [item.strip() for item in fields.get('qop', '').split(',') if item.strip()]
    if not name or not fields.get('nonce') or 'realm' not in fields or (qops and 'auth' not in qops):
        raise CameraAdminError('카메라 Digest 인증 방식이 지원되지 않습니다.')
    digest = lambda value: _ca_hashlib.new(name, value.encode('utf-8')).hexdigest()
    cnonce, count = _ca_secrets.token_hex(16), '{:08x}'.format(nc)
    first = digest(username + ':' + fields['realm'] + ':' + password)
    if algorithm.endswith('-SESS'):
        first = digest(first + ':' + fields['nonce'] + ':' + cnonce)
    second = digest(method + ':' + uri)
    response = digest(first + ':' + fields['nonce'] + ':' +
                      (count + ':' + cnonce + ':auth:' if qops else '') + second)
    values = {'username': username, 'realm': fields['realm'], 'nonce': fields['nonce'],
              'uri': uri, 'response': response, 'algorithm': algorithm}
    if qops:
        values.update(qop='auth', nc=count, cnonce=cnonce)
    elif algorithm.endswith('-SESS'):
        values['cnonce'] = cnonce
    if fields.get('opaque'):
        values['opaque'] = fields['opaque']
    if any(any(ord(c) < 32 or ord(c) == 127 for c in value) for value in values.values()):
        raise CameraAdminError('카메라 인증 정보 형식이 올바르지 않습니다.')
    quote = lambda value: '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return 'Digest ' + ', '.join(key + '=' + (value if key in ('algorithm', 'qop', 'nc') else quote(value))
                                  for key, value in values.items())


class CameraAdminClient(WisenetProfileClient):
    def __init__(self, url):
        super().__init__(url)
        self.allow_writes = False
        self.write_attempted = False
        self._route = _ca_route

    def request(self, path, params=None, method='GET', body=None, accept='text/plain'):
        params = params or {}
        pair = (path, params.get('msubmenu'), params.get('action'), method)
        readonly = {('/stw-cgi/system.cgi', 'deviceinfo', 'view', 'GET'),
                    ('/stw-cgi/network.cgi', 'interface', 'view', 'GET'),
                    ('/stw-cgi/security.cgi', 'users', 'view', 'GET'),
                    ('/stw-cgi/security.cgi', 'rsa', 'view', 'GET'),
                    ('/stw-cgi/media.cgi', 'videoprofile', 'view', 'GET'),
                    ('/stw-cgi/attributes.cgi/cgis', None, None, 'GET'),
                    ('/stw-cgi/attributes.cgi/attributes', None, None, 'GET')}
        writes = {('/stw-cgi/network.cgi', 'interface', 'set', 'GET'),
                  ('/stw-cgi/security.cgi', 'users', 'update', 'POST')}
        if pair not in readonly and not (self.allow_writes and pair in writes):
            raise CameraAdminError('허용되지 않은 카메라 관리 요청입니다.')
        if any('password' in key.lower() for key in params if key != 'IsPasswordEncrypted'):
            raise CameraAdminError('비밀번호를 카메라 주소의 쿼리에 넣을 수 없습니다.')
        # The device's own admin form sends only Index and UserID for a rename.
        rename_only = (pair == ('/stw-cgi/security.cgi', 'users', 'update', 'POST')
                       and set(params) == {'msubmenu', 'action', 'Index', 'UserID'}
                       and params['Index'] == '0' and body == b'')
        if method == 'POST' and not rename_only and (not isinstance(body, bytes) or not 1 <= len(body) <= 16384):
            raise CameraAdminError('암호화된 변경 요청을 확인하지 못했습니다.')
        uri = path + ('?' + _ca_urlencode(params) if params else '')
        for attempt in range(2):
            route = self._route(self.host)
            headers = {'Accept': accept, 'Connection': 'close'}
            if body is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            authenticated = self.challenge is not None
            if authenticated:
                self.nonce_count += 1
                headers['Authorization'] = _ca_digest(self.challenge, self.username, self.password, uri, method, self.nonce_count)
            connection = _ca_http.HTTPSConnection(self.host, 443, timeout=8, context=self.context,
                                                  source_address=(route['source'], 0))
            try:
                connection.connect()
                certificate = _ca_hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                if self.certificate is not None and self.certificate != certificate:
                    raise CameraAdminError('카메라 인증서가 변경되어 중단했습니다. 대상 장비를 다시 확인하세요.')
                self.certificate = certificate
                if pair in writes:
                    self.write_attempted = True
                connection.request(method, uri, body=body, headers=headers)
                response = connection.getresponse()
                status, challenge = response.status, response.getheader('WWW-Authenticate', '')
                raw = response.read(4 * 1024 * 1024 + 1)
            except CameraAdminError:
                raise
            except (OSError, _ca_http.HTTPException):
                raise CameraAdminError('카메라 관리 연결이 종료되었거나 응답하지 않습니다.') from None
            finally:
                try:
                    connection.close()
                except OSError:
                    # The camera may close its socket immediately after a
                    # successful network/password update. Still verify once.
                    pass
            if status == 401:
                if authenticated or attempt:
                    raise _CameraAdminAuthError('카메라 인증이 거부되었습니다. 자동으로 다시 로그인하지 않습니다.')
                self.challenge = challenge
                continue
            if pair in writes and status in (400, 403, 404, 405, 409, 422, 429):
                raise _ca_write_rejection(status, raw[:4096].decode('utf-8-sig', errors='replace'))
            if status != 200 or len(raw) > 4 * 1024 * 1024:
                raise CameraAdminError('카메라 관리 요청이 거부되었거나 응답이 너무 큽니다.')
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeError:
                raise CameraAdminError('카메라 응답을 읽을 수 없습니다.') from None
            if pair in writes:
                rejection = _ca_write_rejection(status, text)
                if rejection:
                    raise rejection
            return text
        raise CameraAdminError('카메라 인증을 완료하지 못했습니다.')

    def get(self, path, params=None):
        return self.request(path, params)

    def public_key(self):
        raw = self.request('/stw-cgi/security.cgi', {'msubmenu': 'rsa', 'action': 'view'}, accept='application/json')
        try:
            value = _ca_json.loads(raw)['PublicKey']
        except (ValueError, KeyError, TypeError):
            match = _ca_re.search(r'-----BEGIN (?:RSA )?PUBLIC KEY-----[\s\S]+?-----END (?:RSA )?PUBLIC KEY-----', raw)
            value = match[0] if match else ''
        if not isinstance(value, str) or not 100 <= len(value) <= 16384:
            raise CameraAdminError('카메라의 비밀번호 암호화 키를 확인하지 못했습니다.')
        return value

    def update_password(self, snapshot, username, new_password):
        current_verification = snapshot['capabilities']['current_password_verification']
        plaintext = (_ca_json.dumps({'CurrentPassword': self.password, 'NewPassword': new_password},
                                   ensure_ascii=False, separators=(',', ':'))
                     if current_verification else new_password)
        encrypted = _camera_admin_encrypt(self.public_key(), plaintext)
        body = encrypted if current_verification else _ca_quote(encrypted, safe="~()*!.'-")
        return self.request('/stw-cgi/security.cgi', {'msubmenu': 'users', 'action': 'update',
            'Index': str(snapshot['user']['index']), 'UserID': username, 'IsPasswordEncrypted': 'True'},
            method='POST', body=body.encode('ascii'))

    def update_username(self, snapshot, username):
        params = {'msubmenu': 'users', 'action': 'update',
                  'Index': str(snapshot['user']['index']), 'UserID': username}
        return self.request('/stw-cgi/security.cgi', params, method='POST', body=b'')

    def update_network(self, desired, interface_name):
        if interface_name != 'NetworkInterface1':
            raise CameraAdminError('조회한 카메라의 네트워크 인터페이스를 확인하세요.')
        # This camera's SUNAPI setter accepts IPv4 settings on its single interface.
        # InterfaceName is returned by the read API; the OEM form sends it only for NVRs.
        return self.get('/stw-cgi/network.cgi', dict(msubmenu='interface', action='set',
            IPv4Type='Manual', IPv4Address=desired['ip'], IPv4SubnetMask=desired['netmask'],
            IPv4Gateway=desired['gateway']))


def _ca_user(raw, username):
    lines = [line.strip().strip('"') for line in raw.splitlines() if line.strip()]
    header = next((line.split('=', 1)[1].split('/') for line in lines if line.startswith('Users.Index=')), None)
    if not header or len(header) != len(set(header)) or not {'UserID', 'AdminAccess'} <= set(header):
        raise CameraAdminError('카메라 계정 목록 형식을 확인하지 못했습니다.')
    rows = []
    for line in lines:
        match = _ca_re.fullmatch(r'Users\.(\d+)=(.*)', line)
        if not match:
            continue
        values = match[2].split('/')
        if len(values) != len(header):
            raise CameraAdminError('카메라 계정 정보를 해석하지 못했습니다.')
        row = dict(zip(header, values))
        row.pop('Password', None)
        if row.get('UserID') == username:
            rows.append({'index': int(match[1]), 'fields': row})
    if len(rows) != 1 or rows[0]['fields'].get('AdminAccess') != 'True' or rows[0]['index'] != 0:
        raise CameraAdminError('현재 관리 계정의 인덱스와 관리자 권한을 확인하지 못했습니다. 기본 관리자 한 계정만 지원합니다.')
    return rows[0]


def _ca_capabilities(cgis, attributes):
    cgi, attrs = _camera_xml(cgis), _camera_xml(attributes)
    network = cgi.find("cgi[@name='network']/submenu[@name='interface']/action[@name='set']")
    users = cgi.find("cgi[@name='security']/submenu[@name='users']/action[@name='add/update']")
    net_params = {item.get('name') for item in network.findall('parameter') if item.get('request') == 'true'} if network is not None else set()
    user_params = {item.get('name') for item in users.findall('parameter') if item.get('request') == 'true'} if users is not None else set()
    support = attrs.find("group[@name='Security']/category[@name='Support']")
    features = {item.get('name'): item.get('value') for item in support.findall('attribute')} if support is not None else {}
    password = users.find("parameter[@name='Password']/dataType/string") if users is not None else None
    prefix = network.find("parameter[@name='IPv4PrefixLength']/dataType/int") if network is not None else None
    return {'network': {'IPv4Type', 'IPv4Address', 'IPv4SubnetMask', 'IPv4Gateway'} <= net_params,
            'password': {'Index', 'UserID', 'Password', 'IsPasswordEncrypted'} <= user_params,
            'can_change_username': (features.get('AdminIDChangeable') == 'True'
                                    and {'Index', 'UserID'} <= user_params),
            'current_password_verification': features.get('CurrentPasswordVerification') in ('Optional', 'Mandatory', 'True'),
            'new_password_policy': features.get('NewPasswordPolicy') == 'True',
            'password_maximum': int(password.get('maxlen', '0')) if password is not None else 0,
            'prefix_min': int(prefix.get('min', '1')) if prefix is not None else 1,
            'prefix_max': int(prefix.get('max', '32')) if prefix is not None else 32,
            'schema_sha256': _ca_hashlib.sha256((cgis + '\n' + attributes).encode()).hexdigest()}


def _ca_snapshot(client):
    info = _camera_pairs(client.get('/stw-cgi/system.cgi', {'msubmenu': 'deviceinfo', 'action': 'view'}))
    identity = {key: info.get(key, '') for key in ('Model', 'SerialNumber', 'ConnectedMACAddress', 'FirmwareVersion')}
    if identity['Model'] != 'PG-ABS08041402' or not identity['SerialNumber'] or not _ca_re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', identity['ConnectedMACAddress']):
        raise CameraAdminError('확인된 카메라 모델·시리얼·MAC이 아닙니다. 이 모델의 자동 변경은 지원하지 않습니다.')
    network = _camera_pairs(client.get('/stw-cgi/network.cgi', {'msubmenu': 'interface', 'action': 'view'}))
    if network.get('MACAddress', '').lower() != identity['ConnectedMACAddress'].lower():
        raise CameraAdminError('카메라 네트워크와 장치 MAC이 다릅니다.')
    fields = {'ip': network.get('IPv4Address'), 'netmask': network.get('IPv4SubnetMask'),
              'gateway': network.get('IPv4Gateway'), 'mode': network.get('IPv4Type')}
    if fields['mode'] not in ('Manual', 'DHCP') or fields['ip'] != client.host or network.get('InterfaceName') != 'NetworkInterface1':
        raise CameraAdminError('접속 주소와 일치하는 IPv4 인터페이스 설정을 확인하지 못했습니다.')
    capabilities = _ca_capabilities(client.get('/stw-cgi/attributes.cgi/cgis'),
                                    client.get('/stw-cgi/attributes.cgi/attributes'))
    user = _ca_user(client.get('/stw-cgi/security.cgi', {'msubmenu': 'users', 'action': 'view', 'UserID': client.username}), client.username)
    return {'identity': identity, 'network': fields, 'user': user, 'capabilities': capabilities,
            'interface_name': network['InterfaceName'],
            'certificate_sha256': client.certificate}


def _ca_private_write(path, data):
    fd = _ca_os.open(str(path), _ca_os.O_WRONLY | _ca_os.O_CREAT | _ca_os.O_EXCL | _ca_os.O_NOFOLLOW, 0o600)
    with _ca_os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        _ca_os.fsync(stream.fileno())


class CameraAdminManager:
    def __init__(self, controller, client_factory=None):
        self.controller = controller
        self.client_factory = client_factory or CameraAdminClient
        self._route = _ca_route
        self._available = lambda ip, route: _camera_admin_address_available(ip, route)
        self._handover_available = _camera_admin_handover_available
        self._probe = lambda ip, route: _camera_admin_duplicate_probe(ip, route)
        self._snapshot = _ca_snapshot
        self._lock = _ca_threading.RLock()
        self._plans = {}
        self._closed = False

    @staticmethod
    def _payload(value):
        if not isinstance(value, dict):
            raise CameraAdminError('카메라 관리 요청 형식을 확인하세요.')
        return value

    def _invalidate_settings(self):
        lock = getattr(self.controller, 'settings_lock', None)
        with lock if lock is not None else _ca_contextlib.nullcontext():
            self.controller.cached_settings = self.controller.cached_revision = None

    def _camera(self, name):
        if self._closed or self.controller.closing:
            raise CameraAdminError('카메라 관리 서비스가 종료되었습니다.')
        data, revision = self.controller.read_config()
        rows = data.get('cameras')
        if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get('url'), str) for row in rows):
            raise CameraAdminError('이 기능은 녹화 설정에 직접 저장된 카메라만 지원합니다. setup.ini는 변경하지 않습니다.')
        matches = [(i, row) for i, row in enumerate(rows) if row.get('name') == name]
        if len(matches) != 1:
            raise CameraAdminError('저장된 카메라 한 대를 선택하세요.')
        index, camera = matches[0]
        parsed = _ca_urlsplit(camera.get('url', ''))
        try:
            address = _ca_ipaddress.IPv4Address(parsed.hostname or '')
        except ValueError:
            raise CameraAdminError('카메라의 IPv4 주소를 확인하세요.') from None
        if parsed.scheme != 'rtsp' or not parsed.username or parsed.password is None or address.is_multicast or address.is_unspecified or address.is_loopback:
            raise CameraAdminError('저장된 RTSP 관리 계정과 IPv4 주소를 확인하세요.')
        if sum(_ca_urlsplit(row.get('url', '')).hostname == parsed.hostname for row in rows) != 1:
            raise CameraAdminError('같은 IP가 여러 카메라 항목에 등록되어 있습니다. 중복 항목을 먼저 정리하세요.')
        return data, revision, index, camera

    @staticmethod
    def _public(name, client, snapshot):
        caps = snapshot['capabilities']
        return {'camera': name, 'host': client.host, 'identity': snapshot['identity'],
                'network': snapshot['network'], 'username': client.username,
                'certificate_sha256': snapshot['certificate_sha256'],
                'can_change_username': caps['can_change_username'],
                'supported': {key: caps[key] for key in ('network', 'password')},
                'warnings': ['이 카메라를 사용하는 센터·NVR·다른 ED의 접속 정보는 별도로 변경해야 합니다.']}

    def inspect(self, payload):
        self._payload(payload)
        with self.controller.action_lock, self._lock:
            _, _, _, camera = self._camera(payload.get('camera'))
            client = self.client_factory(camera['url'])
            return self._public(camera['name'], client, self._observe(client))

    def _observe(self, client, expected_mac=None):
        route = self._route(client.host)
        if expected_mac is not None:
            route = dict(route, expected_mac=_cai_mac(expected_mac))
        probe = self._probe(client.host, route)
        if expected_mac is not None and probe['mac'].lower() != route['expected_mac']:
            raise CameraAdminError('선택한 포트의 카메라와 현재 IP의 MAC이 다릅니다. 장비를 다시 조회하세요.')
        snapshot = self._snapshot(client)
        if probe['mac'].lower() != snapshot['identity']['ConnectedMACAddress'].lower():
            raise CameraAdminError('ARP 응답과 카메라 장치 MAC이 다릅니다. 같은 IP의 중복 장치를 확인하세요.')
        return snapshot

    def _candidate(self, payload):
        if self._closed or self.controller.closing:
            raise CameraAdminError('카메라 관리 서비스가 종료되었습니다.')
        if not isinstance(payload, dict):
            raise CameraAdminError('카메라 조회 입력 형식이 올바르지 않습니다.')
        host = _cai_ip(payload.get('ip'))
        username, password = payload.get('username'), payload.get('password')
        if (not isinstance(username, str) or not _ca_re.fullmatch(r'[A-Za-z0-9_.-]{1,8}', username)
                or not isinstance(password, str) or not 1 <= len(password) <= 64
                or any(ord(c) < 32 or ord(c) == 127 for c in password)):
            raise CameraAdminError('현재 카메라 관리 계정을 입력하세요.')
        mac = _cai_mac(payload.get('expected_mac'))
        certificate = payload.get('expected_cert_sha256')
        if not isinstance(certificate, str) or not _ca_re.fullmatch(r'[0-9a-fA-F]{64}', certificate):
            raise CameraAdminError('먼저 초기 설정 조회에서 MAC·인증서 정보를 확인하세요.')
        url = _ca_url('rtsp://admin:placeholder@' + host + ':554/profile4/media.smp',
                      username=username, password=password)
        client = self.client_factory(url)
        client.certificate = certificate.lower()
        snapshot = self._observe(client, expected_mac=mac)
        self._expected(payload, client, snapshot)
        return url, client, snapshot

    def inspect_candidate(self, payload):
        """Authenticate one discovered camera without registering it or storing credentials."""
        with self.controller.action_lock, self._lock:
            _, client, snapshot = self._candidate(payload)
            profiles = _camera_profile_rows(client.get('/stw-cgi/media.cgi', {'msubmenu': 'videoprofile', 'action': 'view'}))
            result = self._public(None, client, snapshot)
            result.update(registered=False, credentials_saved=False, route=self._route(client.host),
                profiles=[{'id': int(profile), **{key: values[key] for key in
                    ('Name', 'EncodingType', 'Resolution', 'FrameRate') if key in values}}
                    for profile, values in sorted(profiles.items(), key=lambda row: int(row[0]))],
                detail='장비와 현재 설정만 확인했습니다. 계정 저장·카메라 변경은 수행하지 않았습니다.')
            return result

    @staticmethod
    def _expected(payload, client, snapshot):
        pins = {'expected_host': client.host,
                'expected_mac': snapshot['identity']['ConnectedMACAddress'].lower(),
                'expected_cert_sha256': snapshot['certificate_sha256']}
        for key, expected in pins.items():
            if key in payload and str(payload[key]).replace('-', ':').lower() != str(expected).lower():
                raise CameraAdminError('조회한 카메라와 현재 장비의 식별정보가 다릅니다. 다시 조회하세요.')

    def _desired(self, payload, snapshot, client):
        action = payload.get('action')
        if action == 'network':
            if not snapshot['capabilities']['network']:
                raise CameraAdminError('이 카메라의 네트워크 변경 API를 확인하지 못했습니다.')
            try:
                ip = _ca_ipaddress.IPv4Address(payload.get('ip', ''))
                gateway = _ca_ipaddress.IPv4Address(payload.get('gateway', ''))
                network = _ca_ipaddress.IPv4Network(str(ip) + '/' + str(payload.get('netmask', '')), strict=False)
            except (ValueError, TypeError):
                raise CameraAdminError('IPv4 주소·연속된 서브넷 마스크·게이트웨이를 확인하세요.') from None
            if (ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip in (network.network_address, network.broadcast_address)
                    or not snapshot['capabilities']['prefix_min'] <= network.prefixlen <= snapshot['capabilities']['prefix_max']):
                raise CameraAdminError('카메라 지원 범위 안의 호스트 IP와 서브넷을 입력하세요.')
            if gateway != _ca_ipaddress.IPv4Address('0.0.0.0') and (gateway not in network or gateway in (ip, network.network_address, network.broadcast_address)):
                raise CameraAdminError('게이트웨이는 새 카메라 대역 안의 다른 호스트 주소여야 합니다. 사용하지 않으면 0.0.0.0으로 지정하세요.')
            old_route = self._route(client.host)
            if payload.get('defer_reconnect') is True:
                old_network = _ca_ipaddress.IPv4Network(client.host + '/' + snapshot['network']['netmask'], strict=False)
                if ip in old_network or _ca_ipaddress.IPv4Address(client.host) in network:
                    raise CameraAdminError('같은 대역에서는 새 IP 재접속을 확인하는 일반 IP 변경을 사용하세요.')
                self._handover_available(str(ip), old_route)
            else:
                route = self._route(str(ip))
                if _ca_ipaddress.IPv4Address(route['source']) not in network or route['source'] == str(ip):
                    raise CameraAdminError('ED의 enp2 소스 IP가 새 카메라 대역에 없거나 카메라 IP와 같습니다. 기존 주소를 유지한 채 새 대역의 ED 보조 IP와 직접 경로를 먼저 준비해야 합니다. LTE 경로는 사용하지 않습니다.')
                if route['parent'] != old_route['parent'] or route['dev'] != old_route['dev']:
                    raise CameraAdminError('변경 전후 카메라는 같은 enp2 인터페이스로 접근해야 합니다.')
                if str(ip) != client.host:
                    self._available(str(ip), route)
            desired = {'ip': str(ip), 'netmask': str(network.netmask), 'gateway': str(gateway), 'mode': 'Manual'}
            if desired == snapshot['network']:
                raise CameraAdminError('변경할 네트워크 설정이 없습니다.')
            return desired
        if action in ('password', 'username'):
            caps = snapshot['capabilities']
            if action == 'password' and not caps['password']:
                raise CameraAdminError('이 카메라의 비밀번호 변경 API를 확인하지 못했습니다.')
            username = payload.get('new_username') or client.username
            if not isinstance(username, str) or not _ca_re.fullmatch(r'[A-Za-z0-9_.-]{1,8}', username):
                raise CameraAdminError('관리 아이디는 영문·숫자·밑줄·마침표·하이픈 1~8자로 입력하세요.')
            if username != client.username and not caps['can_change_username']:
                raise CameraAdminError('이 카메라는 관리 아이디 변경을 지원하지 않습니다.')
            if action == 'username':
                if username == client.username:
                    raise CameraAdminError('현재 아이디와 다른 새 아이디를 입력하세요.')
                return {'username': username, 'password_changed': False}
            _camera_admin_password(payload.get('new_password'), payload.get('confirm_password'), username=username,
                                   maximum=min(64, caps['password_maximum']), tta=caps.get('new_password_policy', False))
            if payload['new_password'] == client.password and username == client.username:
                raise CameraAdminError('변경할 아이디·비밀번호가 없습니다.')
            return {'username': username, 'password_changed': payload['new_password'] != client.password}
        raise CameraAdminError('네트워크 또는 계정 변경을 선택하세요.')

    def plan(self, payload):
        self._payload(payload)
        with self.controller.action_lock, self._lock:
            direct = 'direct_target' in payload
            action = payload.get('action')
            deferred = payload.get('defer_reconnect', False)
            if type(deferred) is not bool or (deferred and (not direct or action != 'network')):
                raise CameraAdminError('새 IP 재접속을 나중에 확인하는 옵션은 직접 입력한 카메라의 IP 변경에서만 사용할 수 있습니다.')
            warnings = ['적용 전에 예약 대기를 포함한 녹화와 실시간 재생을 중지하세요.',
                        '센터·NVR·다른 ED의 접속 정보는 자동으로 변경하지 않습니다.']
            if deferred:
                warnings.append('현재 주소로 IP 변경을 한 번 요청합니다. 새 IP 접속과 녹화기 주소 갱신은 새 대역이 준비된 후 별도로 확인하세요.')
            if direct:
                if action not in ('network', 'username') or payload.get('isolated') is not True:
                    raise CameraAdminError('대상 IP의 카메라 한 대를 확인하고 IP 또는 아이디 변경을 선택하세요.')
                data, revision = self.controller.read_config()
                rows = data.get('cameras', 'auto')
                if rows == 'auto':
                    rows = []
                if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get('url'), str) for row in rows):
                    raise CameraAdminError('카메라 포트에서 주소를 변경하려면 녹화기의 직접 IP 설정을 확인하세요.')
                url, client, before = self._candidate(payload['direct_target'])
                existing = [row for row in rows if _ca_urlsplit(row['url']).hostname == client.host]
                if existing:
                    data, revision, index, camera = self._camera(existing[0]['name'])
                    stored = _ca_urlsplit(camera['url'])
                    if (_ca_unquote(stored.username or ''), _ca_unquote(stored.password or '')) != (client.username, client.password):
                        raise CameraAdminError('이 IP가 다른 계정으로 녹화기에 등록되어 있습니다. 장비 설정의 저장 계정을 먼저 확인하세요.')
                else:
                    index, camera = None, {'name': client.host, 'url': url}
            else:
                data, revision, index, camera = self._camera(payload.get('camera'))
                client = self.client_factory(camera['url'])
                before = self._observe(client)
            self._expected(payload, client, before)
            desired = self._desired(payload, before, client)
            if index is not None:
                self._configured_ip(data, index, action, desired)
            else:
                if action == 'network' and any(_ca_urlsplit(row['url']).hostname == desired['ip'] for row in rows):
                    warnings.append('새 IP는 녹화기에 이미 등록된 주소입니다. 해당 카메라를 교체하는 경우에만 적용하세요.')
                warnings.append('이 카메라의 접속 정보는 녹화기에 따로 등록되지 않습니다.')
            token = _ca_secrets.token_urlsafe(32)
            now = _ca_time.monotonic()
            self._plans = {key: row for key, row in self._plans.items() if now - row['created'] < 300}
            if len(self._plans) >= 16:
                raise CameraAdminError('변경 미리보기가 너무 많습니다. 잠시 후 다시 시도하세요.')
            public = {'plan_id': token, 'camera': camera['name'], 'action': action, 'expires_seconds': 300,
                'camera_host': client.host, 'identity': before['identity'],
                'before': before['network'] if action == 'network' else {'username': client.username},
                'after': desired, 'recorder_will_update': index is not None and not deferred,
                'defer_reconnect': deferred, 'warnings': warnings}
            self._plans[token] = {'created': now, 'camera': camera['name'], 'revision': revision,
                'url_hash': _ca_hashlib.sha256(camera['url'].encode()).hexdigest(), 'snapshot': before,
                'client': client, 'action': action, 'desired': desired, 'public': public,
                'defer_reconnect': deferred,
                'standalone_url': camera['url'] if index is None else None,
                'password_hash': _ca_hashlib.sha256(payload['new_password'].encode()).hexdigest() if action == 'password' else None}
            return public

    @staticmethod
    def _configured_ip(data, index, action, desired):
        if action == 'network' and any(i != index and _ca_urlsplit(row['url']).hostname == desired['ip']
                                       for i, row in enumerate(data['cameras'])):
            raise CameraAdminError('새 IP가 다른 저장된 카메라에 사용 중입니다. 현재 응답이 없어도 중복 주소로 변경하지 않습니다.')

    @_ca_contextlib.contextmanager
    def _idle(self):
        if self._closed or self.controller.closing:
            raise CameraAdminError('카메라 관리 서비스가 종료되었습니다.')
        state = self.controller.state()
        if state.get('error') or state.get('running') or state.get('external_running') or state.get('active_cameras'):
            raise CameraAdminError('예약 대기를 포함한 녹화를 먼저 중지하세요.')
        acquired = []
        try:
            for obj, key in ((self.controller.live_view, '_start_lock'), (self.controller.camera_snapshots, '_capture_lock')):
                lock = getattr(obj, key)
                if not lock.acquire(blocking=False):
                    raise CameraAdminError('실시간 재생 또는 이미지 캡처가 진행 중입니다. 중지한 뒤 다시 시도하세요.')
                acquired.append(lock)
            if self.controller.live_view._session is not None:
                raise CameraAdminError('실시간 재생을 먼저 중지하세요.')
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def _backup(self, entry):
        root = _ca_Path(self.controller.config_path).parent / '.camera-admin-backups'
        if root.is_symlink():
            raise CameraAdminError('카메라 백업 폴더의 링크를 확인하세요.')
        root.mkdir(mode=0o700, exist_ok=True)
        directory = root / (_ca_time.strftime('%Y%m%d_%H%M%S') + '_' + _ca_secrets.token_hex(8))
        directory.mkdir(mode=0o700)
        if not entry.get('standalone_url'):
            _ca_private_write(directory / 'recording_before.json', _ca_Path(self.controller.config_path).read_bytes())
        safe = {'camera': entry['camera'], 'before': entry['snapshot'], 'change': entry['public']}
        _ca_private_write(directory / 'before.json', _ca_json.dumps(safe, ensure_ascii=False, indent=2).encode())
        return directory

    def register(self, payload):
        """Append one verified camera; never activate/change it or start recording."""
        self._payload(payload)
        with self.controller.action_lock, self._lock, self._idle():
            if self._closed or self.controller.closing:
                raise CameraAdminError('카메라 관리 서비스가 종료되었습니다.')
            if payload.get('isolated') is not True:
                raise CameraAdminError('기본 IP가 겹치는 다른 카메라를 분리한 상태인지 확인하세요.')
            try:
                host = str(_ca_ipaddress.IPv4Address(payload.get('ip', '')))
                if str(payload.get('profile')) not in ('2', '4'):
                    raise ValueError()
                profile = int(payload.get('profile'))
            except (TypeError, ValueError):
                raise CameraAdminError('카메라 IP와 사용할 프로필 2 또는 4를 선택하세요.') from None
            if isinstance(payload.get('profile'), bool) or profile not in (2, 4):
                raise CameraAdminError('사용할 프로필 2 또는 4를 선택하세요.')
            username, password = payload.get('username'), payload.get('password')
            if (not isinstance(username, str) or not _ca_re.fullmatch(r'[A-Za-z0-9_.-]{1,8}', username)
                    or not isinstance(password, str) or not 1 <= len(password) <= 64
                    or any(ord(c) < 32 or ord(c) == 127 for c in password)):
                raise CameraAdminError('현재 카메라 관리 계정을 입력하세요.')
            if (not _ca_re.fullmatch(r'(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}', str(payload.get('expected_mac', '')))
                    or not _ca_re.fullmatch(r'[0-9a-fA-F]{64}', str(payload.get('expected_cert_sha256', '')))):
                raise CameraAdminError('초기 설정 조회에서 확인한 MAC·인증서 정보가 필요합니다.')
            data, revision = self.controller.read_config()
            rows = data.get('cameras')
            if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get('url'), str) for row in rows):
                raise CameraAdminError('직접 저장된 JSON 카메라 목록에만 추가할 수 있습니다. setup.ini는 변경하지 않습니다.')
            if any(_ca_urlsplit(row['url']).hostname == host for row in rows):
                raise CameraAdminError('이 카메라 IP가 이미 등록되어 있습니다.')
            base_name = 'camera_' + host.replace('.', '_')
            names = {row.get('name', '').lower() for row in rows}
            name, suffix = base_name, 2
            while name.lower() in names:
                name = base_name + '_' + str(suffix)
                suffix += 1
            url = _ca_url('rtsp://admin:placeholder@' + host + ':554/profile' + str(profile) + '/media.smp',
                          username=username, password=password)
            client = self.client_factory(url)
            client.certificate = payload['expected_cert_sha256'].lower()
            snapshot = self._observe(client, expected_mac=payload['expected_mac'])
            self._expected(payload, client, snapshot)
            profiles = _camera_profile_rows(client.get('/stw-cgi/media.cgi', {'msubmenu': 'videoprofile', 'action': 'view'}))
            if str(profile) not in profiles:
                raise CameraAdminError('선택한 프로필이 카메라에 없습니다. 먼저 카메라 웹 설정에서 확인하세요.')
            proposed = _ca_json.loads(_ca_json.dumps(data))
            proposed['cameras'].append({'name': name, 'url': url})
            settings_from_data(proposed, _ca_Path(self.controller.config_path).parent)
            backup = self._backup({'camera': name, 'snapshot': snapshot,
                'public': {'action': 'register', 'host': host, 'profile': profile, 'username': username}})
            if self.controller.read_config()[1] != revision:
                raise CameraAdminError('등록 중 녹화 설정이 변경되었습니다. 다시 조회하세요.')
            atomic_private_text(self.controller.config_path, _ca_json.dumps(proposed, ensure_ascii=False, indent=2) + '\n', require_private=True)
            self._invalidate_settings()
            result = {'status': 'registered', 'camera': name, 'host': host, 'profile': profile,
                      'identity': snapshot['identity'], 'recorder_updated': True, 'backup_path': str(backup),
                      'detail': '카메라 접속 정보 한 대를 추가했습니다. 카메라 설정과 녹화 실행 상태는 변경하지 않았습니다.'}
            try:
                _ca_private_write(backup / 'result.json', _ca_json.dumps(result, ensure_ascii=False, indent=2).encode())
            except OSError:
                result['detail'] += ' 결과 파일 저장에 실패했습니다.'
            return result

    def _handover(self, client, before, desired):
        """Send once to the pinned old address; report only what that device acknowledged."""
        result = {'previous_host': client.host, 'expected_mac': before['identity']['ConnectedMACAddress'],
                  'camera_change_verified': False, 'device_acknowledged': None, 'write_attempted': False,
                  'status': 'unverified', 'detail': '변경 요청이 전달됐는지 확인할 수 없습니다. 새 대역에서 확인하세요.'}
        if not client.challenge:
            raise CameraAdminError('현재 IP에서 관리자 인증을 다시 확인한 뒤 적용하세요.')
        client.allow_writes = True
        try:
            acknowledgement = client.update_network(desired, before['interface_name'])
            if isinstance(acknowledgement, str) and acknowledgement.strip() == 'OK':
                result.update(status='accepted_unverified', device_acknowledged=True,
                              detail='카메라가 IP 변경 요청에 OK를 보냈습니다. 새 IP 접속은 새 대역에서 확인하세요.')
        except (_CameraAdminAuthError, _CameraAdminWriteRejected) as exc:
            result.update(status='rejected', device_acknowledged=False, detail=str(exc))
        except CameraAdminError:
            # A device may drop the connection while assigning its new address.
            pass
        finally:
            client.allow_writes = False
            result['write_attempted'] = client.write_attempted
        if not result['write_attempted']:
            result['detail'] = '기존 IP의 카메라에 변경 요청을 보내지 못했습니다. 기존 주소에서 다시 조회하세요.'
        return result

    @staticmethod
    def _store_result(backup, result):
        try:
            _ca_private_write(backup / 'result.json', _ca_json.dumps(result, ensure_ascii=False, indent=2).encode())
        except OSError:
            result['detail'] += ' 결과 파일 저장에도 실패했습니다.'
        return result

    def apply(self, payload):
        self._payload(payload)
        with self.controller.action_lock, self._lock, self._idle():
            if payload.get('confirm') is not True:
                raise CameraAdminError('변경할 카메라와 내용을 확인한 후 적용하세요.')
            if not isinstance(payload.get('plan_id'), str):
                raise CameraAdminError('변경 미리보기를 먼저 확인하세요.')
            entry = self._plans.pop(payload.get('plan_id'), None)
            if entry is None or _ca_time.monotonic() - entry['created'] >= 300:
                raise CameraAdminError('변경 미리보기가 만료되었거나 사용되었습니다. 다시 조회하세요.')
            client = entry['client']
            if entry.get('standalone_url'):
                _, revision = self.controller.read_config()
                index = None
                url = entry['standalone_url']
            else:
                data, revision, index, camera = self._camera(entry['camera'])
                url = camera['url']
            if revision != entry['revision'] or _ca_hashlib.sha256(url.encode()).hexdigest() != entry['url_hash']:
                raise CameraAdminError('녹화 설정이 변경되었습니다. 다시 미리보기를 만드세요.')
            before = self._observe(client, expected_mac=entry['snapshot']['identity']['ConnectedMACAddress'])
            if before != entry['snapshot']:
                raise CameraAdminError('카메라 식별정보 또는 설정이 변경되었습니다. 다시 조회하세요.')
            deferred = entry.get('defer_reconnect', False)
            check = dict(camera=entry['camera'], action=entry['action'], defer_reconnect=deferred)
            if entry['action'] == 'network':
                check.update(entry['desired'])
                new_url = _ca_url(url, host=entry['desired']['ip'])
            else:
                if entry['action'] == 'username':
                    password = client.password
                else:
                    password = payload.get('new_password')
                    if not isinstance(password, str) or _ca_hashlib.sha256(password.encode()).hexdigest() != entry['password_hash']:
                        raise CameraAdminError('미리보기와 동일한 새 비밀번호를 입력하세요.')
                check.update(new_password=password, confirm_password=payload.get('confirm_password'),
                             new_username=entry['desired']['username'])
                new_url = _ca_url(url, username=entry['desired']['username'], password=password)
            self._desired(check, before, client)
            if index is not None:
                self._configured_ip(data, index, entry['action'], entry['desired'])
            if index is not None and not deferred:
                proposed = _ca_json.loads(_ca_json.dumps(data))
                proposed['cameras'][index]['url'] = new_url
                settings_from_data(proposed, _ca_Path(self.controller.config_path).parent)
            backup = self._backup(entry)
            changed_verified, write_attempted = False, False
            result = {'camera': entry['camera'], 'host': _ca_urlsplit(new_url).hostname,
                      'recorder_updated': False, 'backup_path': str(backup)}
            if deferred:
                result.update(self._handover(client, before, entry['desired']))
                return self._store_result(backup, result)
            try:
                client.allow_writes = True
                write_attempted = True
                try:
                    if entry['action'] == 'network':
                        client.update_network(entry['desired'], before['interface_name'])
                    elif entry['action'] == 'username':
                        client.update_username(before, entry['desired']['username'])
                    else:
                        client.update_password(before, entry['desired']['username'], password)
                except (_CameraAdminAuthError, _CameraAdminWriteRejected) as exc:
                    result.update(status='rejected', device_acknowledged=False, camera_change_verified=False,
                                  write_attempted=getattr(client, 'write_attempted', True), detail=str(exc))
                    return self._store_result(backup, result)
                except CameraAdminError:
                    # An IP/password update may end its own HTTPS connection.
                    # Verify once at the intended address; never repeat a write.
                    pass
                finally:
                    client.allow_writes = False
                    write_attempted = getattr(client, 'write_attempted', write_attempted)
                verify = self.client_factory(new_url)
                verify.certificate = before['certificate_sha256']
                after = self._observe(verify, expected_mac=before['identity']['ConnectedMACAddress'])
                if after['identity'] != before['identity'] or after['certificate_sha256'] != before['certificate_sha256']:
                    raise CameraAdminError('변경 후 카메라 식별정보가 일치하지 않습니다.')
                expected_network = entry['desired'] if entry['action'] == 'network' else before['network']
                expected_user = dict(before['user']['fields'])
                if entry['action'] in ('username', 'password'):
                    expected_user['UserID'] = entry['desired']['username']
                if (after['network'] != expected_network or after['user']['index'] != before['user']['index']
                        or after['user']['fields'] != expected_user):
                    raise CameraAdminError('변경 후 설정이나 계정 권한을 확인하지 못했습니다.')
                changed_verified = True
                if index is None:
                    result.update(status='applied', detail='카메라 변경과 새 접속 정보로 재접속을 확인했습니다.')
                else:
                    _, current_revision = self.controller.read_config()
                    if current_revision != revision:
                        raise CameraAdminError('카메라 변경 중 녹화 설정이 다른 곳에서 수정되었습니다.')
                    atomic_private_text(self.controller.config_path, _ca_json.dumps(proposed, ensure_ascii=False, indent=2) + '\n', require_private=True)
                    self._invalidate_settings()
                    result.update(status='applied', recorder_updated=True, detail='카메라 변경과 재접속을 확인하고 이 ED의 녹화 주소를 갱신했습니다.')
            except Exception:
                result.update(status='camera_changed_local_pending' if changed_verified else 'unverified',
                    detail=('카메라 변경과 새 계정·주소 접속은 확인했지만 녹화 설정 갱신에 실패했습니다. 이 ED의 접속 정보를 직접 수정하세요.'
                            if changed_verified else
                            '변경 요청 이후 상태를 확인하지 못했습니다. 카메라가 변경됐을 수 있습니다. 자동 재시도·자동 원복하지 않았습니다.'
                            if write_attempted else '카메라에 변경 요청을 보내지 못했습니다. 새 정보를 다시 확인하세요. 자동 재시도하지 않았습니다.'))
            result['camera_change_verified'] = changed_verified
            result['write_attempted'] = write_attempted
            return self._store_result(backup, result)

    def discard_plan(self, token):
        with self._lock:
            self._plans.pop(token, None)

    def close(self):
        with self._lock:
            self._closed = True
            self._plans.clear()
# END EMBEDDED CAMERA_ADMIN

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


class PortCameraWorkflow:
    """Bind a shared factory address to a freshly identified switch port for each operation."""
    actions = {'identify', 'account', 'live', 'activation-plan', 'activation-apply', 'ip-plan', 'ip-apply'}

    def __init__(self, controller):
        self.controller = controller
        self.bindings = {}

    @staticmethod
    def selection(data):
        port, peers = data.get('port'), data.get('peer_ports')
        if (data.get('isolated') is not True or type(port) is not int or not 1 <= port <= 8
                or not isinstance(peers, list) or len(peers) > 7
                or any(type(p) is not int or not 1 <= p <= 8 or p == port for p in peers)
                or len(peers) != len(set(peers))):
            raise ValueError('대상 LAN 포트와 같은 IP의 다른 포트를 선택하고 분리 상태를 확인하세요.')
        return port, tuple(sorted(peers)), _cai_ip(data.get('ip'))

    @staticmethod
    def observation(snapshot, port, peers, ip):
        ed = snapshot.get('ed_ports', [])
        if not snapshot.get('supported') or len(ed) != 1 or ed[0] not in range(1, 9):
            raise ValueError('지원 허브와 ED의 연결 포트를 확인할 수 없습니다. 허브를 다시 조회하세요.')
        ports = {row['port']: row for row in snapshot['ports']}
        allowed = snapshot['connection']['camera_ports']
        for number in (port, *peers):
            row = ports.get(number)
            if number == ed[0] or number not in allowed or not row or row.get('mode') != 'access':
                raise ValueError(f'{number}번은 등록된 카메라 Access 포트로 확인되지 않았습니다.')
        target = ports[port]
        if not snapshot.get('can_modify') or not target.get('editable'):
            reasons = list(target.get('reasons', [])) + list(snapshot.get('warnings', []))
            raise ValueError('허브와 카메라 포트 설정을 확인하세요. ' + (' / '.join(reasons) or '허브 상태 조회 후 장비 MAC을 등록하세요.'))
        if target.get('admin_up') is not True or target.get('link') != 'up':
            raise ValueError(f'{port}번 포트를 켜고 링크가 연결된 후 다시 확인하세요.')
        for number in peers:
            row = ports[number]
            if row.get('admin_up') is not False and row.get('link') != 'down':
                raise ValueError(f'같은 IP의 {number}번 포트가 아직 연결돼 있습니다. 해당 포트의 통신을 끈 뒤 다시 확인하세요.')
        matches = [m for m in snapshot.get('candidate_matches', []) if m['ip'] == ip]
        if len(matches) != 1 or matches[0].get('status') != 'matched' or not matches[0].get('probe_fresh'):
            reason = (matches[0].get('reason') if len(matches) == 1 else '') or '새 ARP 응답과 단일 포트의 MAC 학습 정보를 확인하세요.'
            raise ValueError(f'{ip}의 연결 포트를 확인할 수 없습니다. {reason}')
        observed = matches[0]
        if observed.get('direct_port') != port:
            raise ValueError(f"{ip}의 응답이 선택한 {port}번과 다른 포트에서 관측됐습니다. 같은 IP 장치의 연결을 확인하세요.")
        scope = (snapshot['switch_id'], snapshot['connection']['target'], _sm_mac(snapshot['identity']['cpu_mac']),
                 ed[0], port, target['pvid'], peers, ip, _cai_mac(observed['mac']), observed['source'], observed['interface'])
        return observed, scope

    def drop_plan(self, binding):
        plan = binding.pop('pending', None)
        if plan:
            manager = self.controller.camera_activation if plan['kind'] == 'activation' else self.controller.camera_admin
            manager.discard_plan(plan['inner'])

    def forget(self, key):
        binding = self.bindings.pop(key, None)
        if binding:
            self.drop_plan(binding)

    def run(self, action, data):
        if action not in self.actions or self.controller.closing:
            raise ValueError('요청한 포트 카메라 작업을 사용할 수 없습니다.')
        port, peers, ip = self.selection(data)
        now = time.monotonic()
        for key, row in list(self.bindings.items()):
            if now >= row['expires']:
                self.forget(key)
            elif row.get('pending') and now >= row['pending']['expires']:
                self.drop_plan(row)
        key = data.get('binding_id')
        binding = self.bindings.get(key) if isinstance(key, str) else None
        if action != 'identify' and (not binding or data.get('expected_cert_sha256') != binding['cert']
                or _cai_mac(data.get('expected_mac')) != binding['mac']):
            raise ValueError('포트 확인이 만료되었거나 선택이 달라졌습니다. 포트와 카메라 확인을 다시 누르세요.')
        switch_request = dict(switch_id=data.get('switch_id'), password=data.get('switch_password'), candidate_ips=[ip])
        with self.controller.switch_manager.observing_port(switch_request) as snapshot:
            obs, scope = self.observation(snapshot, port, peers, ip)
            if action != 'identify' and binding['scope'] != scope:
                self.forget(key)
                raise ValueError('허브·포트·카메라 MAC 또는 연결 경로가 바뀌었습니다. 포트를 다시 확인하세요.')
            pins = dict(ip=ip, source=obs['source'], expected_mac=_cai_mac(obs['mac']))
            if binding and action != 'identify':
                pins['expected_cert_sha256'] = binding['cert']
            status = self.controller.camera_activation.inspect(pins)
            if (status['ip'] != ip or _cai_mac(status['mac']) != pins['expected_mac']
                    or (status['source'], status['interface']) != (obs['source'], obs['interface'])
                    or not re.fullmatch(r'[0-9a-f]{64}', status.get('cert_sha256', ''))
                    or (action != 'identify' and status['cert_sha256'] != binding['cert'])):
                raise ValueError('카메라 MAC·인증서 또는 경로가 포트 조회 결과와 다릅니다. 다시 확인하세요.')
            if action == 'identify':
                for old, row in list(self.bindings.items()):
                    if row['scope'][:5] == scope[:5]:
                        self.forget(old)
                if len(self.bindings) >= 32:
                    self.forget(next(iter(self.bindings)))
                key = secrets.token_urlsafe(24)
                self.bindings[key] = dict(scope=scope, cert=status['cert_sha256'], mac=pins['expected_mac'],
                                          profiles=(), previewed=False, expires=time.monotonic() + 1200)
                return dict(status, port=port, binding_id=key)
            if action.startswith('activation-'):
                if status['initialized']:
                    raise ValueError('이미 최초 비밀번호가 등록된 카메라입니다. 포트를 다시 확인하세요.')
            elif not status['initialized']:
                raise ValueError('영상을 열기 전에 카메라의 최초 관리자 비밀번호를 설정하세요.')
            target = dict(pins, username=data.get('username'), password=data.get('password'))
            if action == 'account':
                self.drop_plan(binding)
                binding.update(profiles=(), previewed=False)
                result = self.controller.camera_admin.inspect_candidate(target)
                binding['profiles'] = tuple(str(p['id']) for p in result.get('profiles', [])
                    if re.sub(r'[^A-Z0-9]', '', str(p.get('EncodingType', '')).upper()) == 'H264')
                return result
            if action == 'live':
                profile = str(data.get('profile', '2'))
                if profile not in binding['profiles']:
                    raise ValueError('계정 확인 후 카메라에서 제공하는 H.264 프로필을 선택하세요.')
                result = self.controller.live_view.start_port(obs, target['username'], target['password'], profile)
                binding['previewed'] = True
                return result
            kind, stage = action.split('-')
            field = 'token' if kind == 'activation' else 'plan_id'
            if stage == 'plan':
                self.drop_plan(binding)
                if kind == 'activation':
                    result = self.controller.activation_action('preview', dict(pins, isolated=True))
                else:
                    if not binding['previewed']:
                        raise ValueError('선택한 포트의 실시간 영상을 먼저 확인하세요.')
                    result = self.controller.camera_admin.plan(dict(
                        action='network', isolated=True, direct_target=target, expected_host=ip,
                        expected_mac=pins['expected_mac'], expected_cert_sha256=binding['cert'],
                        ip=data.get('new_ip'), netmask=data.get('netmask'), gateway=data.get('gateway') or '0.0.0.0',
                        defer_reconnect=data.get('defer_reconnect', False)))
                # The underlying general-purpose apply endpoints must not accept a port workflow's public token.
                outer = secrets.token_urlsafe(32)
                binding['pending'] = dict(kind=kind, inner=result[field], outer=outer, expires=time.monotonic() + 300)
                return {**result, field: outer}
            plan = binding.get('pending')
            if data.get('confirm') is not True or not plan or plan['kind'] != kind or data.get(field) != plan['outer']:
                raise ValueError('선택한 포트의 변경 내용을 다시 검토하고 적용을 확인하세요.')
            try:
                if kind == 'activation':
                    return self.controller.activation_action('apply', dict(token=plan['inner'], new_password=data.get('new_password'),
                                                                           confirm_password=data.get('confirm_password')))
                return self.controller.camera_admin.apply(dict(plan_id=plan['inner'], confirm=True))
            finally:
                self.forget(key)


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
        self.live_view = LiveViewManager(self.settings, lambda: live_view_port_settings(self.read_config()[0], self.config_path.parent))
        self.camera_snapshots = CameraSnapshotManager(self.settings, self.config_path)
        self.port_observations = {}
        self.port_observation_lock = threading.Lock()
        self.port_observation_epoch = 0
        self.switch_manager = SwitchManager(self.config_path.parent / "switches.json", cameras_provider=self._switch_cameras)
        self.camera_profiles = CameraProfileManager(self)
        self.camera_admin = CameraAdminManager(self)
        self.camera_activation = CameraActivationManager(self.config_path)
        self.port_cameras = PortCameraWorkflow(self)
        self.camera_ping = CameraPingMonitor(self.settings, self.config_path)
        self.shared_browser_server = None

    def _switch_cameras(self):
        data, _ = self.read_config()
        if camera_setup_required(data):
            return []
        return [{"id": c.name, "name": c.name, "ip": urlsplit(c.url).hostname or ""}
                for c in self.settings().cameras]

    def switch_operation(self, operation, data):
        # Starting any new read supersedes this switch's previous observation,
        # even when the new query fails. Never cache a payload or a password.
        switch_id = data.get("switch_id") if operation in ("inspect", "setup_network") else data.get("id")
        with self.port_observation_lock:
            self.port_observation_epoch += 1
            epoch = self.port_observation_epoch
            if operation == "apply":
                self.port_observations.clear()
            elif isinstance(switch_id, str):
                self.port_observations.pop(switch_id, None)
        started = time.monotonic()
        observed_at = datetime.now(KST).isoformat(timespec="seconds")
        result = getattr(self.switch_manager, operation)(data)
        if operation != "inspect" or not result.get("supported"):
            return result
        identity = result.get("identity", {}).get("cpu_mac")
        if not identity:
            return result
        devices = []
        for match in result.get("candidate_matches", []):
            if match.get("status") == "matched" and match.get("probe_fresh") and match.get("direct_port"):
                devices.append({key: match[key] for key in ("ip", "mac", "source", "interface", "direct_port")})
        if devices:
            with self.port_observation_lock:
                if epoch == self.port_observation_epoch:
                    self.port_observations[switch_id] = dict(connection=dict(result["connection"]),
                        identity=identity, route=dict(result["route"]), devices=devices,
                        observed_at=observed_at, expires=started + 120)
        return result

    def camera_port_status(self, ip, mac):
        ip, mac = _sm_ip(ip), _sm_mac(mac)
        config = self.switch_manager.get_config(include_network=True)
        health = {row["switch_id"]: row for row in config["connection_health"]}
        switches, matches = [], []
        with self.port_observation_lock:
            for connection in config["switches"]:
                switch_id = connection["id"]
                state = health[switch_id]
                switches.append(dict(id=switch_id, name=connection["name"], target=connection["target"],
                                     ready=state["route_ready"], reason=state["route_error"]))
                obs = self.port_observations.get(switch_id)
                if not obs:
                    continue
                remaining = int(obs["expires"] - time.monotonic())
                if (remaining <= 0 or not state["route_ready"] or connection != obs["connection"]
                        or state.get("route") != obs["route"]
                        or connection.get("expected_cpu_mac") not in (None, "", obs["identity"])):
                    self.port_observations.pop(switch_id, None)
                    continue
                for device in obs["devices"]:
                    if device["ip"] != ip or device["mac"] != mac:
                        continue
                    try:
                        route = _sm_ip_json("-4", "route", "get", ip)
                    except SwitchManagerError:
                        route = []
                    if (len(route) != 1 or route[0].get("gateway") or route[0].get("dev") != device["interface"]
                            or route[0].get("prefsrc") != device["source"]):
                        self.port_observations.pop(switch_id, None)
                        break
                    matches.append(dict(switch_id=switch_id, name=connection["name"], target=connection["target"],
                                        port=device["direct_port"], observed_at=obs["observed_at"], expires_in=remaining))
        return {"switches": switches, "matches": matches}

    def port_camera_action(self, action, data):
        with self.action_lock:
            return self.port_cameras.run(action, data)

    def activation_action(self, action, data):
        if action not in ("inspect", "preview", "apply") or not isinstance(data, dict):
            raise ValueError("카메라 초기 등록 요청 형식이 올바르지 않습니다.")
        with self.action_lock:
            if self.closing:
                raise ValueError("프로그램을 종료하고 있습니다.")
            if action in ("preview", "apply"):
                with self.camera_admin._idle():
                    return getattr(self.camera_activation, action)(data)
            return getattr(self.camera_activation, action)(data)

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
        self.camera_admin.close()
        self.camera_activation.close()
        with self.action_lock:
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
                elif path == "/api/camera-port":
                    self._json(controller.camera_port_status(self._query_value(query, "ip"), self._query_value(query, "mac")))
                elif path == "/api/switch/config":
                    self._json(controller.switch_manager.get_config(include_network=True))
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
                    with controller.action_lock:
                        result = controller.live_view.start(data.get("camera"), data.get("profile", "current"))
                elif self.path == "/api/live/stop":
                    result = controller.live_view.stop(data.get("session"))
                elif self.path == "/api/snapshots/capture":
                    with controller.action_lock:
                        result = controller.camera_snapshots.capture(data.get("camera"), data.get("profile", "current"))
                elif self.path == "/api/camera-admin/inspect":
                    result = controller.camera_admin.inspect(data)
                elif self.path.startswith("/api/port-camera/"):
                    result = controller.port_camera_action(self.path[len("/api/port-camera/"):], data)
                elif self.path == "/api/camera-admin/inspect-candidate":
                    result = controller.camera_admin.inspect_candidate(data)
                elif self.path == "/api/camera-admin/plan":
                    result = controller.camera_admin.plan(data)
                elif self.path == "/api/camera-admin/apply":
                    result = controller.camera_admin.apply(data)
                elif self.path == "/api/camera-admin/register":
                    result = controller.camera_admin.register(data)
                elif self.path == "/api/camera-activation/inspect":
                    result = controller.activation_action("inspect", data)
                elif self.path == "/api/camera-activation/plan":
                    result = controller.activation_action("preview", data)
                elif self.path == "/api/camera-activation/apply":
                    result = controller.activation_action("apply", data)
                elif self.path == "/api/switch/config":
                    result = controller.switch_operation("save_connection", data)
                elif self.path == "/api/switch/inspect":
                    result = controller.switch_operation("inspect", data)
                elif self.path == "/api/switch/network-setup":
                    result = controller.switch_operation("setup_network", data)
                elif self.path == "/api/switch/plan":
                    result = controller.switch_manager.plan(data)
                elif self.path == "/api/switch/apply":
                    result = controller.switch_operation("apply", data)
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
