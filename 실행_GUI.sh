#!/bin/sh
# Use the same private-config and bundled-FFmpeg preparation as the browser UI.
set -eu
RECORDER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$RECORDER_DIR/실행.sh" --gui "$@"
