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
        if method == 'POST' and (not isinstance(body, bytes) or not 1 <= len(body) <= 16384):
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
            if status != 200 or len(raw) > 4 * 1024 * 1024:
                raise CameraAdminError('카메라 관리 요청이 거부되었거나 응답이 너무 큽니다.')
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeError:
                raise CameraAdminError('카메라 응답을 읽을 수 없습니다.') from None
            if pair in writes and _ca_re.search(r'(?:^|\n)\s*(?:Error|ErrorCode)\s*=|NG\s*\[', text, _ca_re.I):
                raise CameraAdminError('카메라가 변경 요청을 거부했습니다.')
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

    def update_network(self, desired, interface_name):
        return self.get('/stw-cgi/network.cgi', dict(msubmenu='interface', action='set',
            InterfaceName=interface_name,
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
            'can_change_username': features.get('AdminIDChangeable') == 'True',
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

    def _observe(self, client):
        route = self._route(client.host)
        probe = self._probe(client.host, route)
        snapshot = self._snapshot(client)
        if probe['mac'].lower() != snapshot['identity']['ConnectedMACAddress'].lower():
            raise CameraAdminError('ARP 응답과 카메라 장치 MAC이 다릅니다. 같은 IP의 중복 장치를 확인하세요.')
        return snapshot

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
            route = self._route(str(ip))
            if _ca_ipaddress.IPv4Address(route['source']) not in network or route['source'] == str(ip):
                raise CameraAdminError('ED의 enp2 소스 IP가 새 카메라 대역에 없거나 카메라 IP와 같습니다.')
            old_route = self._route(client.host)
            if route['parent'] != old_route['parent'] or route['dev'] != old_route['dev']:
                raise CameraAdminError('변경 전후 카메라는 같은 enp2 인터페이스로 접근해야 합니다.')
            if str(ip) != client.host:
                self._available(str(ip), route)
            desired = {'ip': str(ip), 'netmask': str(network.netmask), 'gateway': str(gateway), 'mode': 'Manual'}
            if desired == snapshot['network']:
                raise CameraAdminError('변경할 네트워크 설정이 없습니다.')
            return desired
        if action == 'password':
            caps = snapshot['capabilities']
            if not caps['password']:
                raise CameraAdminError('이 카메라의 계정 변경 API를 확인하지 못했습니다.')
            username = payload.get('new_username') or client.username
            if not isinstance(username, str) or not _ca_re.fullmatch(r'[A-Za-z0-9_.-]{1,8}', username):
                raise CameraAdminError('관리 아이디는 영문·숫자·밑줄·마침표·하이픈 1~8자로 입력하세요.')
            if username != client.username and not caps['can_change_username']:
                raise CameraAdminError('이 카메라는 관리 아이디 변경을 지원하지 않습니다.')
            _camera_admin_password(payload.get('new_password'), payload.get('confirm_password'), username=username,
                                   maximum=min(64, caps['password_maximum']), tta=caps.get('new_password_policy', False))
            if payload['new_password'] == client.password and username == client.username:
                raise CameraAdminError('변경할 아이디·비밀번호가 없습니다.')
            return {'username': username, 'password_changed': payload['new_password'] != client.password}
        raise CameraAdminError('네트워크 또는 계정 변경을 선택하세요.')

    def plan(self, payload):
        self._payload(payload)
        with self.controller.action_lock, self._lock:
            data, revision, index, camera = self._camera(payload.get('camera'))
            client = self.client_factory(camera['url'])
            before = self._observe(client)
            self._expected(payload, client, before)
            desired = self._desired(payload, before, client)
            self._configured_ip(data, index, payload['action'], desired)
            token = _ca_secrets.token_urlsafe(32)
            now = _ca_time.monotonic()
            self._plans = {key: row for key, row in self._plans.items() if now - row['created'] < 300}
            if len(self._plans) >= 16:
                raise CameraAdminError('변경 미리보기가 너무 많습니다. 잠시 후 다시 시도하세요.')
            action = payload['action']
            public = {'plan_id': token, 'camera': camera['name'], 'action': action, 'expires_seconds': 300,
                'camera_host': client.host, 'identity': before['identity'],
                'before': before['network'] if action == 'network' else {'username': client.username},
                'after': desired, 'warnings': ['적용 전에 예약 대기를 포함한 녹화와 실시간 재생을 중지하세요.',
                    '센터·NVR·다른 ED의 접속 정보는 자동으로 변경하지 않습니다.']}
            self._plans[token] = {'created': now, 'camera': camera['name'], 'revision': revision,
                'url_hash': _ca_hashlib.sha256(camera['url'].encode()).hexdigest(), 'snapshot': before,
                'client': client, 'action': action, 'desired': desired, 'public': public,
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
            snapshot = self._observe(client)
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
            data, revision, index, camera = self._camera(entry['camera'])
            if revision != entry['revision'] or _ca_hashlib.sha256(camera['url'].encode()).hexdigest() != entry['url_hash']:
                raise CameraAdminError('녹화 설정이 변경되었습니다. 다시 미리보기를 만드세요.')
            client = entry['client']
            before = self._observe(client)
            if before != entry['snapshot']:
                raise CameraAdminError('카메라 식별정보 또는 설정이 변경되었습니다. 다시 조회하세요.')
            check = dict(camera=entry['camera'], action=entry['action'])
            if entry['action'] == 'network':
                check.update(entry['desired'])
                new_url = _ca_url(camera['url'], host=entry['desired']['ip'])
            else:
                password = payload.get('new_password')
                if not isinstance(password, str) or _ca_hashlib.sha256(password.encode()).hexdigest() != entry['password_hash']:
                    raise CameraAdminError('미리보기와 동일한 새 비밀번호를 입력하세요.')
                check.update(new_password=password, confirm_password=payload.get('confirm_password'),
                             new_username=entry['desired']['username'])
                new_url = _ca_url(camera['url'], username=entry['desired']['username'], password=password)
            self._desired(check, before, client)
            self._configured_ip(data, index, entry['action'], entry['desired'])
            proposed = _ca_json.loads(_ca_json.dumps(data))
            proposed['cameras'][index]['url'] = new_url
            settings_from_data(proposed, _ca_Path(self.controller.config_path).parent)
            backup = self._backup(entry)
            changed_verified, write_attempted = False, False
            result = {'camera': entry['camera'], 'host': _ca_urlsplit(new_url).hostname,
                      'recorder_updated': False, 'backup_path': str(backup)}
            try:
                client.allow_writes = True
                write_attempted = True
                try:
                    if entry['action'] == 'network':
                        client.update_network(entry['desired'], before['interface_name'])
                    else:
                        client.update_password(before, entry['desired']['username'], payload['new_password'])
                except _CameraAdminAuthError:
                    raise
                except CameraAdminError:
                    # An IP/password update may end its own HTTPS connection.
                    # Verify once at the intended address; never repeat a write.
                    pass
                finally:
                    client.allow_writes = False
                    write_attempted = getattr(client, 'write_attempted', write_attempted)
                verify = self.client_factory(new_url)
                verify.certificate = before['certificate_sha256']
                after = self._observe(verify)
                if after['identity'] != before['identity'] or after['certificate_sha256'] != before['certificate_sha256']:
                    raise CameraAdminError('변경 후 카메라 식별정보가 일치하지 않습니다.')
                expected_network = entry['desired'] if entry['action'] == 'network' else before['network']
                expected_user = dict(before['user']['fields'])
                if entry['action'] == 'password':
                    expected_user['UserID'] = entry['desired']['username']
                if (after['network'] != expected_network or after['user']['index'] != before['user']['index']
                        or after['user']['fields'] != expected_user):
                    raise CameraAdminError('변경 후 설정이나 계정 권한을 확인하지 못했습니다.')
                changed_verified = True
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
            try:
                _ca_private_write(backup / 'result.json', _ca_json.dumps(result, ensure_ascii=False, indent=2).encode())
            except OSError:
                result['detail'] += ' 결과 파일 저장에도 실패했습니다.'
            return result

    def close(self):
        with self._lock:
            self._closed = True
            self._plans.clear()
