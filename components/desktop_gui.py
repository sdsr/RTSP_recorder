# Embedded into record.py. Tk is imported only when the desktop GUI is requested.
import queue


DESKTOP_PROVIDERS = (
    ("open_meteo", "Open-Meteo · 개인용 / 키 없음"),
    ("met_no", "MET Norway · 연락처 필요"),
    ("data_go_kr", "기상청 · 공공데이터포털"),
    ("kma_api_hub", "기상청 · API허브"),
)


def desktop_number(value, label, integer=False):
    try:
        result = int(str(value).strip()) if integer else float(str(value).strip())
    except (ValueError, TypeError):
        raise ValueError(label + ("은 정수로 입력하세요." if integer else "은 숫자로 입력하세요.")) from None
    if not math.isfinite(result):
        raise ValueError(label + "에 올바른 숫자를 입력하세요.")
    return result


def desktop_dates(text):
    values = [value for value in re.split(r"[\s,;]+", text.strip()) if value]
    parse_recording_dates(values)
    return values


def desktop_config_patch(baseline, current):
    """Only send fields edited in this window; preserve hidden/legacy settings."""
    return {name: value for name, value in current.items() if value != baseline.get(name)}


def desktop_forecast_text(weather, busy=False):
    if busy:
        return "예보를 확인하고 있습니다. 잠시 기다려 주세요."
    if weather.get("error"):
        return weather["error"]
    if not weather.get("enabled"):
        result = "날씨 조건이 꺼져 있습니다. 날짜와 시간표에 따라 녹화합니다."
    elif weather.get("allowed"):
        result = "오늘은 날씨 조건에 맞습니다. 오늘의 예약 시간표를 실행합니다."
    elif weather.get("known"):
        result = "확인한 예보에는 선택한 날씨가 없습니다. 조건을 기다립니다."
    else:
        result = "아직 유효한 예보가 없습니다. 예보 조회를 눌러 확인하세요."
    detail = " · ".join(str(weather.get(key, "")) for key in ("region", "label", "source") if weather.get(key))
    if detail:
        result += "\n" + detail
    if weather.get("checked_at"):
        result += "\n최근 확인: " + weather["checked_at"].replace("T", " ")[:19] + " KST"
    network = weather.get("network") or {}
    if network:
        result += "\n오늘 수신 {:.1f} KB · 요청 {}회 (응답 본문 기준){}".format(
            network.get("body_bytes_today", 0) / 1024, network.get("requests_today", 0),
            " · 캐시 사용" if network.get("cached") else "")
    return result


