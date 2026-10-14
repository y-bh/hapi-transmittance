"""核心仿真：吸收系数 -> 透过率 -> 仪器卷积，以及光程-透过率矩阵。"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

import hapi

from .config import Config
from .fetcher import CancelledError


LogFn = Callable[[str], None]


def compute_absorption_coefficient(
    cfg: Config,
    downloaded_tables: List[str],
    log: LogFn,
    cancel_event,
    path_length_cm: Optional[float] = None,
) -> Tuple[np.ndarray, np.ndarray, float]:
    """计算全波段高分辨率吸收系数 k(ν)，供光谱模式与光程模式共用。"""
    wmin, wmax = cfg.wavenumber_min, cfg.wavenumber_max
    env = {"T": cfg.simulation.temperature_k, "p": cfg.simulation.pressure_atm}
    if path_length_cm is None:
        path_length_cm = cfg.simulation.path_length_cm

    molecules_by_name = {m.name: m for m in cfg.molecules}
    components_arg = [
        (molecules_by_name[n].M, molecules_by_name[n].I, cfg.components[n])
        for n in downloaded_tables
        if n in cfg.components
    ]

    num_density = hapi.volumeConcentration(env["p"], env["T"])
    intensity_threshold = cfg.simulation.tau_line_min / (
        num_density * path_length_cm
    )

    log("分子数密度 N = %.4e cm⁻³" % num_density)
    log("强度阈值 IntensityThreshold = %.3e（由 τ_line_min = %.0e 反推）"
        % (intensity_threshold, cfg.simulation.tau_line_min))

    log("各分子谱线参与情况（波段内 / 过阈值后）：")
    for name in downloaded_tables:
        nu = np.asarray(hapi.LOCAL_TABLE_CACHE[name]["data"]["nu"], dtype=float)
        sw = np.asarray(hapi.LOCAL_TABLE_CACHE[name]["data"]["sw"], dtype=float)
        inside = (nu >= wmin) & (nu <= wmax)
        m = molecules_by_name[name]
        scaled = sw * (cfg.components[name] / hapi.abundance(m.M, m.I))
        keep = inside & (scaled >= intensity_threshold)
        log("  %-4s: %6d / %6d（剔除 %d 条弱线）"
            % (name, int(inside.sum()), int(keep.sum()),
               int(inside.sum() - keep.sum())))

    if cancel_event.is_set():
        raise CancelledError("用户取消")

    log("正在计算吸收系数（Voigt，高分辨率）...")
    nu, coef = hapi.absorptionCoefficient_Voigt(
        SourceTables=downloaded_tables,
        Components=components_arg,
        Environment=env,
        WavenumberRange=(wmin, wmax),
        WavenumberStep=cfg.simulation.wavenumber_step,
        WavenumberWing=cfg.simulation.wavenumber_wing,
        WavenumberWingHW=0,
        IntensityThreshold=intensity_threshold,
        Diluent=cfg.diluent,
        HITRAN_units=False,
    )
    log("高分辨率网格点数: %d" % len(nu))
    return np.asarray(nu, dtype=float), np.asarray(coef, dtype=float), num_density


def run_simulation(
    cfg: Config,
    downloaded_tables: List[str],
    log: LogFn,
    cancel_event,
) -> Dict[str, np.ndarray]:
    """波长-透过率光谱：固定光程下计算透过率并做仪器卷积。"""
    nu, coef, num_density = compute_absorption_coefficient(
        cfg, downloaded_tables, log, cancel_event
    )

    if cancel_event.is_set():
        raise CancelledError("用户取消")

    log("正在计算透过率光谱...")
    nu_t, trans = hapi.transmittanceSpectrum(
        nu, coef, Environment={"l": cfg.simulation.path_length_cm}
    )

    if cancel_event.is_set():
        raise CancelledError("用户取消")

    log("正在进行仪器卷积（Resolution = %s cm⁻¹，%s 狭缝）..."
        % (cfg.simulation.instrument_resolution, cfg.simulation.slit_function))
    slit = getattr(hapi, cfg.slit_function_name)
    nu_conv, trans_conv, i1, i2, slit_w = hapi.convolveSpectrum(
        nu_t,
        trans,
        Resolution=cfg.simulation.instrument_resolution,
        AF_wing=10.0,
        SlitFunction=slit,
    )

    if nu_conv.size == 0 or trans_conv.size == 0:
        raise RuntimeError(
            "仪器卷积结果为空：波段范围相对分辨率/谱线翼太小，"
            "请增大波段范围或减小 instrument_resolution / wavenumber_wing。"
        )

    wavelength = 1e4 / nu_conv
    wavelength_hr = 1e4 / nu
    log("透过率摘要（卷积后）：平均 %.4f，最小 %.4f，最大 %.4f"
        % (float(trans_conv.mean()), float(trans_conv.min()),
           float(trans_conv.max())))

    return {
        "nu": nu,
        "coef": coef,
        "num_density": num_density,
        "nu_t": nu_t,
        "trans": trans,
        "nu_conv": nu_conv,
        "trans_conv": trans_conv,
        "wavelength": wavelength,
        "wavelength_hr": wavelength_hr,
        "slit_w": slit_w,
    }


def run_path_scan_full(
    cfg: Config,
    downloaded_tables: List[str],
    log: LogFn,
    cancel_event,
) -> Dict[str, np.ndarray]:
    """光程-透过率矩阵：先算 k(ν)，再计算全波段 T(λ, L)。"""
    nu, coef, _ = compute_absorption_coefficient(
        cfg,
        downloaded_tables,
        log,
        cancel_event,
        path_length_cm=cfg.path_scan.distance_max_cm,
    )
    return run_path_scan(cfg, nu, coef, log, cancel_event)


def run_path_scan(
    cfg: Config,
    nu: np.ndarray,
    coef: np.ndarray,
    log: LogFn,
    cancel_event,
) -> Dict[str, np.ndarray]:
    """全波段光程-透过率矩阵 T(λ, L)。

    对全波段等距采样一组激光中心波长，每个波长用高斯线宽对高分辨率
    吸收系数 k(ν) 加权，按功率加权比尔-朗伯定律计算不同距离下的透过率：
        T(L) = ∫ g(ν)·exp(-k(ν)·L) dν
    """
    ps = cfg.path_scan
    nu = np.asarray(nu, dtype=float)
    coef = np.asarray(coef, dtype=float)

    sigma = ps.laser_linewidth_cm1 / (2.0 * np.sqrt(2.0 * np.log(2.0)))
    if nu.size > 1:
        dnu = float(np.median(np.diff(nu)))
        if sigma < dnu:
            log(
                "提示：激光线宽 σ=%.3g cm⁻¹ 小于网格步长 %.3g cm⁻¹，"
                "将退化为最近网格点采样（近似单频激光）" % (sigma, dnu)
            )

    if ps.distance_scale == "log":
        dmin = ps.distance_min_cm
        if dmin <= 0:
            dmin = max(1.0, ps.distance_max_cm / 1e6)
            log("对数距离轴要求 distance_min > 0，已自动设为 %.3g cm" % dmin)
        distances = np.geomspace(dmin, ps.distance_max_cm, ps.distance_points)
    else:
        distances = np.linspace(
            ps.distance_min_cm, ps.distance_max_cm, ps.distance_points
        )

    nu_centers = np.linspace(
        cfg.wavenumber_min, cfg.wavenumber_max, ps.wavelength_points
    )

    exp_factor = np.exp(-np.outer(coef, distances))
    trans_map = np.empty((ps.wavelength_points, ps.distance_points), dtype=float)

    report_every = max(1, ps.wavelength_points // 10)
    for i, center in enumerate(nu_centers):
        if cancel_event.is_set():
            raise CancelledError("用户取消")
        weight = np.exp(-0.5 * ((nu - center) / sigma) ** 2)
        weight_sum = weight.sum()
        if weight_sum == 0:
            weight_sum = 1.0
        trans_map[i] = weight @ exp_factor / weight_sum
        if i % report_every == 0:
            log(
                "  光程扫描 %d/%d（λ=%.3f μm）"
                % (i + 1, ps.wavelength_points, 1e4 / center)
            )

    wavelengths_um = 1e4 / nu_centers
    log(
        "光程-透过率矩阵计算完成：%d 个波长 × %d 个距离"
        % (ps.wavelength_points, ps.distance_points)
    )
    return {
        "distances_cm": distances,
        "wavelengths_um": wavelengths_um,
        "transmittance": trans_map,
    }
