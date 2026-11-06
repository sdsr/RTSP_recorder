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
