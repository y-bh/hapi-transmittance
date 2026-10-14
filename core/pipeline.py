"""CLI 与 GUI 共用的流水线编排。

提供两个相互独立的计算模式：
- 波长-透过率光谱（固定光程，输出到 spectrum/）
- 光程-透过率矩阵（全波段 T(λ, L)，输出到 path_scan/）
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import Config
from .exporter import export_path_scan, export_results
from .fetcher import prepare_tables
from .simulator import run_path_scan_full, run_simulation


LogFn = Callable[[str], None]


def _prepare(cfg: Config, log: LogFn, cancel_event) -> List[str]:
    downloaded = prepare_tables(cfg, log, cancel_event)
    if not downloaded:
        raise RuntimeError("没有成功加载任何光谱数据，请检查网络连接或 API Key 设置。")
    return downloaded


def run_spectrum_pipeline(
    cfg: Config,
    log: LogFn,
    cancel_event: Optional[threading.Event] = None,
    output_dir_override: Optional[str] = None,
) -> Tuple[Path, List[Path]]:
    cancel_event = cancel_event or threading.Event()
    downloaded = _prepare(cfg, log, cancel_event)
    results = run_simulation(cfg, downloaded, log, cancel_event)
    return export_results(cfg, results, log, output_dir_override)


def run_path_scan_pipeline(
    cfg: Config,
    log: LogFn,
    cancel_event: Optional[threading.Event] = None,
    output_dir_override: Optional[str] = None,
) -> Tuple[Path, List[Path]]:
    cancel_event = cancel_event or threading.Event()
    downloaded = _prepare(cfg, log, cancel_event)
    log("开始计算全波段光程-透过率矩阵 T(λ, L)...")
    path_results = run_path_scan_full(cfg, downloaded, log, cancel_event)
    return export_path_scan(cfg, path_results, log, output_dir_override)
