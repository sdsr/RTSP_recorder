#!/bin/sh
# Open the localhost browser UI without requiring Tk.
set -eu
RECORDER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$RECORDER_DIR/실행.sh" --ui "$@"
