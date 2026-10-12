"""命令行入口。

用法:
    python app.py
    python app.py --config config.json
    python app.py --output D:/results
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
    get_output_dir,
    load_config,
)
from core.logging_setup import capture_print_output, setup_logging
from core.pipeline import run_pipeline


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
    args = parser.parse_args(argv)

    base_dir = get_base_dir()
    config_path = Path(args.config) if args.config else base_dir / "config.json"

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
        run_dir, files = run_pipeline(
            cfg, log.info, output_dir_override=args.output
        )
        log.info("仿真完成，输出目录: %s", run_dir)
        return EXIT_OK
    except Exception:
        log.critical("运行失败:\n%s", traceback.format_exc())
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
