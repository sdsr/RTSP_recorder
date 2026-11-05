#!/bin/sh
# Shared launcher. Browser by default; --gui opens the native desktop window.
set -eu
umask 077
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3이 필요합니다. Ubuntu: sudo apt install python3' >&2
    exit 1
fi
RECORDER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RECORDER_MODE=--ui
case "${1-}" in
    --gui) RECORDER_MODE=--gui; shift ;;
    --ui) shift ;;
esac
python3 - "$RECORDER_DIR" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

folder = Path(sys.argv[1])
config = folder / "recording.json"
template = folder / "recording.portable.example.json"
temporary = None
try:
    if config.is_symlink():
        raise ValueError("recording.json이 심볼릭 링크입니다. 일반 설정 파일을 사용하세요.")
    if not config.exists() and template.is_file():
        content = template.read_bytes()
        if not isinstance(json.loads(content.decode("utf-8-sig")), dict):
            raise ValueError("기본 설정 예제는 JSON 객체여야 합니다.")
        # Publish a complete private file atomically, without replacing settings
        # another launcher may have created while this one was preparing them.
        with tempfile.NamedTemporaryFile(dir=str(folder), prefix=".recorder-init-", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(str(temporary), str(config))
        except FileExistsError:
            pass
    if config.exists() or config.is_symlink():
        fd = os.open(str(config), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("recording.json은 일반 설정 파일이어야 합니다.")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    binary = folder / "bin" / "ffmpeg"
    if binary.is_file() and not binary.is_symlink():
        fd = os.open(str(binary), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and not info.st_mode & stat.S_IXUSR:
                os.fchmod(fd, stat.S_IMODE(info.st_mode) | stat.S_IXUSR)
        finally:
            os.close(fd)
except (OSError, ValueError, UnicodeError) as exc:
    if isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        message = "기본 설정 예제의 JSON 형식을 확인하세요."
    else:
        message = str(exc)
    print("녹화 도구를 준비하지 못했습니다: " + message, file=sys.stderr)
    sys.exit(1)
finally:
    if temporary is not None:
        temporary.unlink()
PY
exec python3 "$RECORDER_DIR/record.py" --config "$RECORDER_DIR/recording.json" "$RECORDER_MODE" "$@"
