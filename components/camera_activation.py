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
