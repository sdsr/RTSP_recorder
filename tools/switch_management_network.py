#!/usr/bin/env python3
"""Set up a single switch management VLAN on a recorder ED through NetworkManager."""
import argparse
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid


class SetupError(ValueError):
    pass


def run(argv, timeout=20):
    try:
        cmd = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                             env=dict(os.environ, LC_ALL='C', LANG='C'))
    except FileNotFoundError:
        raise SetupError(f'{argv[0]} 프로그램을 찾지 못했습니다.') from None
    except subprocess.TimeoutExpired:
        raise SetupError(f'{argv[0]} 응답 시간이 초과됐습니다.') from None
    if cmd.returncode:
        detail = (cmd.stderr.strip() or cmd.stdout.strip())[-600:]
        if argv[0] == 'nmcli' and re.search(r'not authori[sz]ed|unauthori[sz]ed|permission denied|insufficient privileges|not permitted|authorization|authentication|access.?denied|no agents? (?:were |was )?available', detail, re.I):
            raise SetupError('NetworkManager 관리자 승인이 필요합니다. ED 화면에서 권한을 승인한 뒤 다시 시도하세요.')
        if os.path.basename(argv[0]) == 'arping' and re.search(r'permi(?:ssion|tted)|cap_net_raw', detail, re.I):
            raise SetupError('허브의 MAC을 확인하는 arping에 원시 패킷 권한이 필요합니다.')
        raise SetupError(f'{argv[0]} 실행 실패. {detail}')
    return cmd.stdout


def nm(*args):
    return run(['nmcli', '--colors', 'no', '--escape', 'no', '--wait', '45', *args], timeout=55)


def ip(*args):
    return json.loads(run(['ip', '-j', *args]))


def mac(value):
    value = str(value).lower().replace('-', ':')
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', value) or int(value[:2], 16) & 1 or value == '00:00:00:00:00:00':
        raise SetupError('유효한 장치 MAC을 입력하세요.')
    return value


def host(value):
    try:
        addr = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise SetupError('허브와 ED에는 IPv4 주소를 입력하세요.') from None
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        raise SetupError('허브와 ED에는 장치에 할당할 수 있는 IPv4 주소를 입력하세요.')
    return str(addr)


def endpoint_mac(interface, source, target):
    binary = shutil.which('arping', path='/usr/sbin:/usr/bin:/sbin:/bin')
    if not binary:
        raise SetupError('관리 IP 확인에 필요한 arping이 설치되어 있지 않습니다.')
    env = dict(os.environ, LC_ALL='C', LANG='C')
    help_cmd = subprocess.run([binary, '--help'], capture_output=True, text=True, timeout=4, env=env)
    help_text = help_cmd.stdout + help_cmd.stderr
    if 'Thomas Habets' in help_text:
        flags = ['-d', '-t', 'ff:ff:ff:ff:ff:ff', '-i', interface, '-S', source]
        pattern = r'\bfrom\s+([0-9a-f:]{17})\s+\(' + re.escape(target) + r'\)'
    elif 'iputils' in help_text.lower() or re.search(r'-I\s+<?interface', help_text):
        flags = ['-b', '-I', interface, '-s', source]
        pattern = r'\breply from\s+' + re.escape(target) + r'\s+\[([0-9a-f:]{17})\]'
    else:
        raise SetupError('지원하는 arping 형식을 확인하지 못했습니다.')
    output = run([binary, *flags, '-c', '3', '-w', '5', target], timeout=8)
    replies = re.findall(pattern, output, re.I)
    if len(replies) < 2 or len(set(value.lower() for value in replies)) != 1:
        raise SetupError('허브에서 같은 관리 MAC으로 두 번 이상 응답하지 않았습니다. 배선과 관리 VLAN 번호를 확인하세요.')
    return mac(replies[0])


