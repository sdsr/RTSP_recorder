#!/usr/bin/env bash
# Only arping receives CAP_NET_RAW. No Python capability, sudo escalation,
# package installation, camera request, IP assignment or route change occurs.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

usage() {
  printf '%s\n' '카메라 IP 중복 검사 준비: arping 실행 파일에만 CAP_NET_RAW 권한을 설정합니다.' \
    '검사: bash prepare_camera_admin.sh' \
    '적용: sudo bash prepare_camera_admin.sh --apply' \
    '카메라·허브·PC IP·VLAN·LTE·Python 실행 권한은 변경하지 않습니다.'
}
APPLY=0
case "${1:-}" in
  '') ;;
  --apply) APPLY=1 ;;
  --help|-h) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then usage >&2; exit 2; fi

ARPING_PATH="$(command -v arping || true)"
if [[ -z "$ARPING_PATH" ]]; then
  printf '%s\n' 'arping이 없습니다. 먼저 sudo apt install arping 을 실행하세요.' >&2
  exit 1
fi
ARPING_BIN="$(readlink -e -- "$ARPING_PATH")"
if [[ ! -f "$ARPING_BIN" || ! -x "$ARPING_BIN" || -L "$ARPING_BIN" || "${ARPING_BIN##*/}" != arping ]]; then
  printf '%s\n' '일반 arping 실행 파일을 확인하지 못했습니다. 변경하지 않습니다.' >&2; exit 1
fi
check_root_owned() {
  local checked_path="$1" owner mode
  owner="$(stat -c '%u' -- "$checked_path")"
  mode="$(stat -c '%a' -- "$checked_path")"
  if [[ "$owner" != 0 ]] || (( (8#$mode & 0022) != 0 )); then
    printf '%s\n' 'arping 또는 상위 경로가 root 소유이거나 쓰기 보호된 경로가 아닙니다. 변경하지 않습니다.' >&2
    exit 1
  fi
}
check_root_owned "$ARPING_BIN"
PARENT_DIR="$(dirname -- "$ARPING_BIN")"
while [[ "$PARENT_DIR" != / ]]; do
  check_root_owned "$PARENT_DIR"
  PARENT_DIR="$(dirname -- "$PARENT_DIR")"
done
check_root_owned /
MODE="$(stat -c '%a' -- "$ARPING_BIN")"
if (( (8#$MODE & 06000) != 0 )); then
  printf '%s\n' 'arping에 setuid/setgid 권한이 이미 있습니다. 추가 권한 변경은 수행하지 않습니다.' >&2; exit 1
fi
MAGIC="$(od -An -tx1 -N4 -- "$ARPING_BIN" | tr -d ' \n')"
if [[ "$MAGIC" != 7f454c46 ]]; then
  printf '%s\n' '배포판에서 설치한 ELF arping 실행 파일이 아닙니다. 변경하지 않습니다.' >&2; exit 1
fi
if ! command -v getcap >/dev/null || ! command -v setcap >/dev/null; then
  printf '%s\n' 'getcap/setcap이 없습니다. 먼저 sudo apt install libcap2-bin 을 실행하세요.' >&2; exit 1
fi
CURRENT="$(getcap -- "$ARPING_BIN")"
printf '확인한 실행 파일: %s\n현재 확장 권한: %s\n' "$ARPING_BIN" "${CURRENT:-없음}"
if [[ "$CURRENT" == "$ARPING_BIN cap_net_raw=ep" ]]; then
  printf '%s\n' '이미 필요한 arping 권한이 설정되어 있습니다. 변경하지 않았습니다.'; exit 0
fi
if [[ -n "$CURRENT" && "$CURRENT" != "$ARPING_BIN cap_net_raw=p" ]]; then
  printf '%s\n' '기존 확장 권한이 있어 자동으로 덮어쓰지 않습니다. 담당자가 먼저 확인하세요.' >&2; exit 1
fi
if (( APPLY == 0 )); then
  printf '%s\n' '검사만 완료했습니다. 적용하려면 sudo bash prepare_camera_admin.sh --apply 를 실행하세요.'; exit 0
fi
if (( EUID != 0 )); then
  printf '%s\n' '권한 설정에는 관리자 인증이 필요합니다: sudo bash prepare_camera_admin.sh --apply' >&2; exit 1
fi
setcap cap_net_raw+ep "$ARPING_BIN"
AFTER="$(getcap -- "$ARPING_BIN")"
if [[ "$AFTER" != "$ARPING_BIN cap_net_raw=ep" ]]; then
  printf '%s\n' '설정 후 권한 검증에 실패했습니다. arping 권한을 확인하세요.' >&2; exit 1
fi
printf '%s\n' 'arping의 CAP_NET_RAW 설정을 확인했습니다. 녹화 프로그램은 기존 사용자로 실행하세요.'
