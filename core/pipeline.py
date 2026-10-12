"""CLI 与 GUI 共用的流水线编排。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import Config
from .exporter import export_results
from .fetcher import prepare_tables
from .simulator import run_simulation


LogFn = Callable[[str], None]


def run_pipeline(
    cfg: Config,
    log: LogFn,
    cancel_event: Optional[threading.Event] = None,
    output_dir_override: Optional[str] = None,
) -> Tuple[Path, List[Path]]:
    cancel_event = cancel_event or threading.Event()

    downloaded = prepare_tables(cfg, log, cancel_event)
    if not downloaded:
        raise RuntimeError("没有成功加载任何光谱数据，请检查网络连接或 API Key 设置。")

    results = run_simulation(cfg, downloaded, log, cancel_event)
    run_dir, files = export_results(cfg, results, log, output_dir_override)
    return run_dir, files
