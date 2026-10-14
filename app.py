"""命令行入口。

用法:
    python app.py
    python app.py --config config.json
    python app.py --output D:/results
    python app.py --mode pathscan
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from core.config import (
    ConfigError,
    ensure_config_exists,
    get_base_dir,
    get_config_path,
    get_data_dir,
    get_output_dir,
    load_config,
)
from core.logging_setup import capture_print_output, setup_logging
from core.pipeline import run_path_scan_pipeline, run_spectrum_pipeline


EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_RUNTIME = 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="HAPI 大气透过率仿真")
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认: 程序根目录下的 config.json）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出目录（覆盖配置文件中的 paths.output_dir）",
    )
    parser.add_argument(
        "--mode",
        choices=("spectrum", "pathscan"),
        default="spectrum",
        help="计算模式：spectrum=波长-透过率，pathscan=光程-透过率（默认 spectrum）",
    )
    args = parser.parse_args(argv)

    base_dir = get_base_dir()
    data_dir = get_data_dir(base_dir)
    config_path = Path(args.config) if args.config else get_config_path(base_dir)

    if data_dir != base_dir:
        print(
            "注意：程序目录不可写，配置/缓存/输出/日志将保存到 %s" % data_dir,
            file=sys.stderr,
        )

    try:
        created = ensure_config_exists(config_path)
        cfg = load_config(config_path)
    except ConfigError as exc:
        print("配置错误: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG

    if created:
        print("已生成配置文件模板: %s" % config_path)

    out_dir = get_output_dir(cfg, base_dir, args.output)
    log = setup_logging(out_dir / "logs")
    capture_print_output(log)

    log.info("程序启动，配置文件: %s", config_path)
    log.info("波段 %.3f–%.3f μm（%.2f–%.2f cm⁻¹）",
             cfg.band.wavelength_min_um, cfg.band.wavelength_max_um,
             cfg.wavenumber_min, cfg.wavenumber_max)

    try:
        if args.mode == "pathscan":
            run_dir, files = run_path_scan_pipeline(
                cfg, log.info, output_dir_override=args.output
            )
        else:
            run_dir, files = run_spectrum_pipeline(
                cfg, log.info, output_dir_override=args.output
            )
        log.info("仿真完成，输出目录: %s", run_dir)
        return EXIT_OK
    except Exception:
        log.critical("运行失败:\n%s", traceback.format_exc())
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
