# -*- coding: utf-8 -*-
"""PySide6 图形界面：左侧导航 + 右侧磨砂玻璃功能页。

核心计算（core/）保持不变，这里只负责界面。背景图铺满右侧，功能面板用
rgba 半透明白色覆盖，实现“通透”效果。
"""

from __future__ import annotations

import html
import logging
import os
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

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


APP_VERSION = "1.2.1"
MAX_LOG_LINES = 5000

BG_MAIN = "#ffffff"
ACCENT = "#2f6fb3"
TEXT = "#202020"
SUBTLE = "#5a6b7d"
BG_WHITE_RATIO = 0.30  # 背景图洗白比例：越大越白、越浅


def _icon_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", str(BASE_DIR))) / "icon.ico"
    return BASE_DIR / "icon.ico"


def _background_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", str(BASE_DIR))) / "background.png"
    return BASE_DIR / "background.png"


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


APP_QSS = """
QWidget { font-family: "Microsoft YaHei UI"; font-size: 10pt; color: #202020; }
QMainWindow { background: #ffffff; }
QPushButton {
    background: rgba(231,241,250,0.85);
    border: 1px solid #b7d0ea;
    border-radius: 4px;
    padding: 6px 12px;
}
QPushButton:hover { background: #d4e6f7; }
QPushButton:disabled { background: #eef1f5; color: #9aa7b4; }
QLineEdit, QTextEdit, QListWidget {
    background: transparent;
    border: 1px solid #b7d0ea;
    border-radius: 4px;
    padding: 4px;
}
QComboBox {
    background: transparent;
    border: 1px solid #b7d0ea;
    border-radius: 4px;
    padding: 4px 8px;
}
QGroupBox {
    background-color: transparent;
    border: 1px solid #b7d0ea;
    border-radius: 8px;
    margin-top: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #2f6fb3;
}
QProgressBar {
    border: 1px solid #b7d0ea;
    border-radius: 4px;
    background: #e9f0f8;
    text-align: center;
}
QProgressBar::chunk { background: #2f6fb3; border-radius: 3px; }
"""


