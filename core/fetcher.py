"""HITRAN 谱线数据的下载与本地缓存管理。

这里不再 monkey-patch HAPI 的 queryHITRAN 或 urllib，而是直接使用 requests
向 HITRAN API 发起请求，下载后写入 HAPI 约定的 .data/.header 文件，
再调用 hapi.storage2cache 装入内存。好处是代理、超时、重试、错误分类都显式可控。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests

import hapi

from .config import Config, get_cache_dir


class NoSpectralDataError(Exception):
    """HITRAN 返回 HTTP 404：该同位素在所请求波数区间内没有谱线。"""


class NetworkError(Exception):
    """网络/下载类错误。"""


class CancelledError(Exception):
    """用户主动取消。"""


LogFn = Callable[[str], None]


def get_api_key(cfg: Config) -> str:
    import os

    return cfg.network.api_key or os.environ.get("HITRAN_API_KEY", "")


def _download_table(
    cfg: Config,
    molecule,
    numin: float,
    numax: float,
    log: LogFn,
    cancel_event,
) -> None:
    name = molecule.name
    iso_id = hapi.ISO[(molecule.M, molecule.I)][hapi.ISO_INDEX["id"]]
    iso_id_list = [iso_id]
    cache_dir = get_cache_dir(cfg)

    host = hapi.VARIABLES.get("GLOBAL_HOST", "http://hitran.org")
    url = "%s/lbl/api" % host
    params = {
        "iso_ids_list": ",".join(str(i) for i in iso_id_list),
        "numin": "%.6f" % numin,
        "numax": "%.6f" % numax,
    }
    api_key = get_api_key(cfg)
    if api_key:
        params["key"] = api_key

    proxies = None
    if cfg.network.use_proxy and cfg.network.proxy:
        proxies = {"http": cfg.network.proxy, "https": cfg.network.proxy}

    last_error = None
    for attempt in range(cfg.network.max_retries + 1):
        if cancel_event.is_set():
            raise CancelledError("用户取消")
        if attempt > 0:
            wait = min(2 ** attempt, 30)
            log("    第 %d 次重试，等待 %d 秒..." % (attempt, wait))
            time.sleep(wait)
        try:
            resp = requests.get(
                url,
                params=params,
                proxies=proxies,
                timeout=cfg.network.timeout_s,
                stream=True,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            log("    请求失败: %s" % exc)
            continue

        if resp.status_code == 403:
            raise NetworkError("HTTP 403：已超过 HITRAN API 当日查询限额。")
        if resp.status_code == 404:
            raise NoSpectralDataError(
                "HTTP 404 — HITRAN 在 %.1f–%.1f cm⁻¹ 内没有该同位素的谱线数据"
                % (numin, numax)
            )
        if resp.status_code >= 500:
            last_error = RuntimeError("HTTP %d" % resp.status_code)
            log("    服务器错误 HTTP %d" % resp.status_code)
            resp.close()
            continue
        if resp.status_code != 200:
            resp.close()
            raise NetworkError("下载失败（HTTP %d）" % resp.status_code)

        data_path = cache_dir / ("%s.data" % name)
        header_path = cache_dir / ("%s.header" % name)
        log("    开始下载 %s（1MB/块）..." % name)
        total = 0
        chunk_count = 0
        try:
            with open(data_path, "w", encoding="utf-8", newline="\n") as fp:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if cancel_event.is_set():
                        raise CancelledError("用户取消")
                    if not chunk:
                        continue
                    fp.write(chunk.decode("utf-8"))
                    total += len(chunk)
                    chunk_count += 1
                    log("    ... %d MB" % chunk_count)
        finally:
            resp.close()

        parlist = hapi.prepareParlist(pargroups=[], params=[], dotpar=True)
        header = hapi.prepareHeader(parlist)
        header["table_name"] = name
        header_path.write_text(json.dumps(header, indent=2), encoding="utf-8")

        log("    共写入 %d 字节（%.1f MB），开始导入缓存..."
            % (total, total / 1048576.0))
        hapi.storage2cache(name)
        log("    导入完成: %s（%d 条谱线）"
            % (name, len(hapi.LOCAL_TABLE_CACHE[name]["data"]["nu"])))
        return

    raise NetworkError("下载失败，已重试 %d 次。最后错误: %s"
                       % (cfg.network.max_retries, last_error))


def load_band_info(cache_dir: Path) -> Optional[dict]:
    path = cache_dir / "band_info.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_band_info(
    cfg: Config,
    cache_dir: Path,
    status_map: Dict[str, str],
    old_info: Optional[dict],
) -> None:
    from datetime import datetime

    nu_min = None
    nu_max = None
    molecules = {}
    old_mols = (old_info or {}).get("molecules", {})
    for m in cfg.molecules:
        status = status_map.get(m.name)
        if status is None:
            rec = old_mols.get(m.name, {})
            status = rec.get("status")
            if status is None:
                status = "ok" if rec.get("success") else "failed"
        nlines = 0
        if status == "ok" and m.name in hapi.LOCAL_TABLE_CACHE:
            nu = hapi.LOCAL_TABLE_CACHE[m.name]["data"]["nu"]
            nlines = len(nu)
            if nlines > 0:
                lo = float(min(nu))
                hi = float(max(nu))
                nu_min = lo if nu_min is None else min(nu_min, lo)
                nu_max = hi if nu_max is None else max(nu_max, hi)
        molecules[m.name] = {"M": m.M, "I": m.I, "status": status, "nlines": int(nlines)}

    info = {
        "wavelength_range_um": [
            float(cfg.band.wavelength_min_um),
            float(cfg.band.wavelength_max_um),
        ],
        "wavenumber_range_cm-1": [float(cfg.wavenumber_min), float(cfg.wavenumber_max)],
        "actual_line_range_cm-1": [nu_min, nu_max],
        "molecules": molecules,
        "download_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = cache_dir / "band_info.json"
    path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")


def prepare_tables(
    cfg: Config,
    log: LogFn,
    cancel_event,
) -> List[str]:
    """准备谱线数据：复用缓存或下载，返回成功可用的分子名列表。"""
    cache_dir = get_cache_dir(cfg)
    cache_dir.mkdir(parents=True, exist_ok=True)
    hapi.db_begin(str(cache_dir))

    info = load_band_info(cache_dir)
    status_map: Dict[str, str] = {}
    need_fetch = []
    reuse_list = []

    wmin, wmax = cfg.wavenumber_min, cfg.wavenumber_max
    rng = (info or {}).get("wavenumber_range_cm-1")
    band_ok = (
        isinstance(rng, (list, tuple))
        and len(rng) == 2
        and rng[0] is not None
        and rng[1] is not None
        and rng[0] <= wmin
        and rng[1] >= wmax
    )

    if info is None:
        log("未找到缓存备注文件，将从头下载谱线数据")
        need_fetch = list(cfg.molecules)
    elif not band_ok:
        log("现有缓存波段未覆盖当前需求（%.2f–%.2f cm⁻¹），将重新下载"
            % (wmin, wmax))
        need_fetch = list(cfg.molecules)
    else:
        old_mols = info.get("molecules", {})
        for m in cfg.molecules:
            rec = old_mols.get(m.name, {})
            st = rec.get("status")
            if st is None:
                st = "ok" if rec.get("success") else "failed"
            if st == "ok":
                reuse_list.append(m)
            elif st == "no_data":
                status_map[m.name] = "no_data"
            else:
                need_fetch.append(m)
        log("缓存覆盖当前需求：复用 %d 个，无数据跳过 %d 个，待下载 %d 个"
            % (len(reuse_list), len(status_map), len(need_fetch)))

    if reuse_list:
        log("正在载入缓存谱线数据...")
        for m in reuse_list:
            if cancel_event.is_set():
                raise CancelledError("用户取消")
            try:
                hapi.storage2cache(m.name)
                n = len(hapi.LOCAL_TABLE_CACHE[m.name]["data"]["nu"])
                status_map[m.name] = "ok"
                log("  ✓ %s 缓存载入成功（%d 条）" % (m.name, n))
            except Exception as exc:
                log("  ✗ %s 缓存载入失败，转重新下载: %s" % (m.name, exc))
                need_fetch.append(m)

    if need_fetch:
        log("开始下载 HITRAN 逐线数据...")
        for m in need_fetch:
            if cancel_event.is_set():
                raise CancelledError("用户取消")
            try:
                _download_table(cfg, m, wmin, wmax, log, cancel_event)
                status_map[m.name] = "ok"
            except NoSpectralDataError as exc:
                status_map[m.name] = "no_data"
                log("  ○ %s 该波段无谱线数据（非故障）: %s" % (m.name, exc))
            except CancelledError:
                raise
            except Exception as exc:
                status_map[m.name] = "failed"
                log("  ✗ %s 下载失败: %s" % (m.name, exc))

    no_data_names = [n for n, s in status_map.items() if s == "no_data"]
    if no_data_names:
        log("说明：%s 在 %.1f–%.1f cm⁻¹ 内 HITRAN 不提供逐线数据，已自动跳过。"
            % (", ".join(no_data_names), wmin, wmax))

    if info is None or need_fetch:
        save_band_info(cfg, cache_dir, status_map, info)

    downloaded = [m.name for m in cfg.molecules if status_map.get(m.name) == "ok"]
    failed = [m.name for m in cfg.molecules if status_map.get(m.name) == "failed"]
    if failed:
        log("⚠ 本次下载失败（下次运行会自动重试）: %s" % ", ".join(failed))
    return downloaded
