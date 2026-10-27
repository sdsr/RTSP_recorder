# Embedded into record.py. No desktop libraries are imported by CLI recording.
DESKTOP_ENTRY_NAME = "ed-camera-recorder.desktop"
DESKTOP_ENTRY_MARKER = "X-ED-Recorder-Managed=true"


def require_desktop_tk():
    try:
        import tkinter
    except ImportError:
        raise RuntimeError("데스크톱 화면에 필요한 Tk가 없습니다. Ubuntu에서 sudo apt install python3-tk ffmpeg 실행 후 다시 여세요.") from None
    return tkinter


def show_desktop_error(message):
    # A launcher has no terminal. Report startup failures with a desktop helper
    # if available; the normal log still contains the same actionable message.
    if sys.platform != "linux" or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    for program, arguments in (
            ("zenity", ["--error", "--title=ED 카메라 녹화", "--text=" + str(message)]),
            ("kdialog", ["--error", str(message), "--title", "ED 카메라 녹화"]),
            ("notify-send", ["--", "ED 카메라 녹화", str(message)])):
        executable = shutil.which(program)
        if executable:
            try:
                subprocess.Popen([executable] + arguments, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue


def desktop_value(value):
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("실행 아이콘 경로에 줄바꿈이나 제어 문자를 사용할 수 없습니다.")
    return value.replace("\\", "\\\\")


def desktop_exec_arg(value):
    # Desktop Entry string escaping is applied before Exec quote parsing.
    value = str(value)
    desktop_value(value)
    value = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    return '"' + desktop_value(value) + '"'


def render_desktop_entry(config_path, script_path=None, python_path=None):
    script = Path(script_path or __file__).resolve()
    config = Path(config_path).expanduser().absolute()
    executable = Path(python_path or sys.executable).resolve()
    if "=" in str(executable):
        raise ValueError("Python 실행 경로에는 = 문자를 사용할 수 없습니다.")
    command = " ".join(desktop_exec_arg(arg) for arg in
                       (executable, script, "--config", config, "--gui"))
    return "\n".join([
        "[Desktop Entry]", DESKTOP_ENTRY_MARKER, "Version=1.0", "Type=Application",
        "Name=ED 카메라 녹화", "Name[en]=ED Camera Recorder",
        "Comment=시간표와 날씨 조건으로 카메라 영상을 저장합니다",
        "Exec=" + command, "Path=" + desktop_value(config.parent),
        "Icon=camera-video", "Terminal=false", "StartupNotify=false",
        "Categories=AudioVideo;Video;", "Keywords=ED;camera;recording;녹화;", "",
    ])


def install_desktop_shortcut(config_path):
    if sys.platform != "linux":
        raise ValueError("실행 아이콘은 Linux 데스크톱에서 만들 수 있습니다.")
    if os.geteuid() == 0:
        raise ValueError("실행 아이콘은 sudo 없이 로그인한 일반 사용자로 만드세요.")
    config = Path(config_path).expanduser().absolute()
    if config.suffix.lower() != ".json":
        raise ValueError("데스크톱 화면에는 recording.json 설정을 사용하세요.")
    if not config.parent.is_dir():
        raise ValueError("설정 파일을 둘 폴더를 먼저 만들어 주세요: " + str(config.parent))
    require_desktop_tk()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    if not data_home.is_absolute():
        raise ValueError("XDG_DATA_HOME은 절대 경로여야 합니다.")
    target = data_home / "applications" / DESKTOP_ENTRY_NAME
    if target.is_symlink():
        raise ValueError("기존 실행 아이콘이 심볼릭 링크이므로 변경하지 않았습니다.")
    if target.exists():
        if not target.is_file() or target.stat().st_size > 65536:
            raise ValueError("기존 실행 아이콘을 확인하세요.")
        if DESKTOP_ENTRY_MARKER not in target.read_text(encoding="utf-8").splitlines():
            raise ValueError("다른 프로그램의 실행 아이콘이 있어 변경하지 않았습니다.")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_private_text(target, render_desktop_entry(config))
    target.chmod(0o644)
    return {"message": "앱 메뉴에 ‘ED 카메라 녹화’를 등록했습니다. 이 코드와 설정 파일은 현재 위치에 보관하세요.",
            "path": str(target)}