def validate_args(args):
    if not re.fullmatch(r'enp2[0-9A-Za-z]*', args.parent) or len(args.parent) > 15:
        raise SetupError('카메라용 enp2 물리 포트를 선택하세요.')
    if not 1 <= args.vlan <= 4094:
        raise SetupError('관리 VLAN 번호는 1~4094입니다.')
    args.parent_mac = mac(args.parent_mac)
    args.management_mac = mac(args.management_mac) if args.management_mac else ''
    args.source, args.target = host(args.source), host(args.target)
    if args.source == args.target:
        raise SetupError('ED 관리 IP와 허브 관리 IP는 서로 달라야 합니다.')


def check_links(args, interface, profile_uuid):
    links = ip('-d', 'link', 'show')
    parent = next((row for row in links if row['ifname'] == args.parent), {})
    if (parent.get('link_type') != 'ether' or parent.get('linkinfo', {}).get('info_kind')
            or 'LOWER_UP' not in parent.get('flags', []) or mac(parent.get('address')) != args.parent_mac):
        raise SetupError('선택한 enp2 물리 랜포트의 MAC과 링크 연결 상태를 확인하세요.')
    for link in links:
        name = link['ifname']
        data = link.get('linkinfo', {})
        same_vlan = (link.get('link', link.get('link_index')) in (args.parent, parent['ifindex'])
                     and data.get('info_kind') == 'vlan' and data.get('info_data', {}).get('id') == args.vlan)
        if name == interface:
            if not (same_vlan and profile_uuid):
                raise SetupError(f'다른 인터페이스에서 {name} 이름을 사용하고 있습니다.')
            # A saved profile can be inactive while somebody else's active
            # connection (or a manually created link) occupies the same VLAN.
            # Activating ours in that case could disconnect the other session.
            active_owner = nm('-g', 'GENERAL.CON-UUID', 'dev', 'show', interface).strip()
            if active_owner != profile_uuid:
                raise SetupError(f'{name}의 현재 연결이 등록된 관리 프로필과 일치하지 않습니다.')
        if same_vlan and name != interface:
            hint = '기존 임시 관리 터미널에서 Enter를 누른 뒤 다시 진행하세요.' if name == 'nxmg100' else '기존 관리 연결을 먼저 확인하세요.'
            raise SetupError(f'{name}에서 이미 같은 VLAN을 사용합니다. {hint}')
    for dev in ip('-4', 'address', 'show'):
        for addr in dev.get('addr_info', []):
            if addr['local'] == args.target or (addr['local'] == args.source and dev['ifname'] != interface):
                raise SetupError('허브 IP 또는 ED 관리 IP가 이 PC의 기존 주소와 겹칩니다.')
            network = ipaddress.ip_network((addr['local'], addr['prefixlen']), strict=False)
            if dev['ifname'] != interface and any(ipaddress.IPv4Address(value) in network for value in (args.target, args.source)):
                raise SetupError('관리 IP가 다른 인터페이스에서 사용하는 대역과 겹칩니다. 기존 직접 경로를 확인하세요.')
    for row in ip('-4', 'route', 'show', 'table', 'all'):
        dest = row.get('dst', 'default')
        if dest == 'default' or row.get('table') == 'local' or row.get('dev') == interface:
            continue
        if ipaddress.IPv4Address(args.target) in ipaddress.ip_network(dest, strict=False):
            raise SetupError('다른 인터페이스에 이미 이 허브 IP를 포함하는 경로가 있습니다.')
    dns = set(nm('-g', 'IP4.DNS', 'dev', 'show').splitlines())
    if dns.intersection((args.target, args.source)):
        raise SetupError('현재 PC가 DNS 서버로 쓰는 IP입니다. ED와 허브에 할당된 관리 주소를 확인하세요.')
    for row in ip('-4', 'route', 'show', 'default'):
        if row.get('dev') == args.parent or args.target == row.get('gateway') or args.source == row.get('gateway'):
            raise SetupError('선택한 포트 또는 주소를 기본 네트워크 경로에서 사용하고 있습니다.')


