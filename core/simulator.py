"""核心仿真：吸收系数 -> 透过率 -> 仪器卷积。"""

from __future__ import annotations

from typing import Callable, Dict, List

import numpy as np

import hapi

from .config import Config
from .fetcher import CancelledError


LogFn = Callable[[str], None]


def run_simulation(
    cfg: Config,
    downloaded_tables: List[str],
    log: LogFn,
    cancel_event,
) -> Dict[str, np.ndarray]:
    wmin, wmax = cfg.wavenumber_min, cfg.wavenumber_max
    env = {"T": cfg.simulation.temperature_k, "p": cfg.simulation.pressure_atm}

    molecules_by_name = {m.name: m for m in cfg.molecules}
    components_arg = [
        (molecules_by_name[n].M, molecules_by_name[n].I, cfg.components[n])
        for n in downloaded_tables
        if n in cfg.components
    ]

    num_density = hapi.volumeConcentration(env["p"], env["T"])
    intensity_threshold = cfg.simulation.tau_line_min / (
        num_density * cfg.simulation.path_length_cm
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
