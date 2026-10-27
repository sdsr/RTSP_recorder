#!/usr/bin/env python3
"""Install the standalone recorder as a Linux systemd service (Python 3.8+)."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


SERVICE_NAME = "goomi-camera-recorder.service"
UNIT_PATH = Path("/etc/systemd/system") / SERVICE_NAME
MANAGED_MARKER = "# Managed by tools/scheduled_recording/install.py"
SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
HERE = Path(__file__).resolve().parent


def unit_quote(value, command=False):
    """Quote word-list values; command arguments also need literal dollar signs."""
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Paths must not contain control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def unit_directory(value):
    """WorkingDirectory is a scalar path: systemd does not unquote/unescape it."""
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Paths must not contain control characters")
    value = value.replace("%", "%%")
    # Avoid configuration whitespace trimming or a trailing backslash being
    # treated as line continuation, while preserving the same directory.
    if value.endswith((" ", "\\")):
        value += "/."
    return value


def resolve_user(requested=None):
    import pwd  # Linux only; keep importable for isolated tests on other hosts.

    name = requested or os.environ.get("SUDO_USER")
    if not name:
        if os.geteuid() == 0:
            raise ValueError("When running directly as root, specify --user USER")
        name = pwd.getpwuid(os.geteuid()).pw_name
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        raise ValueError("Service user does not exist: " + name) from None
    # Use the canonical account name and reject settings/specifier injection.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*\$?", account.pw_name):
        raise ValueError("Unsupported systemd service user name")
    return account.pw_name


def python_executable():
    system_python = Path("/usr/bin/python3")
    return system_python if system_python.is_file() else Path(sys.executable).resolve()


def render_unit(config_path, user, python_path=None, script_path=None):
    python_path = python_path or python_executable()
    script_path = script_path or HERE / "record.py"
    command = " ".join(unit_quote(value, command=True) for value in
                       (python_path, script_path, "--config", config_path))
    return "\n".join([
        MANAGED_MARKER,
        "[Unit]",
        "Description=Goomi standalone camera scheduled recorder",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "User=" + user,
        "WorkingDirectory=" + unit_directory(config_path.parent),
        "Environment=" + unit_quote("PATH=" + SERVICE_PATH),
        "Environment=PYTHONUNBUFFERED=1",
        "ExecStart=" + command,
        "Restart=on-failure",
        "RestartSec=10",
        "KillMode=mixed",
        "TimeoutStopSec=30",
        "UMask=0077",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def systemctl(*args, capture=False):
    return subprocess.run(["systemctl", *args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None)


def ensure_managed_unit(unit_path=UNIT_PATH):
    """Never replace or remove a unit owned by another installer/operator."""
    if unit_path.is_symlink():
        raise ValueError("Refusing to change a symlink or masked unit: " + str(unit_path))
    if unit_path.exists():
        if not unit_path.is_file():
            raise ValueError("Service unit is not a regular file: " + str(unit_path))
        first_line = unit_path.read_text(encoding="utf-8").splitlines()[:1]
        if first_line != [MANAGED_MARKER]:
            raise ValueError("Refusing to change an unmanaged unit: " + str(unit_path))


def ensure_no_foreign_unit(unit_path=UNIT_PATH):
    ensure_managed_unit(unit_path)
    result = systemctl("show", "--property=FragmentPath", "--property=DropInPaths",
                       SERVICE_NAME, capture=True)
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    fragment = properties.get("FragmentPath", "")
    if fragment and Path(fragment) != unit_path:
        raise ValueError("A different unit already owns " + SERVICE_NAME + ": " + fragment)
    if properties.get("DropInPaths", ""):
        raise ValueError("Service has external drop-ins; review/remove them before using this installer")


def preflight(config_path, user, python_path):
    runuser = shutil.which("runuser")
    env = shutil.which("env")
    if not runuser or not env:
        raise ValueError("runuser and env are required (Ubuntu: util-linux and coreutils)")
    # Identical executable, working directory and PATH to the installed service.
    # --check validates local configuration without contacting any camera.
    subprocess.run([runuser, "-u", user, "--", env, "PATH=" + SERVICE_PATH,
                    "PYTHONDONTWRITEBYTECODE=1", str(python_path), str(HERE / "record.py"),
                    "--config", str(config_path), "--check"],
                   cwd=str(config_path.parent), check=True)


def write_unit(content, unit_path=UNIT_PATH):
    ensure_managed_unit(unit_path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=str(unit_path.parent), prefix="." + SERVICE_NAME,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(unit_path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def install(config_path, user, unit_path=UNIT_PATH):
    python_path = python_executable()
    content = render_unit(config_path, user, python_path=python_path)
    preflight(config_path, user, python_path)
    ensure_no_foreign_unit(unit_path)
    write_unit(content, unit_path)
    systemctl("daemon-reload")
    systemctl("enable", SERVICE_NAME)
    systemctl("restart", SERVICE_NAME)
    print("Installed and started " + SERVICE_NAME)
    print("Logs: journalctl -u " + SERVICE_NAME + " -f")


def uninstall(unit_path=UNIT_PATH):
    ensure_no_foreign_unit(unit_path)
    if not unit_path.exists():
        print(SERVICE_NAME + " is not installed by this tool")
        return
    systemctl("disable", "--now", SERVICE_NAME)
    ensure_managed_unit(unit_path)
    unit_path.unlink()
    systemctl("daemon-reload")
    print("Removed " + SERVICE_NAME + "; configuration and recordings were kept")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Recorder JSON config (default: recording.json beside this script)")
    parser.add_argument("--user", help="Service account (default: SUDO_USER or current non-root account)")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--print-unit", action="store_true", help="Print unit only; no writes or service changes")
    operation.add_argument("--uninstall", action="store_true", help="Stop/remove only this managed service; keep recordings")
    args = parser.parse_args(argv)
    try:
        if not sys.platform.startswith("linux"):
            raise ValueError("This installer requires Linux with systemd")
        if args.uninstall:
            if os.geteuid() != 0:
                raise ValueError("Use sudo to uninstall the system service")
            uninstall()
            return 0
        config_path = (args.config or HERE / "recording.json").expanduser().resolve()
        user = resolve_user(args.user)
        if args.print_unit:
            print(render_unit(config_path, user), end="")
            return 0
        if os.geteuid() != 0:
            raise ValueError("Use sudo to install the system service")
        install(config_path, user)
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