def settings_match(args, profile_uuid, interface, parent_uuid, name, autoconnect=True):
    fields = ('connection.id', 'connection.type', 'connection.interface-name', 'vlan.id', 'vlan.parent',
              '802-3-ethernet.mac-address', 'ipv4.method', 'ipv4.addresses', 'ipv4.gateway',
              'ipv4.never-default', 'ipv4.may-fail', 'ipv4.dad-timeout', 'ipv6.method',
              'connection.autoconnect', 'ipv4.routes')
    rows = nm('-g', ','.join(fields), 'con', 'show', 'uuid', profile_uuid).splitlines()
    expected = [name, 'vlan', interface, str(args.vlan), parent_uuid, args.parent_mac, 'manual',
                args.source + '/32', '', 'yes', 'no', '3000', 'disabled']
    if len(rows) != len(fields) or [x.lower() for x in rows[:len(expected)]] != expected:
        return False
    route_ips = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b', rows[-1])
    return rows[-2] == ('yes' if autoconnect else 'no') and route_ips in ([args.target + '/32'], [args.target + '/32', '0.0.0.0'])


def profile_is_persistent(profile_uuid):
    files = dict(line.split(':', 1) for line in nm('-g', 'UUID,FILENAME', 'con', 'show').splitlines())
    path = files.get(profile_uuid, '')
    if path.startswith('/etc/'):
        return True
    # Ubuntu with the netplan NM settings backend can render an /etc/netplan YAML
    # profile to /run/NetworkManager for activation.
    return path.startswith('/run/NetworkManager/') and os.path.isfile('/etc/netplan/90-NM-' + profile_uuid + '.yaml')


def verify_active(args, interface, profile_uuid):
    # On boot NM can report an active connection just before the local IPv4 source
    # and its host route are usable. Wait for both before sending any ARP requests.
    deadline = time.monotonic() + 6
    while True:
        try:
            active = nm('-g', 'GENERAL.CON-UUID', 'dev', 'show', interface).strip()
            if active and active not in (profile_uuid, '--'):
                raise SetupError('다른 프로필에서 관리 VLAN을 사용하고 있습니다.')
            routes = ip('-4', 'route', 'get', args.target, 'from', args.source) if active == profile_uuid else []
        except SetupError as exc:
            if 'Network is unreachable' not in str(exc) and 'Invalid argument' not in str(exc):
                raise
            routes = []
        if len(routes) == 1 and routes[0].get('dev') == interface and not routes[0].get('gateway'):
            break
        if time.monotonic() >= deadline:
            raise SetupError('허브 관리 IP로 향하는 직접 경로를 확인하지 못했습니다.')
        time.sleep(0.25)
    current = endpoint_mac(interface, args.source, args.target)
    if args.management_mac and current != args.management_mac:
        raise SetupError(f'허브 응답 MAC {current}이 지정한 관리 MAC과 다릅니다.')
    return current


def configure(args):
    validate_args(args)
    interface = 'edmg' + str(args.vlan)
    if args.parent != 'enp2s0f1':
        interface += '-' + uuid.uuid5(uuid.NAMESPACE_OID, args.parent_mac).hex[:5]
    name = 'rtsp-mgmt-' + args.parent + '-' + str(args.vlan)
    stable_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'rtsp-recorder:management:' + args.parent_mac + ':' + str(args.vlan)))
    info = {'interface': interface, 'parent': args.parent, 'vlan': args.vlan, 'source': args.source,
            'target': args.target, 'profile': name, 'stable_id': stable_id}
    # Kernel-released lock also serializes a terminal retry with an in-flight UI request.
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as lock:
        try:
            lock.bind('\0rtsp-recorder-net-' + stable_id)
        except OSError:
            raise SetupError('같은 관리 VLAN을 다른 창에서 설정 중입니다. 잠시 후 다시 조회하세요.') from None
        return _configure(args, info)