class BackgroundWidget(QWidget):
    """右侧内容区底图：等比裁切铺满，并叠加白色洗白层。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        path = _background_path()
        self._pixmap = QPixmap(str(path)) if path.exists() else QPixmap()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BG_MAIN))
        if not self._pixmap.isNull():
            painter.drawPixmap(self._cover_rect(), self._pixmap)
            painter.setOpacity(BG_WHITE_RATIO)
            painter.fillRect(self.rect(), QColor("#ffffff"))
            painter.setOpacity(1.0)
        painter.end()

    def _cover_rect(self) -> QRect:
        w, h = self.width(), self.height()
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if w <= 0 or h <= 0 or pw <= 0 or ph <= 0:
            return self.rect()
        scale = max(w / pw, h / ph)
        nw, nh = int(pw * scale), int(ph * scale)
        return QRect((w - nw) // 2, (h - nh) // 2, nw, nh)


class PipelineWorker(QThread):
    done = Signal(str)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(self, fn, cfg: Config, log_fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cfg = cfg
        self.log_fn = log_fn
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        def log(message: str):
            self.log_fn(logging.INFO, message)

        try:
            run_dir, _files = self.fn(self.cfg, log, self._cancel)
            self.done.emit(str(run_dir))
        except CancelledError as exc:
            self.cancelled.emit(str(exc))
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    log_signal = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("HAPI 大气透过率仿真")
        self.resize(1100, 780)
        self.setMinimumSize(980, 680)

        self.worker = None
        self.running_mode = None
        self.last_run_dir = None
        self.log_lines = []
        self.vars = {}
        self.pathscan_vars = {}

        self._build_ui()
        self._load_config()
        capture_print_output(logger, extra=self._on_print)
        self.log_signal.connect(self._append_log)

    # ---------------------------------------------------------------- ui
    def _build_ui(self):
        central = BackgroundWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setFixedWidth(172)
        sidebar.setStyleSheet(
            "QFrame { background: #dce9f7; }"
            "QPushButton { text-align: left; padding: 12px 18px; border: none;"
            " background: transparent; color: #202020; }"
            "QPushButton:hover { background: rgba(255,255,255,0.5); }"
            "QPushButton:checked { background: #ffffff; color: #2f6fb3; font-weight: 600; }"
        )
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 12, 0, 12)
        side.setSpacing(4)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_keys = []
        for idx, (key, label) in enumerate(
            (
                ("run", "波长-透过率"),
                ("pathscan", "光程-透过率"),
                ("output", "输出目录"),
                ("log", "日志预览"),
                ("about", "关于"),
            )
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            self.nav_group.addButton(btn, idx)
            self.nav_keys.append(key)
            side.addWidget(btn)
        side.addStretch(1)
        self.nav_group.idClicked.connect(self._on_nav)
        self.nav_group.button(0).setChecked(True)

        self.pages = QStackedWidget()
        self.pages.setStyleSheet("QStackedWidget { background: transparent; }")
        self.pages.addWidget(self._build_run_page())
        self.pages.addWidget(self._build_pathscan_page())
        self.pages.addWidget(self._build_output_page())
        self.pages.addWidget(self._build_log_page())
        self.pages.addWidget(self._build_about_page())

        root.addWidget(sidebar)
        root.addWidget(self.pages, 1)
        self.statusBar().showMessage("就绪")

    def _new_page(self) -> QFrame:
        page = QFrame()
        page.setObjectName("page")
        page.setStyleSheet(
            "#page { background: rgba(255,255,255,0.86); border-radius: 12px; }"
        )
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        return page

    def _page_title(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-size: 14pt; font-weight: 700; color: #2f6fb3;")
        return label

    def _group(self, title: str) -> QGroupBox:
        """创建真正支持半透明背景的分组框（样式直接写在控件上，避免继承差异）。"""
        gb = QGroupBox(title)
        gb.setAttribute(Qt.WA_StyledBackground, True)
        gb.setStyleSheet(
            "QGroupBox {"
            " background-color: transparent;"
            " border: 1px solid #b7d0ea;"
            " border-radius: 8px;"
            " margin-top: 10px;"
            " font-weight: 600;"
            "}"
            "QGroupBox::title {"
            " subcontrol-origin: margin;"
            " left: 10px;"
            " padding: 0 4px;"
            " color: #2f6fb3;"
            "}"
        )
        return gb

    def _build_runbar(self, mode: str) -> QGroupBox:
        gb = self._group("运行")
        v = QVBoxLayout(gb)
        if mode == "spec":
            self.summary_spec = QLabel("")
            self.summary_spec.setStyleSheet("color: #5a6b7d;")
            v.addWidget(self.summary_spec)
            self.progress_spec = QProgressBar()
            self.progress_spec.setRange(0, 0)
            self.progress_spec.hide()
            v.addWidget(self.progress_spec)
            row = QHBoxLayout()
            self.btn_run = QPushButton("开始计算")
            self.btn_run.clicked.connect(
                lambda: self._start_worker(
                    run_spectrum_pipeline, "spec", "开始计算波长-透过率光谱"
                )
            )
            self.btn_cancel = QPushButton("取消")
            self.btn_cancel.clicked.connect(self._cancel)
            self.btn_cancel.setEnabled(False)
            row.addWidget(self.btn_run)
            row.addWidget(self.btn_cancel)
        else:
            self.progress_ps = QProgressBar()
            self.progress_ps.setRange(0, 0)
            self.progress_ps.hide()
            v.addWidget(self.progress_ps)
            row = QHBoxLayout()
            self.btn_run_ps = QPushButton("开始计算")
            self.btn_run_ps.clicked.connect(
                lambda: self._start_worker(
                    run_path_scan_pipeline, "pathscan", "开始计算光程-透过率矩阵 T(λ, L)"
                )
            )
            self.btn_cancel_ps = QPushButton("取消")
            self.btn_cancel_ps.clicked.connect(self._cancel)
            self.btn_cancel_ps.setEnabled(False)
            row.addWidget(self.btn_run_ps)
            row.addWidget(self.btn_cancel_ps)

        save_btn = QPushButton("保存配置")
        save_btn.clicked.connect(self._save_config)
        open_btn = QPushButton("打开输出目录")
        open_btn.clicked.connect(self._open_output_dir)
        row.addWidget(save_btn)
        row.addWidget(open_btn)
        row.addStretch(1)
        v.addLayout(row)
        return gb

    # ---------------------------------------------------------------- pages
    def _build_run_page(self) -> QFrame:
        page = self._new_page()
        lay = page.layout()
        lay.addWidget(self._page_title("波长-透过率（透过率谱 T(λ)）"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(0, 0, 8, 0)

        sim = self._group("仿真参数")
        grid = QGridLayout(sim)
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
            self.vars[key] = QLineEdit(default)
            grid.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            grid.addWidget(self.vars[key], i // 2, (i % 2) * 2 + 1)
        self.vars["slit_function"] = QComboBox()
        self.vars["slit_function"].addItems(list(SLIT_FUNCTIONS.keys()))
        grid.addWidget(QLabel("狭缝函数"), 5, 0)
        grid.addWidget(self.vars["slit_function"], 5, 1)
        inner_lay.addWidget(sim)

        mid = QHBoxLayout()
        comp = self._group("成分体积混合比（name=value）")
        comp_lay = QVBoxLayout(comp)
        self.components_text = QTextEdit()
        self.components_text.viewport().setAutoFillBackground(False)
        comp_lay.addWidget(self.components_text)
        mol = self._group("分子清单（name,M,I）")
        mol_lay = QVBoxLayout(mol)
        self.molecules_text = QTextEdit()
        self.molecules_text.viewport().setAutoFillBackground(False)
        mol_lay.addWidget(self.molecules_text)
        mid.addWidget(comp)
        mid.addWidget(mol)
        inner_lay.addLayout(mid)

        net = self._group("网络与 API Key")
        ng = QGridLayout(net)
        self.vars["use_proxy"] = QCheckBox("启用代理")
        self.vars["proxy"] = QLineEdit("http://127.0.0.1:7890")
        self.vars["api_key"] = QLineEdit("")
        ng.addWidget(self.vars["use_proxy"], 0, 0)
        ng.addWidget(QLabel("代理地址"), 0, 1)
        ng.addWidget(self.vars["proxy"], 0, 2)
        ng.addWidget(QLabel("API Key"), 1, 1)
        ng.addWidget(self.vars["api_key"], 1, 2)
        inner_lay.addWidget(net)

        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        lay.addWidget(self._build_runbar("spec"))
        return page

    def _build_pathscan_page(self) -> QFrame:
        page = self._new_page()
        lay = page.layout()
        lay.addWidget(self._page_title("光程-透过率（全波段 T(λ, L)）"))

        laser = self._group("激光参数")
        lr = QHBoxLayout(laser)
        lr.addWidget(QLabel("激光线宽 FWHM (cm⁻¹)"))
        self.pathscan_vars["linewidth"] = QLineEdit("0.01")
        lr.addWidget(self.pathscan_vars["linewidth"])
        hint = QLabel("线型：高斯（默认）。1 cm⁻¹ ≈ 30 GHz；在 10 μm 处约对应 0.01 μm。")
        hint.setStyleSheet("color: #5a6b7d;")
        lr.addWidget(hint, 1)
        lay.addWidget(laser)

        scan = self._group("扫描范围")
        sg = QGridLayout(scan)
        self.pathscan_vars["wavelength_points"] = QLineEdit("200")
        self.pathscan_vars["distance_min"] = QLineEdit("0")
        self.pathscan_vars["distance_max"] = QLineEdit("1000000")
        self.pathscan_vars["distance_points"] = QLineEdit("200")
        self.pathscan_vars["scale"] = QComboBox()
        self.pathscan_vars["scale"].addItems(["linear", "log"])
        rows = [
            ("波长采样点数", "wavelength_points"),
            ("距离下限 (cm)", "distance_min"),
            ("距离上限 (cm)", "distance_max"),
            ("距离点数", "distance_points"),
        ]
        for r, (label, key) in enumerate(rows):
            sg.addWidget(QLabel(label), r, 0)
            sg.addWidget(self.pathscan_vars[key], r, 1)
        sg.addWidget(QLabel("距离轴刻度"), 4, 0)
        sg.addWidget(self.pathscan_vars["scale"], 4, 1)
        lay.addWidget(scan)

        desc = QLabel(
            "对整个波段等距采样激光中心波长，用高斯线宽对高分辨吸收系数加权，"
            "按 T(L)=∫g(ν)·exp(-k(ν)·L)dν 计算每个波长在不同距离下的透过率，"
            "输出 CSV 矩阵与热图 PNG。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #5a6b7d;")
        lay.addWidget(desc)
        lay.addStretch(1)
        lay.addWidget(self._build_runbar("pathscan"))
        return page

    def _build_output_page(self) -> QFrame:
        page = self._new_page()
        lay = page.layout()
        lay.addWidget(self._page_title("输出目录"))

        row = QHBoxLayout()
        row.addWidget(QLabel("输出目录"))
        self.output_dir_var = QLineEdit("output")
        row.addWidget(self.output_dir_var, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_output)
        row.addWidget(browse)
        lay.addLayout(row)

        self.timestamped_var = QCheckBox("结果按时间戳分目录")
        lay.addWidget(self.timestamped_var)

        btns = QHBoxLayout()
        check = QPushButton("检查可写")
        check.clicked.connect(self._check_output_writable)
        save = QPushButton("保存配置")
        save.clicked.connect(self._save_config)
        open_dir = QPushButton("打开输出目录")
        open_dir.clicked.connect(self._open_output_dir)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_output_history)
        btns.addWidget(check)
        btns.addWidget(save)
        btns.addWidget(open_dir)
        btns.addWidget(refresh)
        btns.addStretch(1)
        lay.addLayout(btns)

        lay.addWidget(QLabel("最近运行结果（双击打开）"))
        self.history = QListWidget()
        self.history.itemDoubleClicked.connect(self._open_selected_history)
        lay.addWidget(self.history, 1)
        return page

    def _build_log_page(self) -> QFrame:
        page = self._new_page()
        lay = page.layout()
        lay.addWidget(self._page_title("日志预览"))

        head = QHBoxLayout()
        head.addWidget(QLabel("过滤："))
        self.log_filter = QComboBox()
        self.log_filter.addItems(["全部", "INFO", "WARNING", "ERROR"])
        self.log_filter.currentTextChanged.connect(lambda _: self._render_log())
        head.addWidget(self.log_filter)
        self.auto_scroll = QCheckBox("自动滚动")
        self.auto_scroll.setChecked(True)
        head.addWidget(self.auto_scroll)
        head.addStretch(1)
        clear_btn = QPushButton("清空显示")
        clear_btn.clicked.connect(self._clear_log)
        head.addWidget(clear_btn)
        open_log = QPushButton("打开日志文件")
        open_log.clicked.connect(self._open_log_file)
        head.addWidget(open_log)
        lay.addLayout(head)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.viewport().setAutoFillBackground(False)
        lay.addWidget(self.log_text, 1)
        return page

    def _build_about_page(self) -> QFrame:
        page = self._new_page()
        lay = page.layout()
        lay.addWidget(self._page_title("HAPI 大气透过率仿真"))
        lay.addWidget(QLabel("版本 %s" % APP_VERSION))
        desc = QLabel("基于 HITRAN 数据库与 HAPI 库，计算指定波段的大气透过率曲线。")
        desc.setWordWrap(True)
        lay.addWidget(desc)
        cite = QLabel(
            "引用：R.V. Kochanov, I.E. Gordon, L.S. Rothman, P. Wcislo, C. Hill, "
            "J.S. Wilzewski, HITRAN Application Programming Interface (HAPI): "
            "A comprehensive approach to working with spectroscopic data, "
            "J. Quant. Spectrosc. Radiat. Transfer 177, 15-30 (2016). "
            "DOI: 10.1016/j.jqsrt.2016.03.005"
        )
        cite.setWordWrap(True)
        lay.addWidget(cite)
        row = QHBoxLayout()
        open_dir = QPushButton("打开程序目录")
        open_dir.clicked.connect(lambda: os.startfile(str(BASE_DIR)))
        open_cfg = QPushButton("打开配置文件")
        open_cfg.clicked.connect(self._open_config_file)
        row.addWidget(open_dir)
        row.addWidget(open_cfg)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    # ---------------------------------------------------------------- nav
    def _on_nav(self, idx: int):
        self.pages.setCurrentIndex(idx)
        key = self.nav_keys[idx]
        if key == "output":
            self._refresh_output_history()
        if key == "run":
            self._refresh_run_summary()

    # ---------------------------------------------------------------- config
    def _load_config(self):
        ensure_config_exists(CONFIG_PATH)
        try:
            cfg = load_config(CONFIG_PATH)
        except ConfigError as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return
        self._populate_form(cfg)
        self._refresh_run_summary()
        self._refresh_output_history()
        self._log("已加载配置：%s" % CONFIG_PATH)

    def _populate_form(self, cfg: Config):
        self.vars["wavelength_min_um"].setText(str(cfg.band.wavelength_min_um))
        self.vars["wavelength_max_um"].setText(str(cfg.band.wavelength_max_um))
        s = cfg.simulation
        self.vars["wavenumber_step"].setText(str(s.wavenumber_step))
        self.vars["instrument_resolution"].setText(str(s.instrument_resolution))
        self.vars["path_length_cm"].setText(str(s.path_length_cm))
        self.vars["temperature_k"].setText(str(s.temperature_k))
        self.vars["pressure_atm"].setText(str(s.pressure_atm))
        self.vars["wavenumber_wing"].setText(str(s.wavenumber_wing))
        self.vars["tau_line_min"].setText(str(s.tau_line_min))
        self.vars["slit_function"].setCurrentText(s.slit_function)
        self.vars["use_proxy"].setChecked(cfg.network.use_proxy)
        self.vars["proxy"].setText(cfg.network.proxy)
        self.vars["api_key"].setText(cfg.network.api_key)
        self.output_dir_var.setText(cfg.paths.output_dir)
        self.timestamped_var.setChecked(cfg.output.timestamped_subdir)
        self.components_text.setPlainText(
            "\n".join("%s=%s" % (k, v) for k, v in cfg.components.items())
        )
        self.molecules_text.setPlainText(
            "\n".join("%s,%d,%d" % (m.name, m.M, m.I) for m in cfg.molecules)
        )
        ps = cfg.path_scan
        self.pathscan_vars["linewidth"].setText(str(ps.laser_linewidth_cm1))
        self.pathscan_vars["wavelength_points"].setText(str(ps.wavelength_points))
        self.pathscan_vars["distance_min"].setText(str(ps.distance_min_cm))
        self.pathscan_vars["distance_max"].setText(str(ps.distance_max_cm))
        self.pathscan_vars["distance_points"].setText(str(ps.distance_points))
        self.pathscan_vars["scale"].setCurrentText(ps.distance_scale)

    def _read_form_config(self) -> Config:
        def fnum(d, key):
            try:
                return float(d[key].text().strip())
            except ValueError:
                raise ConfigError("字段 %s 不是数字" % key)

        def inum(d, key):
            try:
                return int(float(d[key].text().strip()))
            except ValueError:
                raise ConfigError("字段 %s 不是整数" % key)

        cfg = Config(
            band=BandConfig(
                wavelength_min_um=fnum(self.vars, "wavelength_min_um"),
                wavelength_max_um=fnum(self.vars, "wavelength_max_um"),
            ),
            simulation=SimulationConfig(
                wavenumber_step=fnum(self.vars, "wavenumber_step"),
                instrument_resolution=fnum(self.vars, "instrument_resolution"),
                path_length_cm=fnum(self.vars, "path_length_cm"),
                temperature_k=fnum(self.vars, "temperature_k"),
                pressure_atm=fnum(self.vars, "pressure_atm"),
                wavenumber_wing=fnum(self.vars, "wavenumber_wing"),
                tau_line_min=fnum(self.vars, "tau_line_min"),
                slit_function=self.vars["slit_function"].currentText(),
            ),
            molecules=_parse_molecules(self.molecules_text.toPlainText()),
            components=_parse_components(self.components_text.toPlainText()),
            diluent={"air": 1.0},
            network=NetworkConfig(
                use_proxy=self.vars["use_proxy"].isChecked(),
                proxy=self.vars["proxy"].text(),
                api_key=self.vars["api_key"].text(),
            ),
            paths=PathsConfig(
                cache_dir="hitran_data_cache",
                output_dir=self.output_dir_var.text(),
            ),
            output=OutputConfig(
                timestamped_subdir=self.timestamped_var.isChecked(),
            ),
            path_scan=PathScanConfig(
                laser_linewidth_cm1=fnum(self.pathscan_vars, "linewidth"),
                linewidth_shape="gaussian",
                wavelength_points=inum(self.pathscan_vars, "wavelength_points"),
                distance_min_cm=fnum(self.pathscan_vars, "distance_min"),
                distance_max_cm=fnum(self.pathscan_vars, "distance_max"),
                distance_points=inum(self.pathscan_vars, "distance_points"),
                distance_scale=self.pathscan_vars["scale"].currentText(),
            ),
        )
        validate(cfg)
        return cfg

    def _save_config(self):
        try:
            cfg = self._read_form_config()
            save_config(cfg, CONFIG_PATH)
        except ConfigError as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return
        self._refresh_run_summary()
        self._log("配置已保存：%s" % CONFIG_PATH)
        QMessageBox.information(self, "已保存", "配置已保存到：\n%s" % CONFIG_PATH)

    # ---------------------------------------------------------------- run
    def _start_worker(self, fn, mode: str, label: str):
        if self.worker is not None and self.worker.isRunning():
            return
        try:
            cfg = self._read_form_config()
            save_config(cfg, CONFIG_PATH)
        except ConfigError as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return

        self.running_mode = mode
        self.last_run_dir = None
        self._set_running(True)
        self.statusBar().showMessage("运行中…")
        self._log("=" * 60)
        self._log(label)

        self.worker = PipelineWorker(fn, cfg, self._log, self)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _set_running(self, running: bool):
        if running:
            if self.running_mode == "spec":
                self.btn_run.setEnabled(False)
                self.btn_cancel.setEnabled(True)
                self.progress_spec.show()
            else:
                self.btn_run_ps.setEnabled(False)
                self.btn_cancel_ps.setEnabled(True)
                self.progress_ps.show()
        else:
            self.btn_run.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.progress_spec.hide()
            self.btn_run_ps.setEnabled(True)
            self.btn_cancel_ps.setEnabled(False)
            self.progress_ps.hide()

    def _cancel(self):
        if self.worker is not None:
            self.worker.cancel()
            self._log("正在取消…")

    def _on_done(self, run_dir: str):
        self.last_run_dir = Path(run_dir)
        self.statusBar().showMessage("完成：%s" % run_dir)
        self._log("仿真完成，输出目录：%s" % run_dir)
        QMessageBox.information(self, "完成", "计算完成。\n输出目录：\n%s" % run_dir)

    def _on_failed(self, tb: str):
        self.statusBar().showMessage("运行失败")
        self._log(tb, logging.ERROR)
        logger.error(tb)
        QMessageBox.critical(
            self, "运行失败", "计算过程中出错，详情见“日志预览”页与 logs 目录。"
        )

    def _on_cancelled(self, msg: str):
        self._log("已取消：%s" % msg)

    def _on_finished(self):
        self._set_running(False)
        self.running_mode = None
        if not self.statusBar().currentMessage().startswith("完成"):
            self.statusBar().showMessage("就绪")

    # ---------------------------------------------------------------- logging
    def _log(self, message: str, level: int = logging.INFO):
        logger.log(level, message)
        self.log_signal.emit(level, message)

    def _on_print(self, level: int, message: str):
        self.log_signal.emit(level, message)

    def _level_color(self, level: int) -> str:
        if level >= logging.CRITICAL:
            return "#b00020"
        if level >= logging.ERROR:
            return "#d32f2f"
        if level >= logging.WARNING:
            return "#c77700"
        return "#000000"

    def _level_matches(self, level: int) -> bool:
        current = self.log_filter.currentText()
        if current == "全部":
            return True
        threshold = {"INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}[
            current
        ]
        return level >= threshold

    def _append_log(self, level: int, message: str):
        self.log_lines.append((level, message))
        if len(self.log_lines) > MAX_LOG_LINES:
            self.log_lines = self.log_lines[-MAX_LOG_LINES:]
            self._render_log()
            return
        if self._level_matches(level):
            self._insert_line(level, message)

    def _insert_line(self, level: int, message: str):
        self.log_text.append(
            '<span style="color:%s">%s</span>'
            % (self._level_color(level), html.escape(message))
        )
        if self.auto_scroll.isChecked():
            sb = self.log_text.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _render_log(self):
        self.log_text.clear()
        for level, message in self.log_lines[-MAX_LOG_LINES:]:
            if self._level_matches(level):
                self._insert_line(level, message)

    def _clear_log(self):
        self.log_lines = []
        self.log_text.clear()

    def _open_log_file(self):
        logs = sorted(
            LOG_DIR.glob("run_*.log"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if logs:
            os.startfile(str(logs[0]))
        else:
            QMessageBox.information(self, "提示", "还没有日志文件。")

    # ---------------------------------------------------------------- output
    def _resolve_output_path(self) -> Path:
        value = self.output_dir_var.text().strip() or "output"
        p = Path(value)
        if not p.is_absolute():
            p = DATA_DIR / p
        return p

    def _browse_output(self):
        initial = self._resolve_output_path()
        if not initial.exists():
            initial = DATA_DIR
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录", str(initial))
        if chosen:
            self.output_dir_var.setText(chosen)
            self._refresh_output_history()

    def _check_output_writable(self):
        p = self._resolve_output_path()
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_test.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            QMessageBox.information(self, "可写", "目录可写：\n%s" % p)
        except Exception as exc:
            QMessageBox.critical(self, "不可写", "目录不可写：\n%s\n\n%s" % (p, exc))

    def _refresh_output_history(self):
        self.history.clear()
        self._history_dirs = []
        p = self._resolve_output_path()
        if not p.exists():
            return
        dirs = [d for d in p.iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[:30]:
            self.history.addItem(d.name)
            self._history_dirs.append(d)

    def _open_selected_history(self, item):
        row = self.history.row(item)
        if 0 <= row < len(self._history_dirs):
            os.startfile(str(self._history_dirs[row]))

    def _open_output_dir(self):
        target = self.last_run_dir or self._resolve_output_path()
        if target.exists():
            os.startfile(str(target))
        else:
            QMessageBox.information(self, "提示", "输出目录尚不存在：\n%s" % target)

    def _open_config_file(self):
        if CONFIG_PATH.exists():
            os.startfile(str(CONFIG_PATH))
        else:
            QMessageBox.information(self, "提示", "配置文件不存在：\n%s" % CONFIG_PATH)

    def _refresh_run_summary(self):
        try:
            cfg = self._read_form_config()
        except ConfigError as exc:
            self.summary_spec.setText("参数有误：%s" % exc)
            return
        self.summary_spec.setText(
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


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_QSS)
    icon = _icon_path()
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
