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


def _sm_camera_neighbors(cameras, parent):
    """Read routes and existing neighbor cache only: no ping, ARP or IP changes."""
    links = _sm_ip_json('-d', 'link', 'show')
    cache, result = {}, []
    for camera in cameras:
        item = dict(camera, interface=None, neighbors=[], route_error='')
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
            if route.get('gateway') or route.get('type') in ('local', 'unreachable', 'blackhole', 'prohibit') or _sm_link_parent(interface, links) != parent:
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
        match.update(mac=None, mac_candidates=[], neighbor_state=[], ports=[], direct_port=None,
                     status='unresolved', reason='', fdb_entries=[])
        neighbors = camera.get('neighbors', [])
        match['neighbor_state'] = sorted({state for row in neighbors for state in row['state']})
        known_states = {'REACHABLE', 'STALE', 'DELAY', 'PROBE', 'PERMANENT', 'NOARP'}
        valid = [row for row in neighbors if row['mac'] and row['state'] and set(row['state']).issubset(known_states)]
        macs = sorted({row['mac'] for row in valid})
        match['mac_candidates'] = macs
        if len(macs) == 1:
            match['mac'] = macs[0]
            match['fdb_entries'] = [row for row in fdb if row['mac'] == macs[0]]
            match['ports'] = sorted({row['port'] for row in match['fdb_entries']})
        if camera.get('route_error'):
            match.update(status='off_network', reason=camera['route_error'])
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
            port = by_port[match['ports'][0]]
            if port['port'] >= 9 or port['mode'] != 'access' or port['port'] in ed_ports:
                match.update(status='uplink', reason='광·업링크·ED 경로에서 학습된 MAC입니다. 이 포트를 카메라 직결 포트로 판단하지 않습니다.')
            elif len(port.get('macs', [])) != 1:
                match.update(status='ambiguous', reason='해당 포트에서 여러 MAC이 학습되었습니다. 하위 허브 또는 공유 연결일 수 있습니다.')
            elif port['link'] != 'up' or port['admin_up'] is not True:
                match['reason'] = '학습 기록은 있지만 현재 포트 링크 또는 사용 상태가 켜짐으로 확인되지 않습니다.'
            elif len({row['vlan'] for row in match['fdb_entries']}) != 1 or match['fdb_entries'][0]['vlan'] != port['pvid']:
                match.update(status='ambiguous', reason='MAC 학습 VLAN과 포트 VLAN이 단일 값으로 일치하지 않습니다.')
            else:
                recent = bool(valid) and all(row['state'] == ['REACHABLE'] for row in valid)
                match.update(direct_port=port['port'], status='matched' if recent else 'cached',
                             reason=('ARP 캐시가 REACHABLE이고 같은 MAC이 단일 Access 포트에서 학습되었습니다.' if recent else
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

    def _map_cameras(self, snapshot, raw):
        snapshot['camera_matches'] = []
        snapshot['camera_mapping_warnings'] = []
        snapshot['camera_mapping_note'] = ('기존 ARP 캐시와 현재 허브 MAC 학습표를 대조한 결과입니다. '
                                           '새 핑·ARP 검사는 하지 않으며, IP 중복 부재나 카메라의 물리적 직결을 보장하지 않습니다. '
                                           '카메라 포트 등록·제어 대상은 자동 변경하지 않습니다.')
        if self._cameras_provider is None:
            return
        try:
            registered = self._cameras_provider()
            if not isinstance(registered, (list, tuple)) or len(registered) > 256:
                raise SwitchManagerError('카메라 목록은 최대 256개의 IP 항목이어야 합니다.')
            cameras = []
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
            fdb = _sm_fdb_entries(raw['show mac-address-table'])
            matches = _sm_camera_matches(observations, fdb, snapshot['ports'], snapshot['ed_ports'])
            snapshot['camera_matches'] = matches
            for match in matches:
                if match['direct_port'] is not None:
                    port = next(row for row in snapshot['ports'] if row['port'] == match['direct_port'])
                    port['cameras'].append({key: match[key] for key in ('id', 'name', 'ip', 'status', 'reason')})
        except Exception:
            # Mapping is supplementary evidence. A camera settings error must not
            # conceal switch state or leak RTSP credential-bearing exceptions.
            snapshot['camera_mapping_warnings'].append('카메라 목록 또는 이웃 캐시를 읽지 못해 카메라-포트 대조를 완료하지 못했습니다.')

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
                  'vlans': [], 'ed_ports': [], 'can_modify': False, 'warnings': [],
                  'camera_matches': [], 'camera_mapping_warnings': []}
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
                                    'editable': not reasons, 'mac_count': len(macs), 'macs': sorted(macs), 'cameras': []})
        if not connection['expected_cpu_mac']:
            result['warnings'].append('CPU MAC을 저장하기 전에는 설정을 변경할 수 없습니다.')
        if not ed_known:
            result['warnings'].append('ED 포트가 명확하지 않아 모든 설정 변경을 차단했습니다.')
        if _sm_comparable(raw['show running-config']) != _sm_comparable(raw['show startup-config']):
            result['warnings'].append('실행 설정과 저장 설정이 다릅니다. 기존 미저장 변경을 먼저 확인해야 합니다.')
        if 'not configured' not in raw['show reload'].lower():
            result['warnings'].append('기존 재부팅 예약 상태를 확인해야 합니다. 이 도구는 재부팅을 예약하거나 취소하지 않습니다.')
        result['can_modify'] = bool(connection['expected_cpu_mac'] and ed_known and not result['warnings'])
        self._map_cameras(result, raw)
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