def _configure(args, info):
    interface = info['interface']
    name = info['profile']
    stable_id = info['stable_id']
    rows = nm('-g', 'UUID,NAME', 'con', 'show').splitlines()
    matches = [row.split(':', 1)[0] for row in rows if row.partition(':')[2] == name]
    if len(matches) > 1:
        raise SetupError('같은 이름의 NetworkManager 프로필이 여러 개 있어 구분할 수 없습니다.')
    profile_uuid = matches[0] if matches else ''
    existing = bool(profile_uuid)
    if existing and nm('-g', 'connection.stable-id', 'con', 'show', 'uuid', profile_uuid).strip() != stable_id:
        raise SetupError('같은 이름의 다른 NetworkManager 연결이 이미 있습니다.')
    check_links(args, interface, profile_uuid)
    parent_uuid = nm('-g', 'GENERAL.CON-UUID', 'dev', 'show', args.parent).strip()
    try:
        uuid.UUID(parent_uuid)
    except ValueError:
        raise SetupError('enp2 포트가 NetworkManager에 연결되어 있어야 합니다.') from None
    permanent = existing and profile_is_persistent(profile_uuid)
    if existing and not settings_match(args, profile_uuid, interface, parent_uuid, name, autoconnect=permanent):
        raise SetupError('같은 VLAN의 기존 NetworkManager 프로필 설정이 입력값과 다릅니다. 기존 프로필을 확인하세요.')
    active = profile_uuid in nm('-g', 'UUID', 'con', 'show', '--active').splitlines()
    if permanent and active:
        return dict(info, status='ready', profile_uuid=profile_uuid, management_mac=verify_active(args, interface, profile_uuid))
    if not args.apply:
        return dict(info, status='planned')
    created = existing and not permanent  # Resume an identical in-memory profile after an interrupted run.
    activated = False
    try:
        if not existing:
            reply = nm('con', 'add', 'save', 'no', 'type', 'vlan', 'ifname', interface, 'con-name', name,
               'autoconnect', 'no', 'dev', parent_uuid, 'id', str(args.vlan),
               'connection.stable-id', stable_id, '802-3-ethernet.mac-address', args.parent_mac,
               'ipv4.method', 'manual', 'ipv4.addresses', args.source + '/32',
               'ipv4.routes', args.target + '/32', 'ipv4.never-default', 'yes', 'ipv4.may-fail', 'no',
               'ipv4.dad-timeout', '3000', 'ipv6.method', 'disabled')
            # NetworkManager assigns this UUID; Ubuntu 22.04 rejects 'connection.uuid'
            # as an input to con add. Resolve the new object rather than operating by name.
            match = re.search(r'\(([0-9A-Fa-f-]{36})\)', reply)
            if match:
                profile_uuid = str(uuid.UUID(match[1]))
            else:
                profile_uuid = str(uuid.UUID(nm('-g', 'connection.uuid', 'con', 'show', 'id', name).strip()))
                if nm('-g', 'connection.stable-id', 'con', 'show', 'uuid', profile_uuid).strip() != stable_id:
                    raise SetupError('새로 만든 임시 프로필을 식별할 수 없습니다.')
            created = True
        if not active:
            nm('con', 'up', 'uuid', profile_uuid)
            activated = True
        observed = verify_active(args, interface, profile_uuid)
        if created:
            # A regular (not --temporary) update persists this in-memory connection.
            nm('con', 'modify', 'uuid', profile_uuid, 'connection.autoconnect', 'yes')
        if not profile_is_persistent(profile_uuid) or not settings_match(args, profile_uuid, interface, parent_uuid, name):
            raise SetupError('NetworkManager가 영구 프로필을 저장했는지 확인하지 못했습니다.')
        return dict(info, status='ready', profile_uuid=profile_uuid, management_mac=observed)
    except BaseException as exc:
        try:
            if created:
                nm('con', 'delete', 'uuid', profile_uuid)
            elif activated:
                nm('con', 'down', 'uuid', profile_uuid)
        except SetupError as cleanup:
            raise SetupError(f'{exc} 임시 프로필 정리에도 실패했습니다. {profile_uuid}를 확인하세요. {cleanup}') from None
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('parent', 'parent-mac', 'source', 'target'):
        parser.add_argument('--' + field, required=True)
    parser.add_argument('--vlan', required=True, type=int)
    parser.add_argument('--management-mac', default='')
    parser.add_argument('--apply', action='store_true')
    try:
        result = configure(parser.parse_args())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise SetupError('관리 경로 설정이 중단되었습니다.')
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
