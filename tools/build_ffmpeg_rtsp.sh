#!/usr/bin/env bash
# Optional local build. This script is never called by the recorder or installer.
set -euo pipefail

FFMPEG_SOURCE_REPOSITORY='https://github.com/FFmpeg/FFmpeg.git'
FFMPEG_SOURCE_COMMIT='2ec7bc915e56c4f3cb71bb4f96c7d900965b6c82'

usage() {
    cat <<'USAGE'
Usage: bash tools/build_ffmpeg_rtsp.sh --check | --build | --help

  --check  Check local build tools; do not download or build anything.
  --build  Fetch the pinned official FFmpeg source and build a local binary.
  --help   Show this help.

Install location: <repository>/.local/ffmpeg-sha256/bin/ffmpeg
Build jobs: 3, with nice priority 10.
No sudo, package installation, system FFmpeg changes, or camera connections.
USAGE
}

if [[ $# -ne 1 ]]; then
    usage >&2
    exit 2
fi
case "$1" in
    --help|-h) usage; exit 0 ;;
    --check|--build) action=$1 ;;
    *) usage >&2; exit 2 ;;
esac

missing=()
for command_name in git cc make nice install sha256sum grep sed awk cmp mktemp python3 flock tail uname \
                    ar as ld nm ranlib strip dirname mkdir mv rm cat; do
    command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
