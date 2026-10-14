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


def _run_dir(cfg: Config, out_root: Path, sub: str) -> Path:
    base = out_root / sub
    if cfg.output.timestamped_subdir:
        return base / time.strftime("%Y%m%d_%H%M%S")
    return base


def export_results(
    cfg: Config,
    results: Dict[str, np.ndarray],
    log: LogFn,
    output_dir_override: Optional[str] = None,
) -> Tuple[Path, List[Path]]:
    out_root = get_output_dir(cfg, override=output_dir_override)
    run_dir = _run_dir(cfg, out_root, "spectrum")
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


def export_path_scan(
    cfg: Config,
    path_results: Dict[str, np.ndarray],
    log: LogFn,
    output_dir_override: Optional[str] = None,
) -> Tuple[Path, List[Path]]:
    """导出全波段光程-透过率矩阵：CSV 与热图 PNG。"""
    out_root = get_output_dir(cfg, override=output_dir_override)
    run_dir = _run_dir(cfg, out_root, "path_scan")
    run_dir.mkdir(parents=True, exist_ok=True)

    distances_cm = path_results["distances_cm"]
    wavelengths_um = path_results["wavelengths_um"]
    trans_map = path_results["transmittance"]  # 形状 (Nλ, N距离)

    wavelengths_asc = np.asarray(wavelengths_um)[::-1]  # 波长从小到大
    trans_asc = np.asarray(trans_map)[:, ::-1]          # 列与波长对齐
    distance_m = np.asarray(distances_cm) / 100.0

    files: List[Path] = []

    if cfg.output.save_csv:
        table = np.column_stack([distance_m, trans_asc.T])
        header = "distance_m," + ",".join("%.6f" % w for w in wavelengths_asc)
        p = run_dir / "transmittance_vs_distance.csv"
        np.savetxt(p, table, delimiter=",", header=header, comments="")
        files.append(p)

    if cfg.output.save_png:
        p = _make_path_scan_figure(cfg, path_results, run_dir)
        files.append(p)

    log("光程-透过率结果已写入目录: %s" % run_dir)
    for f in files:
        log("  - %s" % f.name)
    return run_dir, files


def _make_path_scan_figure(
    cfg: Config,
    path_results: Dict[str, np.ndarray],
    run_dir: Path,
) -> Path:
    distances_m = np.asarray(path_results["distances_cm"]) / 100.0
    wavelengths_um = np.asarray(path_results["wavelengths_um"])
    trans_map = np.asarray(path_results["transmittance"])

    data = trans_map[:, ::-1]  # 列按波长从小到大
    extent = [
        float(wavelengths_um[-1]),
        float(wavelengths_um[0]),
        float(distances_m[0]),
        float(distances_m[-1]),
    ]

    fig, ax = plt.subplots(figsize=(11, 7))
    im = ax.imshow(
        data,
        aspect="auto",
        origin="lower",
        extent=extent,
        cmap="viridis",
        interpolation="nearest",
        vmin=0.0,
        vmax=1.0,
    )
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Transmittance")
    ax.set_xlabel("Wavelength (μm)")
    ax.set_ylabel("Distance (m)")
    if cfg.path_scan.distance_scale == "log":
        ax.set_yscale("log")
    ax.set_title(
        "Transmittance vs Distance (%.3f–%.3f μm, laser FWHM=%.3g cm$^{-1}$)"
        % (
            cfg.band.wavelength_min_um,
            cfg.band.wavelength_max_um,
            cfg.path_scan.laser_linewidth_cm1,
        )
    )
    fig.tight_layout()
    path = run_dir / "transmittance_vs_distance.png"
    fig.savefig(path, dpi=cfg.output.png_dpi)
    plt.close(fig)
    return path
