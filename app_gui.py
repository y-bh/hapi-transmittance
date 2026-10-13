"""Tkinter 图形界面入口。

界面负责编辑配置、启动后台计算、显示进度与日志。真正的计算在后台线程执行，
通过队列把日志消息传回主线程，避免界面卡死。
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from core.config import (
    BandConfig,
    Config,
    ConfigError,
    Molecule,
    NetworkConfig,
    OutputConfig,
    PathsConfig,
    SimulationConfig,
    SLIT_FUNCTIONS,
    ensure_config_exists,
    get_base_dir,
    get_config_path,
    get_data_dir,
    get_output_dir,
    load_config,
    save_config,
)
from core.fetcher import CancelledError
from core.logging_setup import capture_print_output, setup_logging


BASE_DIR = get_base_dir()
DATA_DIR = get_data_dir(BASE_DIR)
CONFIG_PATH = get_config_path(BASE_DIR)
LOG_DIR = DATA_DIR / "logs"

# 先建好日志并接管 print，再导入 pipeline（其内部会 import hapi 并打印 banner）
logger = setup_logging(LOG_DIR)
capture_print_output(logger)

if DATA_DIR != BASE_DIR:
    logger.warning("程序目录不可写，配置/缓存/输出/日志将保存到 %s", DATA_DIR)

from core.pipeline import run_pipeline  # noqa: E402


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
        root.title("HAPI 大气透过率仿真")
        root.geometry("900x760")
        root.minsize(820, 680)

        self.q = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.last_run_dir: Path | None = None

        self._vars = {}
        self._build_widgets()
        self._load()
        root.after(120, self._poll)

    # ---------------------------------------------------------- UI
    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        fields = [
            ("wavelength_min_um", "波长下限 (μm)", "8.9"),
            ("wavelength_max_um", "波长上限 (μm)", "11.0"),
            ("wavenumber_step", "计算步长 (cm⁻¹)", "0.01"),
            ("instrument_resolution", "仪器分辨率 (cm⁻¹)", "0.5"),
            ("path_length_cm", "光程 (cm)", "1000000"),
            ("temperature_k", "温度 (K)", "296"),
            ("pressure_atm", "压强 (atm)", "1.0"),
            ("wavenumber_wing", "谱线翼截断 (cm⁻¹)", "25"),
            ("tau_line_min", "最小光学厚度 τ_line", "0.0001"),
            ("proxy", "代理地址", "http://127.0.0.1:7890"),
            ("api_key", "HITRAN API Key", ""),
        ]
        for i, (key, label, default) in enumerate(fields):
            r, c = divmod(i, 2)
            self._add_field(top, r, c, key, label, default)

        # 狭缝函数 / 开关
        row = (len(fields) + 1) // 2
        self._vars["slit_function"] = tk.StringVar(value="gaussian")
        ttk.Label(top, text="狭缝函数").grid(row=row, column=0, sticky="w", pady=4)
        slit_box = ttk.Combobox(
            top,
            textvariable=self._vars["slit_function"],
            values=list(SLIT_FUNCTIONS.keys()),
            state="readonly",
            width=18,
        )
        slit_box.grid(row=row, column=1, sticky="we", pady=4)

        self._vars["use_proxy"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="启用代理", variable=self._vars["use_proxy"]
        ).grid(row=row, column=2, sticky="w", pady=4)
        self._vars["timestamped_subdir"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top, text="结果按时间戳分目录", variable=self._vars["timestamped_subdir"]
        ).grid(row=row, column=3, sticky="w", pady=4)

        # 成分与分子
        mid = ttk.Frame(self.root, padding=10)
        mid.pack(fill="x")
        left = ttk.LabelFrame(mid, text="成分体积混合比（name=value，每行一个）")
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.components_text = tk.Text(left, height=6, width=38)
        self.components_text.pack(fill="both", expand=True)

        right = ttk.LabelFrame(mid, text="分子清单（name,M,I，每行一个，高级）")
        right.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.molecules_text = tk.Text(right, height=6, width=38)
        self.molecules_text.pack(fill="both", expand=True)

        # 输出目录
        out_row = ttk.Frame(self.root, padding=(10, 0))
        out_row.pack(fill="x")
        self._vars["output_dir"] = tk.StringVar(value="output")
        ttk.Label(out_row, text="输出目录").pack(side="left")
        ttk.Entry(out_row, textvariable=self._vars["output_dir"]).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(out_row, text="浏览...", command=self._browse_output).pack(side="left")

        # 控制按钮
        btn_row = ttk.Frame(self.root, padding=(10, 6))
        btn_row.pack(fill="x")
        self.btn_run = ttk.Button(btn_row, text="开始计算", command=self._run)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(
            btn_row, text="取消", command=self._cancel, state="disabled"
        )
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(btn_row, text="保存配置", command=self._save).pack(side="left")
        self.btn_open = ttk.Button(
            btn_row, text="打开输出目录", command=self._open_output, state="disabled"
        )
        self.btn_open.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=(0, 4))

        self.log_text = scrolledtext.ScrolledText(self.root, height=18, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _add_field(self, parent, row, col, key, label, default) -> None:
        self._vars[key] = tk.StringVar(value=default)
        ttk.Label(parent, text=label).grid(
            row=row, column=col * 2, sticky="w", pady=4
        )
        ttk.Entry(parent, textvariable=self._vars[key], width=22).grid(
            row=row, column=col * 2 + 1, sticky="we", pady=4, padx=(0, 12)
        )
        parent.columnconfigure(col * 2 + 1, weight=1)

    # ---------------------------------------------------------- 配置
    def _load(self) -> None:
        ensure_config_exists(CONFIG_PATH)
        try:
            cfg = load_config(CONFIG_PATH)
        except ConfigError as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        self._vars["wavelength_min_um"].set(str(cfg.band.wavelength_min_um))
        self._vars["wavelength_max_um"].set(str(cfg.band.wavelength_max_um))
        s = cfg.simulation
        self._vars["wavenumber_step"].set(str(s.wavenumber_step))
        self._vars["instrument_resolution"].set(str(s.instrument_resolution))
        self._vars["path_length_cm"].set(str(s.path_length_cm))
        self._vars["temperature_k"].set(str(s.temperature_k))
        self._vars["pressure_atm"].set(str(s.pressure_atm))
        self._vars["wavenumber_wing"].set(str(s.wavenumber_wing))
        self._vars["tau_line_min"].set(str(s.tau_line_min))
        self._vars["slit_function"].set(s.slit_function)
        self._vars["proxy"].set(cfg.network.proxy)
        self._vars["api_key"].set(cfg.network.api_key)
        self._vars["use_proxy"].set(cfg.network.use_proxy)
        self._vars["timestamped_subdir"].set(cfg.output.timestamped_subdir)
        self._vars["output_dir"].set(cfg.paths.output_dir)

        self.components_text.delete("1.0", "end")
        self.components_text.insert(
            "1.0", "\n".join("%s=%s" % (k, v) for k, v in cfg.components.items())
        )
        self.molecules_text.delete("1.0", "end")
        self.molecules_text.insert(
            "1.0", "\n".join("%s,%d,%d" % (m.name, m.M, m.I) for m in cfg.molecules)
        )
        self._log("已加载配置: %s" % CONFIG_PATH)

    def _form_to_config(self) -> Config:
        def num(key):
            try:
                return float(self._vars[key].get())
            except ValueError:
                raise ConfigError("字段 %s 不是数字" % key)

        components = _parse_components(self.components_text.get("1.0", "end"))
        molecules = _parse_molecules(self.molecules_text.get("1.0", "end"))
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
                slit_function=self._vars["slit_function"].get(),
            ),
            molecules=molecules,
            components=components,
            diluent={"air": 1.0},
            network=NetworkConfig(
                use_proxy=self._vars["use_proxy"].get(),
                proxy=self._vars["proxy"].get(),
                api_key=self._vars["api_key"].get(),
            ),
            paths=PathsConfig(
                cache_dir="hitran_data_cache",
                output_dir=self._vars["output_dir"].get(),
            ),
            output=OutputConfig(timestamped_subdir=self._vars["timestamped_subdir"].get()),
        )
        # 触发校验
        from core.config import validate

        validate(cfg)
        return cfg

    def _save(self) -> None:
        try:
            cfg = self._form_to_config()
            save_config(cfg, CONFIG_PATH)
        except ConfigError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self._log("配置已保存: %s" % CONFIG_PATH)
        messagebox.showinfo("已保存", "配置已保存到 %s" % CONFIG_PATH)

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(BASE_DIR))
        if chosen:
            self._vars["output_dir"].set(chosen)

    # ---------------------------------------------------------- 运行
    def _run(self) -> None:
        if self.running:
            return
        try:
            cfg = self._form_to_config()
            save_config(cfg, CONFIG_PATH)
        except ConfigError as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        self.running = True
        self.cancel_event.clear()
        self.btn_run.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.btn_open.config(state="disabled")
        self.progress.start(12)
        self._log("=" * 60)
        self._log("开始计算：%.3f–%.3f μm" %
                 (cfg.band.wavelength_min_um, cfg.band.wavelength_max_um))
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _worker(self, cfg: Config) -> None:
        try:
            run_dir, files = run_pipeline(
                cfg, self._log, self.cancel_event
            )
            self.q.put(("done", run_dir))
        except CancelledError as exc:
            self.q.put(("cancelled", str(exc)))
        except Exception:
            self.q.put(("error", traceback.format_exc()))
        finally:
            self.q.put(("stopped", None))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self._log("正在取消...")

    def _log(self, message: str) -> None:
        self.q.put(("log", message))
        logger.info(message)

    # ---------------------------------------------------------- 队列
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "cancelled":
                    self._append_log("已取消: %s" % payload)
                elif kind == "error":
                    self._on_error(payload)
                elif kind == "stopped":
                    self._on_stopped()
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _append_log(self, message: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _on_done(self, run_dir: Path) -> None:
        self.last_run_dir = Path(run_dir)
        self._append_log("仿真完成，输出目录: %s" % run_dir)
        messagebox.showinfo("完成", "计算完成。\n输出目录:\n%s" % run_dir)

    def _on_error(self, tb: str) -> None:
        self._append_log(tb)
        logger.error(tb)
        messagebox.showerror(
            "运行失败", "计算过程中出错，详情见日志区与 logs 目录下的日志文件。"
        )

    def _on_stopped(self) -> None:
        self.running = False
        self.progress.stop()
        self.btn_run.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.btn_open.config(state="normal" if self.last_run_dir else "disabled")

    def _open_output(self) -> None:
        target = self.last_run_dir or get_output_dir(
            load_config(CONFIG_PATH), BASE_DIR
        )
        if target.exists():
            os.startfile(str(target))
        else:
            messagebox.showinfo("提示", "输出目录尚不存在: %s" % target)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
