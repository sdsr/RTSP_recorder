#!/bin/sh
# Launch the native desktop window from any working directory.
set -eu
RECORDER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Python 3이 필요합니다. Ubuntu: sudo apt install python3 python3-tk ffmpeg' >&2
    exit 1
fi
exec python3 "$RECORDER_DIR/record.py" --config "$RECORDER_DIR/recording.json" --gui