class RecorderDesktopApp:
    """Native forms over the existing controller; all controller work is serialized."""

    def __init__(self, root, tk, ttk, dialogs, controller):
        self.root, self.tk, self.ttk = root, tk, ttk
        self.filedialog, self.messagebox = dialogs
        self.controller = controller
        self.jobs, self.results = queue.Queue(), queue.Queue()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()
        self.pending_action, self.poll_pending, self.closing, self.destroyed = False, False, False, False
        self.loading, self.loaded, self.dirty = False, False, False
        self.revision, self.baseline, self.last_state = None, {}, {}
        self.rows, self.form, self.condition_vars = [], {}, {}
        self.region_rows, self.disabled_widgets, self.tab_canvases = [], [], {}
        self.wrap_labels, self.wrap_job = [], None
        self.last_logs = None
        self.root.title("ED 카메라 녹화")
        self.root.geometry("1120x820")
        self.root.minsize(860, 640)
        self.root.configure(background="#edf1f6")
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self._style()
        self._build()
        self.root.after(75, self._pump)
        self.root.after(2000, self._poll)
        self.reload(initial=True)

    def _style(self):
        from tkinter import font as tkfont
        families = {name.casefold(): name for name in tkfont.families(self.root)}
        preferred = ("Noto Sans CJK KR", "Noto Sans KR", "Malgun Gothic", "맑은 고딕", "NanumGothic", "나눔고딕")
        self.font_family = next((families[name.casefold()] for name in preferred if name.casefold() in families),
                                tkfont.nametofont("TkDefaultFont", root=self.root).actual("family"))
        style = self.ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(self.font_family, 11), background="#edf1f6", foreground="#1d304a")
        style.configure("TFrame", background="#edf1f6")
        style.configure("TLabel", background="#edf1f6")
        style.configure("Heading.TLabel", font=(self.font_family, 22, "bold"))
        style.configure("Section.TLabel", font=(self.font_family, 14, "bold"))
        style.configure("Muted.TLabel", foreground="#536783")
        style.configure("TButton", padding=(12, 8))
        style.configure("Primary.TButton", background="#2d5182", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#223e63"), ("disabled", "#a3b0c3")])
        style.configure("TNotebook", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 12))
        style.map("TNotebook.Tab", background=[("selected", "#2d5182")],
                  foreground=[("selected", "#ffffff")], padding=[("selected", (20, 12))])
        style.configure("Treeview", rowheight=30, fieldbackground="#f9fbfd", background="#f9fbfd")
        style.configure("Treeview.Heading", font=(self.font_family, 11, "bold"), padding=(6, 8))
        style.configure("TEntry", fieldbackground="#ffffff", padding=6)
        style.configure("TCombobox", padding=6)

    def _var(self, name, value="", boolean=False, track=True):
        var = self.tk.BooleanVar(self.root, value=value) if boolean else self.tk.StringVar(self.root, value=value)
        self.form[name] = var
        if track:
            var.trace_add("write", self._changed)
        return var

    def _label(self, parent, text, row, column=0, **kwargs):
        label = self.ttk.Label(parent, text=text, **kwargs)
        label.grid(row=row, column=column, sticky="w", padx=(0, 14), pady=5)
        return label

    def _entry(self, parent, name, row, value="", column=1, width=38, **kwargs):
        entry = self.ttk.Entry(parent, textvariable=self._var(name, value), width=width, **kwargs)
        entry.grid(row=row, column=column, sticky="ew", pady=5)
        return entry

    def _scroll_tab(self, title):
        holder = self.ttk.Frame(self.notebook)
        self.notebook.add(holder, text=title)
        canvas = self.tk.Canvas(holder, borderwidth=0, highlightthickness=0, background="#edf1f6")
        bar = self.ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = self.ttk.Frame(canvas, padding=(22, 20, 22, 26))
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.tab_canvases[str(holder)] = canvas
        content.columnconfigure(0, weight=1)
        return content

    def _build(self):
        top = self.ttk.Frame(self.root, padding=(24, 20, 24, 14))
        top.pack(fill="x")
        self.ttk.Label(top, text="카메라 녹화 관리", style="Heading.TLabel").pack(side="left")
        self.ttk.Label(top, text="Linux 데스크톱  /  한국 시간", style="Muted.TLabel").pack(side="right")
        self.notice = self.tk.StringVar(self.root, "설정을 불러오고 있습니다.")
        self.ttk.Label(self.root, textvariable=self.notice, wraplength=1000, padding=(24, 8),
                       style="Muted.TLabel").pack(fill="x")
        footer = self.ttk.Frame(self.root, padding=(20, 12))
        footer.pack(side="bottom", fill="x")
        self.dirty_text = self.tk.StringVar(self.root, "")
        self.ttk.Label(footer, textvariable=self.dirty_text).pack(side="left")
        self.ttk.Button(footer, text="설정 저장 · 적용", style="Primary.TButton", command=self.save).pack(side="right")
        self.ttk.Button(footer, text="설정 검사", command=lambda: self.with_settings(self.controller.check)).pack(side="right", padx=8)
        self.ttk.Button(footer, text="다시 불러오기", command=self.reload).pack(side="right")
        self.notebook = self.ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=18)
        self.status_tab = self._scroll_tab("녹화 현황")
        self.schedule_tab = self._scroll_tab("시간표 · 날씨")
        self.device_tab = self._scroll_tab("장비 · 저장 위치")
        self._build_status()
        self._build_schedule()
        self._build_device()
        self.root.bind("<MouseWheel>", self._wheel, add="+")
        self.root.bind("<Button-4>", self._wheel, add="+")
        self.root.bind("<Button-5>", self._wheel, add="+")
        self._prepare_wrap(self.root)
        self.root.bind("<Configure>", self._queue_wrap, add="+")
        self._queue_wrap()

    def _prepare_wrap(self, parent):
        for child in parent.winfo_children():
            if isinstance(child, self.ttk.Label):
                length = self.root.winfo_pixels(child.cget("wraplength") or 0)
                if length or len(str(child.cget("text"))) >= 38:
                    child.configure(wraplength=min(length or 520, 520), justify="left")
                    self.wrap_labels.append(child)
            self._prepare_wrap(child)

    def _queue_wrap(self, _event=None):
        if self.wrap_job is None and not self.destroyed:
            self.wrap_job = self.root.after(90, self._wrap_content)

    def _wrap_content(self):
        self.wrap_job = None
        if self.destroyed:
            return
        for label in self.wrap_labels:
            if not label.winfo_exists() or not label.winfo_ismapped():
                continue
            parent = label.master
            if parent.winfo_width() < 150:
                continue
            available = max(120, min(950, parent.winfo_width() - label.winfo_x() - 40))
            current = self.root.winfo_pixels(label.cget("wraplength"))
            if abs(current - available) > 3:
                label.configure(wraplength=available)

    def _wheel(self, event):
        if isinstance(event.widget, (self.tk.Text, self.ttk.Treeview, self.ttk.Combobox)):
            return
        canvas = self.tab_canvases.get(self.notebook.select())
        if canvas is not None:
            step = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
            canvas.yview_scroll(step * 3, "units")

    def _build_status(self):
        parent = self.status_tab
        self.state_text = self.tk.StringVar(self.root, "녹화 중지")
        self.ttk.Label(parent, textvariable=self.state_text, style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        controls = self.ttk.Frame(parent)
        controls.grid(row=1, column=0, sticky="ew", pady=(14, 8))
        self.start_button = self.ttk.Button(controls, text="예약 녹화 시작", style="Primary.TButton",
                                           command=lambda: self.with_settings(lambda: self.controller.start("scheduled")))
        self.start_button.pack(side="left")
        self.stop_button = self.ttk.Button(controls, text="녹화 중지", command=lambda: self.action(self.controller.stop))
        self.stop_button.pack(side="left", padx=8)
        self.ttk.Button(controls, text="저장 폴더 열기", command=self.open_output).pack(side="right")
        self.summary_text = self.tk.StringVar(self.root, "")
        self.ttk.Label(parent, textvariable=self.summary_text, wraplength=950).grid(row=2, column=0, sticky="w", pady=8)
        once = self.ttk.LabelFrame(parent, text="한 번만 녹화", padding=14)
        once.grid(row=3, column=0, sticky="ew", pady=(12, 16))
        self.ttk.Label(once, text="녹화 길이").grid(row=0, column=0, sticky="w")
        self.ttk.Entry(once, textvariable=self._var("once_minutes", "5", track=False), width=7).grid(row=0, column=1, padx=8)
        self.ttk.Label(once, text="분").grid(row=0, column=2)
        self.ttk.Checkbutton(once, text="오늘 시작 시각 지정", variable=self._var("once_later", False, boolean=True, track=False),
                             command=self._once_mode).grid(row=0, column=3, padx=(22, 8))
        self.once_time = self.ttk.Entry(once, textvariable=self._var("once_time", "18:00", track=False), width=8, state="disabled")
        self.once_time.grid(row=0, column=4)
        self.once_button = self.ttk.Button(once, text="시험 녹화 시작", command=self.start_once)
        self.once_button.grid(row=0, column=5, padx=(18, 0))
        self.ttk.Label(once, text="날짜·시간표·날씨 조건과 관계없이 녹화하며, 정한 시간이 끝나면 자동 종료합니다.",
                       style="Muted.TLabel", wraplength=900).grid(row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))
        self.ttk.Label(parent, text="연결할 카메라", style="Section.TLabel").grid(row=4, column=0, sticky="w", pady=(0, 8))
        self.camera_tree = self.ttk.Treeview(parent, columns=("client", "name", "direction", "state"), show="headings", height=4)
        for column, title, width in (("client", "Client 번호", 130), ("name", "교차로명", 330),
                                     ("direction", "방향", 140), ("state", "상태", 160)):
            self.camera_tree.heading(column, text=title)
            self.camera_tree.column(column, width=width, minwidth=80)
        self.camera_tree.grid(row=5, column=0, sticky="ew")
        weather_head = self.ttk.Frame(parent)
        weather_head.grid(row=6, column=0, sticky="ew", pady=(20, 6))
        self.ttk.Label(weather_head, text="날씨와 녹화 조건", style="Section.TLabel").pack(side="left")
        self.ttk.Button(weather_head, text="예보 조회", command=lambda: self.with_settings(self.controller.forecast)).pack(side="right")
        self.weather_text = self.tk.StringVar(self.root, "")
        self.ttk.Label(parent, textvariable=self.weather_text, wraplength=950).grid(row=7, column=0, sticky="w", pady=5)
        self.ttk.Label(parent, text="최근 실행 기록", style="Section.TLabel").grid(row=8, column=0, sticky="w", pady=(20, 8))
        log_frame = self.ttk.Frame(parent)
        log_frame.grid(row=9, column=0, sticky="ew")
        self.log_box = self.tk.Text(log_frame, height=8, font=(self.font_family, 10), background="#f9fbfd",
                                    foreground="#1d304a", wrap="word", borderwidth=1, relief="solid", state="disabled")
        self.log_box.pack(side="left", fill="both", expand=True)
        log_scroll = self.ttk.Scrollbar(log_frame, command=self.log_box.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=log_scroll.set)
        self.ttk.Label(parent, text="이 창을 종료하면 이 창에서 시작한 녹화도 정상 종료합니다. 계속 녹화하려면 창을 최소화하세요.",
                       style="Muted.TLabel", wraplength=950).grid(row=10, column=0, sticky="w", pady=(16, 0))

    def _build_schedule(self):
        parent = self.schedule_tab
        self.ttk.Label(parent, text="녹화할 시간", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Label(parent, text="모든 시각은 한국 시간입니다. 종료가 시작보다 이르면 다음 날까지 녹화합니다.",
                       style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 12))
        self.schedule_note = self.tk.StringVar(self.root, "")
        self.ttk.Label(parent, textvariable=self.schedule_note, wraplength=950).grid(row=2, column=0, sticky="w")
        self.range_frame = self.ttk.Frame(parent)
        self.range_frame.grid(row=3, column=0, sticky="ew")
        self.ttk.Button(parent, text="시간 구간 추가", command=self.add_range).grid(row=4, column=0, sticky="w", pady=8)
        dates = self.ttk.Frame(parent)
        dates.grid(row=5, column=0, sticky="ew", pady=(12, 16))
        dates.columnconfigure(1, weight=1)
        self._label(dates, "녹화 날짜", 0)
        self._entry(dates, "recording_dates", 0)
        self.ttk.Label(dates, text="비우면 매일 반복. 예: 2026-10-28, 2026-10-30", style="Muted.TLabel").grid(row=1, column=1, sticky="w")
        self.ttk.Separator(parent).grid(row=6, column=0, sticky="ew", pady=(0, 18))
        self.ttk.Label(parent, text="날씨 조건", style="Section.TLabel").grid(row=7, column=0, sticky="w")
        self.ttk.Checkbutton(parent, text="선택한 날씨가 예보된 날에만 예약 녹화", variable=self._var("weather_enabled", False, boolean=True)).grid(row=8, column=0, sticky="w", pady=10)
        self.ttk.Label(parent, text="하루 중 한 번이라도 조건이 맞으면 그날의 시간표를 그대로 실행합니다. 실제 관측 날씨가 아닌 예보 기준입니다.",
                       style="Muted.TLabel", wraplength=950).grid(row=9, column=0, sticky="w", pady=(0, 12))
        weather = self.ttk.Frame(parent)
        weather.grid(row=10, column=0, sticky="ew")
        weather.columnconfigure(1, weight=1)
        self._label(weather, "예보 제공처", 0)
        self.provider = self.ttk.Combobox(weather, textvariable=self._var("provider"), state="readonly", values=[label for _, label in DESKTOP_PROVIDERS])
        self.provider.grid(row=0, column=1, sticky="ew", pady=5)
        self.provider.bind("<<ComboboxSelected>>", lambda _event: self._provider_fields())
        region = self.ttk.Frame(weather)
        region.grid(row=1, column=1, sticky="ew", pady=5)
        self._label(weather, "녹화 지역", 1)
        self.region_boxes = []
        for index, (key, width) in enumerate((("sido", 18), ("sigungu", 19), ("eupmyeondong", 18))):
            box = self.ttk.Combobox(region, textvariable=self._var(key), state="readonly", width=width)
            box.grid(row=0, column=index, padx=(0, 6), sticky="ew")
            region.columnconfigure(index, weight=1)
            self.region_boxes.append(box)
            box.bind("<<ComboboxSelected>>", lambda _event, level=index: self._region_changed(level))
        self.ttk.Label(weather, text="읍·면·동은 비워 두면 시·군·구 대표 지점을 사용합니다.", style="Muted.TLabel").grid(row=2, column=1, sticky="w")
        self._label(weather, "녹화할 날씨", 3)
        choices = self.ttk.Frame(weather)
        choices.grid(row=3, column=1, sticky="w", pady=8)
        for name, label in WEATHER_LABELS.items():
            var = self._var("condition_" + name, False, boolean=True)
            self.condition_vars[name] = var
            self.ttk.Checkbutton(choices, text=label, variable=var).pack(side="left", padx=(0, 18))
        self._label(weather, "예보 조회 간격", 4)
        interval = self.ttk.Frame(weather)
        interval.grid(row=4, column=1, sticky="w")
        self.ttk.Entry(interval, textvariable=self._var("poll_minutes", "30"), width=7).pack(side="left")
        self.ttk.Label(interval, text="분 (10~180분, 유효한 캐시 재사용)").pack(side="left", padx=8)
        self.met_frame = self.ttk.Frame(weather)
        self.met_frame.grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)
        self.met_frame.columnconfigure(1, weight=1)
        self._label(self.met_frame, "연락처", 0)
        self._entry(self.met_frame, "met_contact", 0)
        self.ttk.Label(self.met_frame, text="MET Norway에 전송할 개인 이메일 또는 프로젝트 홈페이지 주소", style="Muted.TLabel").grid(row=1, column=1, sticky="w")
        self.key_frame = self.ttk.Frame(weather)
        self.key_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=8)
        self.key_frame.columnconfigure(1, weight=1)
        self._label(self.key_frame, "인증키 파일", 0)
        self._entry(self.key_frame, "api_key_file", 0, "kma_service.key")
        self._label(self.key_frame, "새 인증키", 1)
        self.key_input = self.ttk.Entry(self.key_frame, show="*", width=38)
        self.key_input.grid(row=1, column=1, sticky="ew", pady=5)
        self.ttk.Button(self.key_frame, text="인증키 저장", command=self.save_key).grid(row=1, column=2, padx=8)
        self.ttk.Label(self.key_frame, text="저장된 키는 다시 표시하지 않습니다. 새 키를 등록할 때만 입력하세요.", style="Muted.TLabel").grid(row=2, column=1, sticky="w")
        self.provider_note = self.tk.StringVar(self.root, "")
        self.ttk.Label(weather, textvariable=self.provider_note, wraplength=900, style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.ttk.Button(weather, text="제공처 안내 열기", command=self.provider_info).grid(row=8, column=1, sticky="w", pady=8)

    def _build_device(self):
        parent = self.device_tab
        self.ttk.Label(parent, text="카메라와 저장 위치", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        fields = self.ttk.Frame(parent)
        fields.grid(row=1, column=0, sticky="ew", pady=14)
        fields.columnconfigure(1, weight=1)
        self._label(fields, "카메라 setup.ini", 0)
        self.setup_box = self.ttk.Combobox(fields, textvariable=self._var("setup_ini", "auto"), width=55)
        self.setup_box.grid(row=0, column=1, sticky="ew", pady=5)
        self.ttk.Button(fields, text="파일 선택", command=self.choose_setup).grid(row=0, column=2, padx=(8, 0))
        self.ttk.Button(fields, text="자동 찾기", command=self.find_setup).grid(row=1, column=1, sticky="w", pady=5)
        self.setup_note = self.tk.StringVar(self.root, "여러 설정이 나오면 실제 운영 중인 파일을 직접 선택하세요.")
        self.ttk.Label(fields, textvariable=self.setup_note, style="Muted.TLabel", wraplength=700).grid(row=2, column=1, sticky="w", pady=5)
        self._label(fields, "영상 저장 폴더", 3)
        self._entry(fields, "output_dir", 3, "auto")
        self.ttk.Button(fields, text="폴더 선택", command=self.choose_output).grid(row=3, column=2, padx=(8, 0))
        self.ttk.Label(fields, text="auto이면 프로젝트의 runtime/scheduled_recordings 폴더에 저장합니다.", style="Muted.TLabel").grid(row=4, column=1, sticky="w", pady=5)
        self._label(fields, "교차로명", 5)
        self._entry(fields, "intersection_name", 5)
        self.ttk.Label(fields, text="비우면 카메라 설정의 이름을 사용합니다.", style="Muted.TLabel").grid(row=6, column=1, sticky="w")
        self._label(fields, "파일명 날씨 표시", 7)
        self._entry(fields, "weather", 7)
        self.ttk.Label(fields, text="수동 표시용 이름입니다. 자동 예보 조건은 시간표·날씨에서 설정하세요.", style="Muted.TLabel").grid(row=8, column=1, sticky="w")
        self._label(fields, "파일 분할 (분)", 9)
        self.ttk.Combobox(fields, textvariable=self._var("segment_minutes", "30"), values=("5", "10", "30", "60"), width=12).grid(row=9, column=1, sticky="w", pady=5)
        self._label(fields, "자동 삭제 기준 (일)", 10)
        self._entry(fields, "retention_days", 10, "0", width=12).grid_configure(sticky="w")
        self.ttk.Label(fields, text="0이면 자동 삭제하지 않습니다. 1 이상이면 보관 기간이 지난 녹화 파일을 삭제합니다.", style="Muted.TLabel", wraplength=740).grid(row=11, column=1, sticky="w")
        self._label(fields, "최소 여유 공간 (GiB)", 12)
        self._entry(fields, "min_free_gb", 12, "5", width=12).grid_configure(sticky="w")
        self.ttk.Separator(parent).grid(row=2, column=0, sticky="ew", pady=16)
        self.ttk.Label(parent, text="다음에도 간단히 실행", style="Section.TLabel").grid(row=3, column=0, sticky="w")
        self.ttk.Label(parent, text="앱 메뉴에 ‘ED 카메라 녹화’ 아이콘을 만듭니다. 이 작업은 녹화를 시작하거나 부팅 서비스를 등록하지 않습니다.",
                       style="Muted.TLabel", wraplength=950).grid(row=4, column=0, sticky="w", pady=8)
        self.ttk.Button(parent, text="실행 아이콘 만들기", command=lambda: self.action(
            lambda: install_desktop_shortcut(self.controller.config_path))).grid(row=5, column=0, sticky="w")
        self.ttk.Label(parent, text="현재 설정 파일\n" + str(self.controller.config_path), wraplength=950,
                       style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=(20, 0))

    def _changed(self, *_args):
        if not self.loading and self.loaded:
            self.dirty = True
            self.dirty_text.set("저장하지 않은 변경 사항이 있습니다.")

    def _once_mode(self):
        self.once_time.configure(state="normal" if self.form["once_later"].get() else "disabled")

    def add_range(self, value="06:00-11:00"):
        if not isinstance(value, str):
            value = "06:00-11:00"
        start, _, end = value.partition("-")
        row = self.ttk.Frame(self.range_frame)
        row.pack(fill="x", pady=4)
        start_var, end_var = self.tk.StringVar(self.root, start), self.tk.StringVar(self.root, end)
        for var in (start_var, end_var):
            var.trace_add("write", self._changed)
        self.ttk.Label(row, text="시작").pack(side="left", padx=(0, 8))
        self.ttk.Entry(row, textvariable=start_var, width=9).pack(side="left")
        self.ttk.Label(row, text="종료").pack(side="left", padx=(18, 8))
        self.ttk.Entry(row, textvariable=end_var, width=9).pack(side="left")
        record = (row, start_var, end_var)
        self.rows.append(record)
        self.ttk.Button(row, text="삭제", command=lambda: self.remove_range(record)).pack(side="left", padx=18)
        self._changed()

    def remove_range(self, record):
        self.rows.remove(record)
        record[0].destroy()
        self._changed()

    def _region_changed(self, level):
        rows = self.region_rows
        if level <= 0:
            cities = sorted({row[1] for row in rows if row[0] == self.form["sido"].get()})
            self.region_boxes[1].configure(values=cities)
            if self.form["sigungu"].get() not in cities:
                self.form["sigungu"].set("")
        towns = sorted({row[2] for row in rows if row[:2] == [self.form["sido"].get(), self.form["sigungu"].get()]})
        self.region_boxes[2].configure(values=towns)
        if self.form["eupmyeondong"].get() not in towns:
            self.form["eupmyeondong"].set("")

    def _provider_id(self):
        value = self.form["provider"].get()
        return next((key for key, label in DESKTOP_PROVIDERS if value == label), "open_meteo")

    def _provider_fields(self):
        provider = self._provider_id()
        self.met_frame.grid() if provider == "met_no" else self.met_frame.grid_remove()
        self.key_frame.grid() if provider in ("data_go_kr", "kma_api_hub") else self.key_frame.grid_remove()
        notes = {
            "open_meteo": "Open-Meteo: 개인·비상업용 무료 API입니다. API 키와 연락처가 필요 없습니다. 예보 데이터 출처: Open-Meteo / CC BY 4.0.",
            "met_no": "MET Norway: API 키는 없지만 실제 연락처를 요청에 포함해야 합니다. 예보 데이터 출처: MET Norway / CC BY 4.0.",
            "data_go_kr": "기상청 공공데이터포털의 단기예보 서비스 인증키가 필요합니다.",
            "kma_api_hub": "기상청 API허브의 단기예보 서비스 인증키가 필요합니다.",
        }
        self.provider_note.set(notes[provider])

    def provider_info(self):
        urls = {"open_meteo": "https://open-meteo.com/en/pricing", "met_no": "https://api.met.no/doc/TermsOfService",
                "data_go_kr": "https://www.data.go.kr/data/15084084/openapi.do", "kma_api_hub": "https://apihub.kma.go.kr/"}
        url = urls[self._provider_id()]
        self.action(lambda: webbrowser.open(url))

    def choose_setup(self):
        value = self.filedialog.askopenfilename(parent=self.root, title="운영 중인 setup.ini 선택",
                                              filetypes=(("카메라 설정", "*.ini"), ("모든 파일", "*")))
        if value:
            self.form["setup_ini"].set(value)

    def choose_output(self):
        value = self.filedialog.askdirectory(parent=self.root, title="영상 저장 폴더 선택")
        if value:
            self.form["output_dir"].set(value)

    def find_setup(self):
        def show(values):
            self.setup_box.configure(values=values)
            self.setup_note.set("{}개를 찾았습니다. 위 목록에서 실제 운영 파일을 선택하세요.".format(len(values)) if values
                                else "자동 검색 결과가 없습니다. ‘파일 선택’으로 setup.ini를 지정하세요.")
        self.action(self.controller.setup_candidates, show, "카메라 설정을 찾고 있습니다.")

    def collect(self):
        values = {name: self.form[name].get().strip() for name in
                  ("setup_ini", "output_dir", "intersection_name", "weather")}
        values["time_ranges"] = [start.get().strip() + "-" + end.get().strip() for _, start, end in self.rows]
        values["recording_dates"] = desktop_dates(self.form["recording_dates"].get())
        for name, label, integer in (("segment_minutes", "파일 분할 길이", False), ("retention_days", "보관 일수", True),
                                     ("min_free_gb", "최소 여유 공간", False)):
            values[name] = desktop_number(self.form[name].get(), label, integer)
        values["weather_condition"] = {
            "enabled": self.form["weather_enabled"].get(), "provider": self._provider_id(),
            "region": {key: self.form[key].get() for key in ("sido", "sigungu", "eupmyeondong")},
            "conditions": [key for key, var in self.condition_vars.items() if var.get()],
            "poll_minutes": desktop_number(self.form["poll_minutes"].get(), "예보 조회 간격"),
            "api_key_file": self.form["api_key_file"].get().strip(), "met_contact": self.form["met_contact"].get().strip(),
        }
        return values

    def _load(self, result):
        config = result["config"]
        self.loading = True
        try:
            for key, default in (("setup_ini", "auto"), ("output_dir", "auto"), ("intersection_name", ""), ("weather", ""),
                                 ("segment_minutes", 30), ("retention_days", 0), ("min_free_gb", 5)):
                self.form[key].set(str(config.get(key, default)))
            self.form["recording_dates"].set(", ".join(config.get("recording_dates", [])))
            for row, _, _ in self.rows:
                row.destroy()
            self.rows = []
            for value in config.get("time_ranges", DEFAULT_TIME_RANGES):
                self.add_range(value)
            self.schedule_note.set("기존 설정에 이 화면에서 표현할 수 없는 시간표가 있습니다. 시간 구간을 추가하면 기존 시간표를 대체합니다."
                                   if config.get("legacy_schedule") else "")
            weather = config.get("weather_condition") or default_weather_config()
            self.form["weather_enabled"].set(weather.get("enabled", False))
            # An existing weather object without provider is the legacy KMA mode.
            provider = weather.get("provider", "data_go_kr")
            self.form["provider"].set(next((label for key, label in DESKTOP_PROVIDERS if key == provider), DESKTOP_PROVIDERS[0][1]))
            self.region_rows = result["regions"]["rows"]
            self.region_boxes[0].configure(values=sorted({row[0] for row in self.region_rows}))
            for key in ("sido", "sigungu", "eupmyeondong"):
                self.form[key].set(weather.get("region", {}).get(key, ""))
            self._region_changed(0)
            for name, var in self.condition_vars.items():
                var.set(name in weather.get("conditions", ["rain"]))
            for key, default in (("poll_minutes", 30), ("api_key_file", "kma_service.key"), ("met_contact", "")):
                self.form[key].set(str(weather.get(key, default)))
            self._provider_fields()
            self.revision = result["revision"]
            try:
                self.baseline = self.collect()
            except ValueError:
                # Let an existing invalid numeric/date value be corrected in the
                # form instead of trapping the user behind a failed first load.
                self.baseline = {key: value for key, value in config.items() if key in (
                    "setup_ini", "output_dir", "intersection_name", "weather", "recording_dates", "time_ranges",
                    "segment_minutes", "retention_days", "min_free_gb", "weather_condition")}
            self.loaded, self.dirty = True, False
            self.dirty_text.set("변경 사항을 저장했습니다.")
        finally:
            self.loading = False

    def reload(self, initial=False):
        if self.dirty and not self.messagebox.askyesno("설정 다시 불러오기", "저장하지 않은 변경을 버리고 파일에서 다시 불러올까요?", parent=self.root):
            return
        def done(value):
            self._load(value)
            self.notice.set("카메라 설정과 저장 위치를 확인하고 5분 시험 녹화를 시작하세요." if initial else "저장된 설정을 불러왔습니다.")
            self._request_state()
        self.action(self.controller.configuration, done, "설정을 불러오고 있습니다.")

    def _patch(self):
        if not self.loaded:
            raise ValueError("설정을 먼저 불러오세요. 현재 설정 파일의 경로와 JSON 형식을 확인하세요.")
        return desktop_config_patch(self.baseline, self.collect())

    def save(self):
        self.with_settings(None, explicit=True)

    def with_settings(self, operation, explicit=False):
        if self.pending_action or self.closing:
            return
        try:
            patch = self._patch()
        except (ValueError, TypeError) as exc:
            self._error(exc)
            return
        if patch and not explicit and not self.messagebox.askyesno("설정 저장", "화면에서 변경한 설정을 저장하고 계속할까요?", parent=self.root):
            return
        revision = self.revision
        def work():
            saved = self.controller.save(patch, revision) if patch else {"message": "변경된 설정이 없습니다."}
            config = self.controller.configuration() if patch else None
            # Preserve a successful save even if the following check/start fails.
            try:
                result = operation() if operation is not None else saved
                return {"config": config, "result": result, "operation_error": None}
            except Exception as exc:
                return {"config": config, "result": saved, "operation_error": exc}
        def done(value):
            if value["config"]:
                self._load(value["config"])
            else:
                self.dirty = False
                self.dirty_text.set("저장된 설정을 사용합니다.")
            result = value["result"]
            if isinstance(result, dict) and result.get("message"):
                self.notice.set(result["message"])
            if value["operation_error"] is not None:
                self._error(value["operation_error"])
            self._request_state()
        self.action(work, done, "설정을 적용하고 있습니다." if patch else "요청을 처리하고 있습니다.")

    def start_once(self):
        try:
            minutes = desktop_number(self.form["once_minutes"].get(), "녹화 길이")
            start = self.form["once_time"].get().strip() if self.form["once_later"].get() else None
            make_once_window(minutes, start)
        except ValueError as exc:
            self._error(exc)
            return
        self.with_settings(lambda: self.controller.start("once", minutes, start))

    def save_key(self):
        key = self.key_input.get()
        if not key.strip():
            self._error(ValueError("등록할 인증키를 입력하세요."))
            return
        self.with_settings(lambda: self.controller.save_key(key))
        self.key_input.delete(0, "end")

    def open_output(self):
        value = self.last_state.get("output_dir")
        if not value:
            self._error(ValueError("저장 위치를 먼저 설정하고 저장하세요."))
            return
        def open_folder():
            path = Path(value)
            if not path.is_dir():
                raise ValueError("아직 저장 폴더가 없습니다. 녹화를 시작하면 생성합니다.")
            executable = shutil.which("xdg-open")
            if not executable:
                raise ValueError("파일 관리자를 찾을 수 없습니다. 저장 위치: " + str(path))
            subprocess.Popen([executable, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"message": "저장 폴더를 열었습니다."}
        self.action(open_folder)

    def _worker(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            kind, function, callback = job
            try:
                self.results.put((kind, callback, function(), None))
            except Exception as exc:
                self.results.put((kind, callback, None, exc))

    def _set_busy(self, busy):
        if busy:
            self.disabled_widgets = []
            def visit(parent):
                for child in parent.winfo_children():
                    if isinstance(child, (self.ttk.Entry, self.ttk.Button, self.ttk.Combobox, self.ttk.Checkbutton)):
                        self.disabled_widgets.append((child, child.state()))
                        child.state(["disabled"])
                    visit(child)
            visit(self.root)
        else:
            for widget, states in self.disabled_widgets:
                if widget.winfo_exists():
                    widget.state(["!disabled", "!readonly"])
                    widget.state(states)
            self.disabled_widgets = []
            self._update_buttons()

    def action(self, function, callback=None, message="요청을 처리하고 있습니다."):
        if self.pending_action or self.closing:
            return
        self.pending_action = True
        self._set_busy(True)
        self.notice.set(message)
        self.jobs.put(("action", function, callback))

    def _request_state(self):
        if not self.poll_pending and not self.closing:
            self.poll_pending = True
            self.jobs.put(("state", self.controller.state, self._show_state))

    def _poll(self):
        if self.closing:
            return
        if not self.pending_action and self.loaded:
            self._request_state()
        self.root.after(2000, self._poll)

    def _pump(self):
        try:
            while True:
                kind, callback, value, error = self.results.get_nowait()
                if kind == "state":
                    self.poll_pending = False
                if kind == "action":
                    self.pending_action = False
                    self._set_busy(False)
                if error is not None:
                    if kind == "close":
                        self.closing = False
                        self.controller.closing = False
                        self.pending_action = False
                        self._set_busy(False)
                        self.root.after(2000, self._poll)
                    if kind == "state":
                        self.notice.set(friendly_ui_error(error))
                    else:
                        self._error(error)
                elif callback is not None:
                    callback(value)
                    if self.destroyed:
                        return
                elif isinstance(value, dict) and value.get("message"):
                    self.notice.set(value["message"])
                    self._request_state()
        except queue.Empty:
            pass
        except Exception as exc:
            self._error(exc)
        if not self.destroyed and self.root.winfo_exists():
            self.root.after(75, self._pump)

    def _error(self, exc):
        message = friendly_ui_error(exc)
        self.notice.set(message)
        self.messagebox.showerror("확인이 필요합니다", message, parent=self.root)

    def _update_buttons(self):
        if self.pending_action or self.closing:
            return
        running = self.last_state.get("running", False)
        external = self.last_state.get("external_running", False)
        for button in (self.start_button, self.once_button):
            button.state(["disabled"] if running or external else ["!disabled"])
        self.stop_button.state(["!disabled"] if running else ["disabled"])
        self._once_mode()

    def _show_state(self, state):
        self.last_state = state
        running, external, active = state.get("running"), state.get("external_running"), state.get("active_cameras", [])
        if external:
            label = "별도 서비스가 녹화 중입니다"
        elif running and active:
            label = "녹화 중 · {}대".format(len(active))
        elif running:
            label = "한 번 녹화 준비 · 대기" if state.get("mode") == "once" else "예약 녹화 대기 중"
        else:
            label = "녹화 중지"
        self.state_text.set(label)
        cameras = state.get("cameras", [])
        text = "대상 카메라 {}대  ·  남은 공간 {} GiB".format(len(cameras), state.get("disk", {}).get("free_gb", "—"))
        if state.get("once_end") and running:
            text += "  ·  종료 " + state["once_end"].replace("T", " ")[:19] + " KST"
        if state.get("output_dir"):
            text += "\n저장 위치: " + state["output_dir"]
        if external:
            text += "\n기존 서비스를 먼저 중지하면 이 창에서 녹화를 시작할 수 있습니다. 이 창은 별도 서비스를 종료하지 않습니다."
        if state.get("error"):
            text += "\n" + state["error"]
        self.summary_text.set(text)
        rows = [(camera.get("client_no", ""), camera.get("intersection", ""), camera.get("direction", ""),
                 "녹화 중" if camera.get("name") in active else "대기" if running or external else "중지") for camera in cameras]
        old_rows = [tuple(self.camera_tree.item(item, "values")) for item in self.camera_tree.get_children()]
        if old_rows != [tuple(str(value) for value in row) for row in rows]:
            self.camera_tree.delete(*self.camera_tree.get_children())
            for row in rows:
                self.camera_tree.insert("", "end", values=row)
        weather = dict(state.get("weather") or {})
        # A manual forecast returns an enabled preview even with the saved rule
        # off; the UI must describe the saved recording rule, not that preview.
        weather["enabled"] = bool((self.baseline.get("weather_condition") or {}).get("enabled"))
        self.weather_text.set(desktop_forecast_text(weather, state.get("forecast_busy")))
        logs = "\n".join(state.get("logs", []))
        if logs != self.last_logs:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.insert("1.0", logs or "실행 기록이 없습니다. 녹화를 시작하면 여기에 표시합니다.")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
            self.last_logs = logs
        self._update_buttons()

    def request_close(self, force=False):
        if self.closing:
            return
        if self.pending_action:
            self.notice.set("진행 중인 작업이 끝난 뒤 창을 종료해 주세요.")
            return
        running = self.last_state.get("running", False)
        messages = []
        if running:
            messages.append("이 창에서 실행한 녹화를 중지하고 현재 영상 파일을 마무리합니다.")
        if self.dirty:
            messages.append("저장하지 않은 설정 변경은 버립니다.")
        if messages and not force and not self.messagebox.askyesno("녹화 도구 종료", "\n\n".join(messages) + "\n\n종료할까요?", parent=self.root):
            return
        self.closing = True
        self._set_busy(True)
        self.notice.set("녹화를 정상 종료하고 있습니다. 영상 파일을 마무리할 때까지 기다려 주세요.")
        self.jobs.put(("close", self.controller.close, self._closed))

    def _closed(self, _result):
        self.jobs.put(None)
        self.destroyed = True
        # Tk retains scheduled Tcl commands after widgets are destroyed. Cancel
        # this dedicated interpreter's timers before removing their callbacks.
        for pending in self.root.tk.call("after", "info"):
            self.root.after_cancel(pending)
        self.root.destroy()


def desktop_signal_watch(root, app):
    """Relay terminal/session termination to the Tk thread and finalize owned video."""
    stop_requested = threading.Event()
    previous = {}

    def receive(_number, _frame):
        # Python handlers may interrupt a Tk call; defer every GUI operation.
        stop_requested.set()

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            value = getattr(signal, name, None)
            if value is not None and value not in previous:
                previous[value] = signal.getsignal(value)
                signal.signal(value, receive)

    def poll():
        if app.destroyed:
            return
        if stop_requested.is_set() and not app.pending_action:
            stop_requested.clear()
            app.request_close(force=True)
        if not app.destroyed:
            root.after(100, poll)

    root.after(100, poll)

    def restore():
        for number, handler in previous.items():
            signal.signal(number, handler)

    return restore


def run_recorder_gui(config_path):
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        raise RuntimeError("데스크톱 GUI에 필요한 Tk가 없습니다. Ubuntu에서 sudo apt install python3-tk 명령으로 설치하세요. 브라우저 방식은 --ui를 사용하세요.") from None
    try:
        root = tk.Tk()
    except tk.TclError:
        raise RuntimeError("데스크톱 화면에 연결하지 못했습니다. Linux 데스크톱에서 실행하세요. SSH 접속에서는 --ui와 SSH 포트 전달을 사용하세요.") from None
    controller = RecorderUiController(config_path)
    app = None
    restore_signals = lambda: None
    try:
        app = RecorderDesktopApp(root, tk, ttk, (filedialog, messagebox), controller)
        restore_signals = desktop_signal_watch(root, app)
        root.mainloop()
    finally:
        restore_signals()
        # A keyboard interruption must also finalize owned MP4 files.
        if app is None or not app.closing:
            controller.close()
