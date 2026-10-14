# -*- coding: utf-8 -*-
"""Tkinter 图形界面：左侧浅蓝侧边栏 + 右侧白色功能页。

包含清晰度修复（DPI 感知 + 统一字体）、输出目录管理页、日志预览页。
计算在后台线程执行，通过队列把进度与日志传回主线程。
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from core.config import (
    BandConfig,
    Config,
    ConfigError,
    Molecule,
    NetworkConfig,
    OutputConfig,
    PathScanConfig,
    PathsConfig,
    SimulationConfig,
    SLIT_FUNCTIONS,
    ensure_config_exists,
    get_base_dir,
    get_config_path,
    get_data_dir,
    load_config,
    save_config,
    validate,
)
from core.fetcher import CancelledError
from core.logging_setup import capture_print_output, setup_logging


BASE_DIR = get_base_dir()
DATA_DIR = get_data_dir(BASE_DIR)
CONFIG_PATH = get_config_path(BASE_DIR)
LOG_DIR = DATA_DIR / "logs"

# 先建日志并接管 print，再导入 pipeline（其内部会 import hapi 并打印 banner）
logger = setup_logging(LOG_DIR)
capture_print_output(logger)

from core.pipeline import run_path_scan_pipeline, run_spectrum_pipeline  # noqa: E402

if DATA_DIR != BASE_DIR:
    logger.warning("程序目录不可写，配置/缓存/输出/日志将保存到 %s", DATA_DIR)


APP_VERSION = "1.2.0"
MAX_LOG_LINES = 5000

# 配色：浅蓝 + 白
BG_MAIN = "#ffffff"
BG_SIDEBAR = "#dce9f7"
BG_SIDEBAR_ACTIVE = "#ffffff"
ACCENT = "#2f6fb3"
BORDER = "#b7d0ea"
BUTTON_BG = "#e7f1fa"
BUTTON_ACTIVE = "#d4e6f7"
TEXT = "#202020"
SUBTLE = "#5a6b7d"


def enable_dpi_awareness() -> None:
    """在创建任何窗口前开启高 DPI 感知，避免界面被 Windows 拉伸而模糊。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _icon_path() -> Path:
    """窗口图标路径：源码模式取项目根目录，打包后取捆绑目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", str(BASE_DIR)))
        return base / "icon.ico"
    return BASE_DIR / "icon.ico"


def _parse_components(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError("成分格式应为 name=value: %s" % line)
        name, value = line.split("=", 1)
        out[name.strip()] = float(value.strip())
    return out


def _parse_molecules(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            raise ConfigError("分子格式应为 name,M,I: %s" % line)
        out.append(Molecule(parts[0], int(parts[1]), int(parts[2])))
    return out


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.running_mode = None
        self.last_run_dir: Path | None = None

        self.log_lines = []  # [(level, text), ...]
        self.log_filter_var = tk.StringVar(value="ALL")
        self.auto_scroll_var = tk.BooleanVar(value=True)
        self.output_dir_var = tk.StringVar(value="output")
        self.timestamped_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="就绪")
        self.vars = {}
        self._history_dirs = []

        root.title("HAPI 大气透过率仿真")
        root.geometry("1020x740")
        root.minsize(920, 660)

        self._setup_style()
        self._build_layout()
        self._load_config()
        capture_print_output(logger, extra=self._on_print)
        root.after(120, self._poll)

    # ---------------------------------------------------------------- style
    def _style_conf(self, widget: str, **opts) -> None:
        try:
            self.style.configure(widget, **opts)
        except Exception:
            pass

    def _setup_style(self) -> None:
        self.font = ("Microsoft YaHei UI", 10)
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self._style_conf("TFrame", background=BG_MAIN)
        self._style_conf("TLabel", background=BG_MAIN, foreground=TEXT)
        self._style_conf(
            "Heading.TLabel",
            background=BG_MAIN,
            foreground=ACCENT,
            font=("Microsoft YaHei UI", 13, "bold"),
        )
        self._style_conf(
            "TLabelframe",
            background=BG_MAIN,
            bordercolor=BORDER,
            relief="solid",
        )
        self._style_conf(
            "TLabelframe.Label",
            background=BG_MAIN,
            foreground=ACCENT,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self._style_conf(
            "TButton",
            background=BUTTON_BG,
            foreground=TEXT,
            bordercolor=BORDER,
            borderwidth=1,
            relief="flat",
            padding=(10, 5),
        )
        try:
            self.style.map(
                "TButton",
                background=[("active", BUTTON_ACTIVE), ("disabled", "#eef1f5")],
            )
        except Exception:
            pass
        self._style_conf(
            "TEntry",
            fieldbackground="#ffffff",
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
        )
        self._style_conf(
            "TCombobox",
            fieldbackground="#ffffff",
            background="#ffffff",
            foreground=TEXT,
            bordercolor=BORDER,
            arrowcolor=ACCENT,
        )
        self._style_conf("TCheckbutton", background=BG_MAIN, foreground=TEXT)
        self._style_conf("TRadiobutton", background=BG_MAIN, foreground=TEXT)
        self._style_conf(
            "TProgressbar", background=ACCENT, troughcolor="#e9f0f8"
        )
        self._style_conf(
            "TScrollbar", background="#c8d9ec", troughcolor="#eef4fa"
        )
        try:
            self.root.option_add("*Font", "{Microsoft YaHei UI} 10")
        except Exception:
            pass

    # ---------------------------------------------------------------- layout
    def _build_layout(self) -> None:
        self.status_label = tk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            bg=BG_SIDEBAR,
            fg=TEXT,
            padx=8,
            pady=3,
        )
        self.status_label.pack(side="bottom", fill="x")

        container = tk.Frame(self.root, bg=BG_MAIN)
        container.pack(side="top", fill="both", expand=True)

        self.sidebar = tk.Frame(container, bg=BG_SIDEBAR, width=168)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        separator = tk.Frame(container, bg=BORDER, width=1)
        separator.pack(side="left", fill="y")

        self.content = ttk.Frame(container, style="TFrame")
        self.content.pack(side="left", fill="both", expand=True)

        self.pages = {}
        self.nav_buttons = {}
        for key, label in (
            ("run", "波长-透过率"),
            ("pathscan", "光程-透过率"),
            ("output", "输出目录"),
            ("log", "日志预览"),
            ("about", "关于"),
        ):
            btn = tk.Button(
                self.sidebar,
                text=label,
                anchor="w",
                relief="flat",
                bd=0,
                padx=18,
                pady=11,
                font=self.font,
                cursor="hand2",
                bg=BG_SIDEBAR,
                fg=TEXT,
                activebackground=BG_SIDEBAR_ACTIVE,
                activeforeground=ACCENT,
                command=lambda k=key: self._show(k),
            )
            btn.pack(fill="x")
            self.nav_buttons[key] = btn

        self.pages["run"] = self._build_run_page(self.content)
        self.pages["pathscan"] = self._build_pathscan_page(self.content)
        self.pages["output"] = self._build_output_page(self.content)
        self.pages["log"] = self._build_log_page(self.content)
        self.pages["about"] = self._build_about_page(self.content)

        for page in self.pages.values():
            page.place(x=0, y=0, relwidth=1, relheight=1)
        self._show("run")

    def _show(self, name: str) -> None:
        self.pages[name].tkraise()
        for key, btn in self.nav_buttons.items():
            if key == name:
                btn.config(bg=BG_SIDEBAR_ACTIVE, fg=ACCENT)
            else:
                btn.config(bg=BG_SIDEBAR, fg=TEXT)
        if name == "output":
            self._refresh_output_history()
        if name == "run":
            self._refresh_run_summary()

    def _heading(self, parent, text: str) -> ttk.Label:
        label = ttk.Label(parent, text=text, style="Heading.TLabel")
        label.pack(anchor="w")
        return label

    def _make_scrollable(self, parent):
        canvas = tk.Canvas(parent, bg=BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas, style="TFrame")
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind(
            "<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
        )
        return canvas, inner

    def _bind_wheel_recursive(self, widget, handler) -> None:
        try:
            widget.bind("<MouseWheel>", handler)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind_wheel_recursive(child, handler)

    # ---------------------------------------------------------------- run page
    def _build_run_page(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=(14, 10))
        self._heading(f, "波长-透过率（透过率谱 T(λ)）")

        # 运行栏固定在最下方，与“光程-透过率”页保持一致，且窗口较小时也始终可见
        runbar = ttk.Labelframe(f, text="运行", padding=8)
        runbar.pack(side="bottom", fill="x", pady=(6, 0))
        self.run_summary = ttk.Label(
            runbar, text="", foreground=SUBTLE, justify="left"
        )
        self.run_summary.pack(anchor="w", fill="x")
        self.progress = ttk.Progressbar(runbar, mode="indeterminate")
        self.progress.pack(fill="x", pady=(6, 4))
        row = ttk.Frame(runbar)
        row.pack(anchor="w")
        self.btn_run = ttk.Button(row, text="开始计算", command=self._start_spectrum)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(
            row, text="取消", command=self._cancel, state="disabled"
        )
        self.btn_cancel.pack(side="left", padx=8)
        ttk.Button(row, text="保存配置", command=self._save_config).pack(
            side="left", padx=8
        )
        ttk.Button(row, text="打开输出目录", command=self._open_output_dir).pack(
            side="left"
        )

        # 参数区放入可滚动区域
        canvas, inner = self._make_scrollable(f)
        self._build_run_params(inner)
        self._bind_wheel_recursive(
            inner, lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
        )
        return f

    def _build_run_params(self, parent) -> None:
        sim = ttk.Labelframe(parent, text="仿真参数", padding=8)
        sim.pack(fill="x", pady=(0, 6))
        fields = [
            ("wavelength_min_um", "波长下限 (μm)", "8.9"),
            ("wavelength_max_um", "波长上限 (μm)", "11.0"),
            ("wavenumber_step", "计算步长 (cm⁻¹)", "0.01"),
            ("instrument_resolution", "仪器分辨率 (cm⁻¹)", "0.5"),
            ("path_length_cm", "光程 (cm)", "1000000"),
            ("temperature_k", "温度 (K)", "296"),
            ("pressure_atm", "压强 (atm)", "1.0"),
            ("wavenumber_wing", "谱线翼截断 (cm⁻¹)", "25"),
            ("tau_line_min", "最小光学厚度 τ", "0.0001"),
        ]
        for i, (key, label, default) in enumerate(fields):
            r, c = divmod(i, 2)
            self._add_field(sim, r, c, key, label, default)
        self.vars["slit_function"] = tk.StringVar(value="gaussian")
        ttk.Label(sim, text="狭缝函数").grid(
            row=5, column=0, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Combobox(
            sim,
            textvariable=self.vars["slit_function"],
            values=list(SLIT_FUNCTIONS.keys()),
            state="readonly",
            width=14,
        ).grid(row=5, column=1, sticky="w", pady=4)

        mid = ttk.Frame(parent)
        mid.pack(fill="x", pady=4)
        left = ttk.Labelframe(mid, text="成分体积混合比（name=value）")
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.components_text = self._make_text(left)
        self.components_text.pack(fill="both", expand=True)

        right = ttk.Labelframe(mid, text="分子清单（name,M,I）")
        right.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.molecules_text = self._make_text(right)
        self.molecules_text.pack(fill="both", expand=True)

        net = ttk.Labelframe(parent, text="网络与 API Key", padding=8)
        net.pack(fill="x", pady=4)
        self.vars["use_proxy"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(net, text="启用代理", variable=self.vars["use_proxy"]).grid(
            row=0, column=0, sticky="w"
        )
        self.vars["proxy"] = tk.StringVar(value="http://127.0.0.1:7890")
        ttk.Label(net, text="代理地址").grid(row=0, column=1, sticky="e", padx=(14, 4))
        ttk.Entry(net, textvariable=self.vars["proxy"], width=30).grid(
            row=0, column=2, sticky="we"
        )
        self.vars["api_key"] = tk.StringVar(value="")
        ttk.Label(net, text="API Key").grid(row=1, column=1, sticky="e", padx=(14, 4))
        ttk.Entry(net, textvariable=self.vars["api_key"], width=30).grid(
            row=1, column=2, sticky="we", pady=4
        )
        net.columnconfigure(2, weight=1)

    def _make_text(self, parent) -> tk.Text:
        return tk.Text(
            parent,
            height=5,
            width=38,
            font=self.font,
            bg="white",
            fg=TEXT,
            insertbackground=TEXT,
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )

    def _add_field(self, parent, row, col, key, label, default) -> None:
        self.vars[key] = tk.StringVar(value=default)
        ttk.Label(parent, text=label).grid(
            row=row, column=col * 2, sticky="w", padx=(0, 6), pady=4
        )
        ttk.Entry(parent, textvariable=self.vars[key], width=20).grid(
            row=row, column=col * 2 + 1, sticky="we", pady=4
        )
        parent.columnconfigure(col * 2 + 1, weight=1)

    def _refresh_run_summary(self) -> None:
        try:
            cfg = self._read_form_config()
        except ConfigError as exc:
            self.run_summary.config(text="参数有误：%s" % exc)
            return
        self.run_summary.config(
            text=(
                "波段 %.3f–%.3f μm（%.2f–%.2f cm⁻¹）  ·  分辨率 %.3g cm⁻¹  ·  "
                "光程 %.0f m  ·  T=%.1f K  ·  p=%.3g atm  ·  输出：%s"
                % (
                    cfg.band.wavelength_min_um,
                    cfg.band.wavelength_max_um,
                    cfg.wavenumber_min,
                    cfg.wavenumber_max,
                    cfg.simulation.instrument_resolution,
                    cfg.simulation.path_length_cm / 100.0,
                    cfg.simulation.temperature_k,
                    cfg.simulation.pressure_atm,
                    self._resolve_output_path(),
                )
            )
        )

    # ---------------------------------------------------------------- path scan page
    def _build_pathscan_page(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=16)
        self._heading(f, "光程-透过率（全波段 T(λ, L)）")

        self.pathscan_vars = {}

        laser = ttk.Labelframe(f, text="激光参数", padding=10)
        laser.pack(fill="x", pady=6)
        self.pathscan_vars["linewidth"] = tk.StringVar(value="0.01")
        ttk.Label(laser, text="激光线宽 FWHM (cm⁻¹)").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(laser, textvariable=self.pathscan_vars["linewidth"], width=18).grid(
            row=0, column=1, sticky="w", padx=8
        )
        ttk.Label(
            laser,
            text="线型：高斯（默认）。1 cm⁻¹ ≈ 30 GHz；在 10 μm 处约对应 0.01 μm。",
            foreground=SUBTLE,
        ).grid(row=0, column=2, sticky="w", padx=12)

        scan = ttk.Labelframe(f, text="扫描范围", padding=10)
        scan.pack(fill="x", pady=6)
        self.pathscan_vars["wavelength_points"] = tk.StringVar(value="200")
        self.pathscan_vars["distance_min"] = tk.StringVar(value="0")
        self.pathscan_vars["distance_max"] = tk.StringVar(value="1000000")
        self.pathscan_vars["distance_points"] = tk.StringVar(value="200")
        self.pathscan_vars["scale"] = tk.StringVar(value="linear")

        rows = [
            ("波长采样点数", "wavelength_points", 10),
            ("距离下限 (cm)", "distance_min", 14),
            ("距离上限 (cm)", "distance_max", 14),
            ("距离点数", "distance_points", 10),
        ]
        for r, (label, key, width) in enumerate(rows):
            ttk.Label(scan, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(
                scan, textvariable=self.pathscan_vars[key], width=width
            ).grid(row=r, column=1, sticky="w", pady=4)
        ttk.Label(scan, text="距离轴刻度").grid(
            row=4, column=0, sticky="w", pady=4
        )
        ttk.Combobox(
            scan,
            textvariable=self.pathscan_vars["scale"],
            values=("linear", "log"),
            state="readonly",
            width=10,
        ).grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(
            f,
            text=(
                "说明：对整个波段等距采样激光中心波长，用高斯线宽对高分辨吸收系数加权，"
                "按 T(L)=∫g(ν)·exp(-k(ν)·L)dν 计算每个波长在不同距离下的透过率，"
                "输出 CSV 矩阵与热图 PNG。"
            ),
            foreground=SUBTLE,
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        runbar = ttk.Labelframe(f, text="运行", padding=8)
        runbar.pack(fill="x", pady=(10, 0))
        self.pathscan_progress = ttk.Progressbar(runbar, mode="indeterminate")
        self.pathscan_progress.pack(fill="x", pady=(0, 4))
        row = ttk.Frame(runbar)
        row.pack(anchor="w")
        self.btn_pathscan_run = ttk.Button(
            row, text="开始计算", command=self._start_pathscan
        )
        self.btn_pathscan_run.pack(side="left")
        self.btn_pathscan_cancel = ttk.Button(
            row, text="取消", command=self._cancel, state="disabled"
        )
        self.btn_pathscan_cancel.pack(side="left", padx=8)
        ttk.Button(row, text="保存配置", command=self._save_config).pack(
            side="left", padx=8
        )
        ttk.Button(row, text="打开输出目录", command=self._open_output_dir).pack(
            side="left"
        )
        return f

    # ---------------------------------------------------------------- output page
    def _build_output_page(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=16)
        self._heading(f, "输出目录")

        row = ttk.Frame(f)
        row.pack(fill="x", pady=(10, 4))
        ttk.Label(row, text="输出目录").pack(side="left")
        ttk.Entry(row, textvariable=self.output_dir_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(row, text="浏览…", command=self._browse_output).pack(side="left")

        ttk.Checkbutton(
            f, text="结果按时间戳分目录", variable=self.timestamped_var
        ).pack(anchor="w", pady=4)

        btns = ttk.Frame(f)
        btns.pack(anchor="w", pady=4)
        ttk.Button(btns, text="检查可写", command=self._check_output_writable).pack(
            side="left"
        )
        ttk.Button(btns, text="保存配置", command=self._save_config).pack(
            side="left", padx=8
        )
        ttk.Button(btns, text="打开输出目录", command=self._open_output_dir).pack(
            side="left", padx=8
        )
        ttk.Button(btns, text="刷新", command=self._refresh_output_history).pack(
            side="left"
        )

        ttk.Label(f, text="最近运行结果（双击打开）").pack(anchor="w", pady=(14, 2))
        self.history = tk.Listbox(
            f,
            font=self.font,
            height=8,
            bg="white",
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground="white",
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.history.pack(fill="both", expand=True)
        self.history.bind("<Double-Button-1>", lambda e: self._open_selected_history())
        return f

    def _resolve_output_path(self) -> Path:
        value = self.output_dir_var.get().strip() or "output"
        p = Path(value)
        if not p.is_absolute():
            p = DATA_DIR / p
        return p

    def _browse_output(self) -> None:
        initial = self._resolve_output_path()
        if not initial.exists():
            initial = DATA_DIR
        chosen = filedialog.askdirectory(initialdir=str(initial))
        if chosen:
            self.output_dir_var.set(chosen)
            self._refresh_output_history()

    def _check_output_writable(self) -> None:
        p = self._resolve_output_path()
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_test.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            messagebox.showinfo("可写", "目录可写：\n%s" % p)
        except Exception as exc:
            messagebox.showerror("不可写", "目录不可写：\n%s\n\n%s" % (p, exc))

    def _refresh_output_history(self) -> None:
        p = self._resolve_output_path()
        self.history.delete(0, "end")
        self._history_dirs = []
        if not p.exists():
            return
        dirs = [d for d in p.iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[:30]:
            self.history.insert("end", d.name)
            self._history_dirs.append(d)

    def _open_selected_history(self) -> None:
        selection = self.history.curselection()
        if selection and selection[0] < len(self._history_dirs):
            os.startfile(str(self._history_dirs[selection[0]]))

    def _open_output_dir(self) -> None:
        target = self.last_run_dir or self._resolve_output_path()
        if target.exists():
            os.startfile(str(target))
        else:
            messagebox.showinfo("提示", "输出目录尚不存在：\n%s" % target)

    # ---------------------------------------------------------------- log page
    def _build_log_page(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=16)
        self._heading(f, "日志预览")

        head = ttk.Frame(f)
        head.pack(fill="x")
        for text, value in (
            ("全部", "ALL"),
            ("INFO", "INFO"),
            ("WARNING", "WARNING"),
            ("ERROR", "ERROR"),
        ):
            ttk.Radiobutton(
                head,
                text=text,
                value=value,
                variable=self.log_filter_var,
                command=self._render_log,
            ).pack(side="left", padx=(0, 4))
        ttk.Checkbutton(head, text="自动滚动", variable=self.auto_scroll_var).pack(
            side="left", padx=8
        )
        ttk.Button(head, text="清空显示", command=self._clear_log).pack(side="right")
        ttk.Button(head, text="打开日志文件", command=self._open_log_file).pack(
            side="right", padx=6
        )

        self.log_text = scrolledtext.ScrolledText(
            f,
            state="disabled",
            font=self.font,
            wrap="word",
            bg="white",
            fg=TEXT,
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text.tag_configure("info", foreground="#000000")
        self.log_text.tag_configure("warning", foreground="#c77700")
        self.log_text.tag_configure("error", foreground="#d32f2f")
        self.log_text.tag_configure("critical", foreground="#b00020")
        return f

    def _level_tag(self, level: int) -> str:
        if level >= logging.CRITICAL:
            return "critical"
        if level >= logging.ERROR:
            return "error"
        if level >= logging.WARNING:
            return "warning"
        return "info"

    def _level_matches(self, level: int) -> bool:
        current = self.log_filter_var.get()
        if current == "ALL":
            return True
        threshold = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}[
            current
        ]
        return level >= threshold

    def _append_log(self, level: int, message: str) -> None:
        self.log_lines.append((level, message))
        if len(self.log_lines) > MAX_LOG_LINES:
            self.log_lines = self.log_lines[-MAX_LOG_LINES:]
            self._render_log()
            return
        if self._level_matches(level):
            self._insert_line(level, message)

    def _insert_line(self, level: int, message: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n", self._level_tag(level))
        if self.auto_scroll_var.get():
            self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _render_log(self) -> None:
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        for level, message in self.log_lines[-MAX_LOG_LINES:]:
            if self._level_matches(level):
                self.log_text.insert("end", message + "\n", self._level_tag(level))
        if self.auto_scroll_var.get():
            self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self) -> None:
        self.log_lines = []
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _open_log_file(self) -> None:
        logs = sorted(LOG_DIR.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            os.startfile(str(logs[0]))
        else:
            messagebox.showinfo("提示", "还没有日志文件。")

    # ---------------------------------------------------------------- about page
    def _build_about_page(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, padding=20)
        ttk.Label(
            f, text="HAPI 大气透过率仿真", font=("Microsoft YaHei UI", 15, "bold")
        ).pack(anchor="w")
        ttk.Label(f, text="版本 %s" % APP_VERSION).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            f,
            text="基于 HITRAN 数据库与 HAPI 库，计算指定波段的大气透过率曲线。",
            wraplength=620,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            f,
            text=(
                "引用：R.V. Kochanov, I.E. Gordon, L.S. Rothman, P. Wcislo, "
                "C. Hill, J.S. Wilzewski, HITRAN Application Programming Interface "
                "(HAPI): A comprehensive approach to working with spectroscopic data, "
                "J. Quant. Spectrosc. Radiat. Transfer 177, 15-30 (2016). "
                "DOI: 10.1016/j.jqsrt.2016.03.005"
            ),
            wraplength=620,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(14, 0))
        ttk.Button(row, text="打开程序目录", command=lambda: os.startfile(str(BASE_DIR))).pack(
            side="left"
        )
        ttk.Button(row, text="打开配置文件", command=self._open_config_file).pack(
            side="left", padx=8
        )
        return f

    def _open_config_file(self) -> None:
        if CONFIG_PATH.exists():
            os.startfile(str(CONFIG_PATH))
        else:
            messagebox.showinfo("提示", "配置文件不存在：\n%s" % CONFIG_PATH)

    # ---------------------------------------------------------------- config
    def _load_config(self) -> None:
        ensure_config_exists(CONFIG_PATH)
        try:
            cfg = load_config(CONFIG_PATH)
        except ConfigError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self._populate_form(cfg)
        self._refresh_run_summary()
        self._refresh_output_history()
        self._log("已加载配置：%s" % CONFIG_PATH)

    def _populate_form(self, cfg: Config) -> None:
        self.vars["wavelength_min_um"].set(str(cfg.band.wavelength_min_um))
        self.vars["wavelength_max_um"].set(str(cfg.band.wavelength_max_um))
        s = cfg.simulation
        self.vars["wavenumber_step"].set(str(s.wavenumber_step))
        self.vars["instrument_resolution"].set(str(s.instrument_resolution))
        self.vars["path_length_cm"].set(str(s.path_length_cm))
        self.vars["temperature_k"].set(str(s.temperature_k))
        self.vars["pressure_atm"].set(str(s.pressure_atm))
        self.vars["wavenumber_wing"].set(str(s.wavenumber_wing))
        self.vars["tau_line_min"].set(str(s.tau_line_min))
        self.vars["slit_function"].set(s.slit_function)
        self.vars["use_proxy"].set(cfg.network.use_proxy)
        self.vars["proxy"].set(cfg.network.proxy)
        self.vars["api_key"].set(cfg.network.api_key)
        self.output_dir_var.set(cfg.paths.output_dir)
        self.timestamped_var.set(cfg.output.timestamped_subdir)

        self.components_text.delete("1.0", "end")
        self.components_text.insert(
            "1.0", "\n".join("%s=%s" % (k, v) for k, v in cfg.components.items())
        )
        self.molecules_text.delete("1.0", "end")
        self.molecules_text.insert(
            "1.0", "\n".join("%s,%d,%d" % (m.name, m.M, m.I) for m in cfg.molecules)
        )

        ps = cfg.path_scan
        self.pathscan_vars["linewidth"].set(str(ps.laser_linewidth_cm1))
        self.pathscan_vars["wavelength_points"].set(str(ps.wavelength_points))
        self.pathscan_vars["distance_min"].set(str(ps.distance_min_cm))
        self.pathscan_vars["distance_max"].set(str(ps.distance_max_cm))
        self.pathscan_vars["distance_points"].set(str(ps.distance_points))
        self.pathscan_vars["scale"].set(ps.distance_scale)

    def _read_form_config(self) -> Config:
        def num(key):
            try:
                return float(self.vars[key].get())
            except (ValueError, KeyError):
                raise ConfigError("字段 %s 不是数字" % key)

        def num_ps(key):
            try:
                return float(self.pathscan_vars[key].get())
            except (ValueError, KeyError):
                raise ConfigError("光程曲线字段 %s 不是数字" % key)

        def int_ps(key):
            try:
                return int(self.pathscan_vars[key].get())
            except (ValueError, KeyError):
                raise ConfigError("光程曲线字段 %s 不是整数" % key)

        cfg = Config(
            band=BandConfig(
                wavelength_min_um=num("wavelength_min_um"),
                wavelength_max_um=num("wavelength_max_um"),
            ),
            simulation=SimulationConfig(
                wavenumber_step=num("wavenumber_step"),
                instrument_resolution=num("instrument_resolution"),
                path_length_cm=num("path_length_cm"),
                temperature_k=num("temperature_k"),
                pressure_atm=num("pressure_atm"),
                wavenumber_wing=num("wavenumber_wing"),
                tau_line_min=num("tau_line_min"),
                slit_function=self.vars["slit_function"].get(),
            ),
            molecules=_parse_molecules(self.molecules_text.get("1.0", "end")),
            components=_parse_components(self.components_text.get("1.0", "end")),
            diluent={"air": 1.0},
            network=NetworkConfig(
                use_proxy=self.vars["use_proxy"].get(),
                proxy=self.vars["proxy"].get(),
                api_key=self.vars["api_key"].get(),
            ),
            paths=PathsConfig(
                cache_dir="hitran_data_cache",
                output_dir=self.output_dir_var.get(),
            ),
            output=OutputConfig(
                timestamped_subdir=self.timestamped_var.get(),
            ),
            path_scan=PathScanConfig(
                laser_linewidth_cm1=num_ps("linewidth"),
                linewidth_shape="gaussian",
                wavelength_points=int_ps("wavelength_points"),
                distance_min_cm=num_ps("distance_min"),
                distance_max_cm=num_ps("distance_max"),
                distance_points=int_ps("distance_points"),
                distance_scale=self.pathscan_vars["scale"].get(),
            ),
        )
        validate(cfg)
        return cfg

    def _save_config(self) -> None:
        try:
            cfg = self._read_form_config()
            save_config(cfg, CONFIG_PATH)
        except ConfigError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self._refresh_run_summary()
        self._log("配置已保存：%s" % CONFIG_PATH)
        messagebox.showinfo("已保存", "配置已保存到：\n%s" % CONFIG_PATH)

    # ---------------------------------------------------------------- run
    def _start_spectrum(self) -> None:
        self._start_worker(
            run_spectrum_pipeline, "spectrum", "开始计算波长-透过率光谱"
        )

    def _start_pathscan(self) -> None:
        self._start_worker(
            run_path_scan_pipeline, "pathscan", "开始计算光程-透过率矩阵 T(λ, L)"
        )

    def _start_worker(self, pipeline_fn, mode: str, label: str) -> None:
        if self.running:
            return
        try:
            cfg = self._read_form_config()
            save_config(cfg, CONFIG_PATH)
        except ConfigError as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        self.running = True
        self.running_mode = mode
        self.cancel_event.clear()
        self.last_run_dir = None
        if mode == "spectrum":
            self.btn_run.config(state="disabled")
            self.btn_cancel.config(state="normal")
            self.progress.start(12)
        else:
            self.btn_pathscan_run.config(state="disabled")
            self.btn_pathscan_cancel.config(state="normal")
            self.pathscan_progress.start(12)
        self.status_var.set("运行中…")
        self._log("=" * 60)
        self._log(label)
        threading.Thread(
            target=self._worker, args=(pipeline_fn, cfg), daemon=True
        ).start()

    def _worker(self, pipeline_fn, cfg: Config) -> None:
        try:
            run_dir, files = pipeline_fn(cfg, self._log, self.cancel_event)
            self.q.put(("done", run_dir))
        except CancelledError as exc:
            self.q.put(("cancelled", str(exc)))
        except Exception:
            self.q.put(("error", traceback.format_exc()))
        finally:
            self.q.put(("stopped", None))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self._log("正在取消…")

    def _log(self, message: str, level: int = logging.INFO) -> None:
        self.q.put(("log", level, message))
        logger.log(level, message)

    def _on_print(self, level: int, message: str) -> None:
        self.q.put(("log", level, message))

    # ---------------------------------------------------------------- queue
    def _poll(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1], item[2])
                elif kind == "done":
                    self._on_done(item[1])
                elif kind == "cancelled":
                    self._log("已取消：%s" % item[1])
                elif kind == "error":
                    self._on_error(item[1])
                elif kind == "stopped":
                    self._on_stopped()
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _on_done(self, run_dir: Path) -> None:
        self.last_run_dir = Path(run_dir)
        self.status_var.set("完成：%s" % run_dir)
        self._log("仿真完成，输出目录：%s" % run_dir)
        messagebox.showinfo("完成", "计算完成。\n输出目录：\n%s" % run_dir)

    def _on_error(self, tb: str) -> None:
        self.status_var.set("运行失败")
        self._log(tb, logging.ERROR)
        logger.error(tb)
        messagebox.showerror("运行失败", "计算过程中出错，详情见“日志预览”页与 logs 目录。")

    def _on_stopped(self) -> None:
        self.running = False
        self.progress.stop()
        self.pathscan_progress.stop()
        self.btn_run.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.btn_pathscan_run.config(state="normal")
        self.btn_pathscan_cancel.config(state="disabled")
        self.running_mode = None
        if self.status_var.get().startswith("运行中"):
            self.status_var.set("就绪")


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    icon = _icon_path()
    if icon.exists():
        try:
            root.iconbitmap(str(icon))
        except Exception:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