done
if (( ${#missing[@]} )); then
    printf 'Missing build tools: %s\n' "${missing[*]}" >&2
    printf 'Install the required tools separately, then rerun this command.\n' >&2
    exit 1
fi
if [[ $(uname -s) != Linux ]]; then
    printf 'This build helper is intended for Linux.\n' >&2
    exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
install_dir="$repo_root/.local/ffmpeg-sha256"
work_root="$repo_root/.local/build/ffmpeg-sha256-$FFMPEG_SOURCE_COMMIT"
source_dir="$work_root/source"
build_dir="$work_root/build"
printf 'Official source: %s\nCommit: %s\nInstall: %s/bin/ffmpeg\n' \
    "$FFMPEG_SOURCE_REPOSITORY" "$FFMPEG_SOURCE_COMMIT" "$install_dir"
if [[ $action == --check ]]; then
    printf 'Build tools are available. Nothing was downloaded or changed.\n'
    exit 0
fi

mkdir -p -- "$work_root"
exec 9> "$work_root/build.lock"
if ! flock -n 9; then
    printf 'Another build is already using %s\n' "$work_root" >&2
    exit 1
fi

run_logged() {
    local log_file=$1
    shift
    if ! "$@" >"$log_file" 2>&1; then
        printf 'Command failed. Last log lines (%s):\n' "$log_file" >&2
        tail -n 40 "$log_file" >&2
        return 1
    fi
}

if [[ ! -d $source_dir/.git ]]; then
    if [[ -e $source_dir ]]; then
        printf 'Source path already exists without a Git checkout: %s\n' "$source_dir" >&2
        exit 1
    fi
    git init -q "$source_dir"
    git -C "$source_dir" remote add origin "$FFMPEG_SOURCE_REPOSITORY"
fi
if [[ $(git -C "$source_dir" remote get-url origin) != "$FFMPEG_SOURCE_REPOSITORY" ]]; then
    printf 'Existing source checkout does not use the pinned official repository.\n' >&2
    exit 1
fi
if ! git -C "$source_dir" cat-file -e "$FFMPEG_SOURCE_COMMIT^{commit}" 2>/dev/null; then
    printf 'Downloading the pinned source. Log: %s/fetch.log\n' "$work_root"
    run_logged "$work_root/fetch.log" git -C "$source_dir" fetch --no-tags --depth 1 origin "$FFMPEG_SOURCE_COMMIT"
fi
if git -C "$source_dir" rev-parse --verify HEAD >/dev/null 2>&1; then
    if [[ $(git -C "$source_dir" rev-parse HEAD) != "$FFMPEG_SOURCE_COMMIT" ]] || \
       ! git -C "$source_dir" diff --quiet || ! git -C "$source_dir" diff --cached --quiet || \
       [[ -n $(git -C "$source_dir" ls-files --others --exclude-standard) ]]; then
        printf 'Existing source checkout differs from the pinned clean source; no files were reset.\n' >&2
        exit 1
    fi
else
    git -c core.hooksPath=/dev/null -C "$source_dir" checkout -q --detach "$FFMPEG_SOURCE_COMMIT"
fi
if [[ $(git -C "$source_dir" rev-parse HEAD) != "$FFMPEG_SOURCE_COMMIT" ]]; then
    printf 'Source commit verification failed.\n' >&2
    exit 1
fi
if ! git -C "$source_dir" diff --quiet || ! git -C "$source_dir" diff --cached --quiet || \
   [[ -n $(git -C "$source_dir" ls-files --others --exclude-standard) ]]; then
    printf 'Source checkout is not clean; no modified source will be built.\n' >&2
    exit 1
fi
if ! grep -Fq 'strcmp(digest->algorithm, "SHA-256")' "$source_dir/libavformat/httpauth.c"; then
    printf 'Expected SHA-256 Digest authentication code was not found.\n' >&2
    exit 1
fi

mkdir -p -- "$build_dir"
cd -- "$build_dir"
configure_options=(
    "--prefix=$install_dir"
    --disable-debug --disable-doc --disable-autodetect
    --disable-ffplay --disable-ffprobe --disable-x86asm
    --disable-everything --enable-ffmpeg --enable-network
    --enable-protocol=file,pipe,tcp,udp,rtp
    --enable-demuxer=rtsp,rtp,sdp,mov
    --enable-muxer=mp4,mov,segment,null
    --enable-parser=h264,hevc,aac,mpeg4video
    --enable-bsf=extract_extradata,aac_adtstoasc,h264_mp4toannexb,hevc_mp4toannexb,filter_units,null
    --enable-filter=null,anull
)
printf 'Configuring. Log: %s/configure.log\n' "$work_root"
run_logged "$work_root/configure.log" "$source_dir/configure" "${configure_options[@]}"
printf 'Building with 3 jobs at nice priority 10. Log: %s/build.log\n' "$work_root"
run_logged "$work_root/build.log" nice -n 10 make -j3 ffmpeg

printf 'Checking the local executable without connecting to a camera.\n'
run_logged "$work_root/version.log" ./ffmpeg -hide_banner -version
run_logged "$work_root/rtsp-help.log" ./ffmpeg -hide_banner -h demuxer=rtsp
run_logged "$work_root/segment-help.log" ./ffmpeg -hide_banner -h muxer=segment
run_logged "$work_root/mp4-help.log" ./ffmpeg -hide_banner -h muxer=mp4
grep -q -- '-rtsp_transport' "$work_root/rtsp-help.log"
grep -Eq -- '^[[:space:]]+-timeout[[:space:]]' "$work_root/rtsp-help.log"
grep -q -- '-segment_time' "$work_root/segment-help.log"
grep -q -- 'frag_keyframe' "$work_root/mp4-help.log"

mkdir -p -- "$install_dir/bin"
candidate=$(mktemp "$install_dir/bin/.ffmpeg.XXXXXX")
trap 'if [[ -n ${candidate:-} ]]; then rm -f -- "$candidate"; fi' EXIT
install -m 755 -- ./ffmpeg "$candidate"
mv -f -- "$candidate" "$install_dir/bin/ffmpeg"
candidate=''
python3 - "$install_dir" "$FFMPEG_SOURCE_REPOSITORY" "$FFMPEG_SOURCE_COMMIT" "$work_root" "${configure_options[@]}" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

prefix, repository, commit, work = sys.argv[1:5]
binary = Path(prefix) / "bin" / "ffmpeg"
manifest = {
    "source_repository": repository,
    "source_commit": commit,
    "source_modified": False,
    "binary": str(binary),
    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "configure_options": sys.argv[5:],
    "build_command": ["nice", "-n", "10", "make", "-j3", "ffmpeg"],
    "build_logs": work,
    "runtime_option_checks_passed": True,
    "camera_connection_attempted_by_builder": False,
    "system_ffmpeg_or_network_modified": False,
}
(Path(prefix) / "build_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY
printf 'Installed: %s/bin/ffmpeg\n' "$install_dir"
sha256sum "$install_dir/bin/ffmpeg"
printf 'Set the recorder configuration ffmpeg field to this executable path to use it.\n'
