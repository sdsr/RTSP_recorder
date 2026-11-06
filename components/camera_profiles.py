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
