"""把计算结果写入输出目录：txt/csv/png。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .config import Config, get_output_dir


LogFn = Callable[[str], None]


def export_results(
    cfg: Config,
    results: Dict[str, np.ndarray],
    log: LogFn,
    output_dir_override: Optional[str] = None,
) -> Tuple[Path, List[Path]]:
    out_root = get_output_dir(cfg, override=output_dir_override)
    run_dir = (
        out_root / time.strftime("%Y%m%d_%H%M%S")
        if cfg.output.timestamped_subdir
        else out_root
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    files: List[Path] = []
    nu = results["nu"]
    coef = results["coef"]
    num_density = results["num_density"]
    nu_t = results["nu_t"]
    trans = results["trans"]
    wavelength = results["wavelength"]
    trans_conv = results["trans_conv"]

    if cfg.output.save_txt:
        coef_hitran = coef / num_density  # cm²/molecule
        p1 = run_dir / "absorption_coefficient.txt"
        np.savetxt(
            p1,
            np.column_stack((nu, coef_hitran)),
            delimiter=" ",
            header="wavenumber_cm-1 absorption_coefficient_cm2_per_molecule",
            comments="# ",
        )
        files.append(p1)

        p2 = run_dir / "transmittance_highres.txt"
        np.savetxt(
            p2,
            np.column_stack((nu_t, trans)),
            delimiter=" ",
            header="wavenumber_cm-1 transmittance",
            comments="# ",
        )
        files.append(p2)

    if cfg.output.save_csv:
        p3 = run_dir / "transmittance_final.csv"
        np.savetxt(
            p3,
            np.column_stack((wavelength, 1e4 / wavelength, trans_conv)),
            delimiter=",",
            header="wavelength_um,wavenumber_cm-1,transmittance",
            comments="",
        )
        files.append(p3)

    if cfg.output.save_png:
        p4 = _make_figure(cfg, results, run_dir)
        files.append(p4)

    log("结果已写入目录: %s" % run_dir)
    for f in files:
        log("  - %s" % f.name)
    return run_dir, files


def _make_figure(cfg: Config, results: Dict[str, np.ndarray], run_dir: Path) -> Path:
    wavelength = results["wavelength"]
    wavelength_hr = results["wavelength_hr"]
    coef = results["coef"]
    trans = results["trans"]
    trans_conv = results["trans_conv"]

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

    ax1 = axes[0]
    ax1.plot(wavelength_hr, coef, "r-", linewidth=0.4, alpha=0.8)
    ax1.set_ylabel("Absorption Coefficient (cm$^{-1}$)", fontsize=11)
    ax1.set_title(
        "Atmospheric Transmittance Simulation: %.3f–%.3f μm "
        "(%.0f–%.0f cm$^{-1}$)\n"
        "T=%.1f K, p=%.3g atm, Path=%.0f m"
        % (
            cfg.band.wavelength_min_um,
            cfg.band.wavelength_max_um,
            cfg.wavenumber_min,
            cfg.wavenumber_max,
            cfg.simulation.temperature_k,
            cfg.simulation.pressure_atm,
            cfg.simulation.path_length_cm / 100.0,
        ),
        fontsize=13,
    )
    ax1.set_yscale("log")
    ax1.grid(True, alpha=0.3)
    ax1.set_xlim(cfg.band.wavelength_min_um, cfg.band.wavelength_max_um)

    ax2 = axes[1]
    ax2.plot(wavelength_hr, trans, "g-", linewidth=0.4, alpha=0.8)
    ax2.set_ylabel("Transmittance (High-Res)", fontsize=11)
    ax2.set_ylim(0, 1.05)
    ax2.grid(True, alpha=0.3)

    ax3 = axes[2]
    ax3.plot(
        wavelength,
        trans_conv,
        "b-",
        linewidth=1.5,
        label="Convolved (Resolution=%.3g cm$^{-1}$)"
        % cfg.simulation.instrument_resolution,
    )
    ax3.set_xlabel("Wavelength (μm)", fontsize=12)
    ax3.set_ylabel("Transmittance (Convolved)", fontsize=11)
    ax3.set_ylim(0, 1.05)
    ax3.grid(True, alpha=0.3)
    ax3.legend(loc="lower left")

    fig.tight_layout()
    path = run_dir / "atmospheric_transmittance.png"
    fig.savefig(path, dpi=cfg.output.png_dpi)
    plt.close(fig)
    return path
