#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于 HAPI 计算指定波段的大气透过率曲线。
只需修改下方「用户输入区」中的波长范围，即可计算任意波段。
程序会自动复用已下载的谱线数据（按分辨率与波段包含关系判断），避免重复下载。
兼容 HAPI v1.3.0.0（Python 2/3 通用）。
"""

import sys
import os
import json
from datetime import datetime
import numpy as np
import matplotlib.pyplot as plt

# 在 GBK 等非 UTF-8 控制台/重定向环境下，避免输出 cm⁻¹ 等字符时抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ============================================================
# 用户输入区（改这里即可计算不同波段）
# ============================================================
WAVELENGTH_RANGE = (8.900, 11.000)  # 波长范围（μm）

# ---- 代理开关 ----
# True  = 启用下方 PROXY 中配置的代理（如 Clash / V2RayN / Shadowsocks）
# False = 不使用代理，直连网络
USE_PROXY = False

# ---- 仿真参数 ----
WAVENUMBER_STEP = 0.01          # 高分辨率计算步长 (cm⁻¹)。0.1 会使谱线峰值失真 2%~8%，0.02 已收敛
INSTRUMENT_RESOLUTION = 0.5     # 仪器分辨率 (cm⁻¹)
PATH_LENGTH = 1000000.0         # 光程 (cm)
ENV = {'T': 296.0, 'p': 1.0}    # 标准温压（T: K，p: atm）

# 谱线翼截断 (cm⁻¹)。Diluent 修正后洛伦兹翼才真正生效，25 cm⁻¹ 可覆盖远翼贡献
WAVENUMBER_WING = 25.0

# 单条谱线允许的最小积分光学厚度 τ_line = S·N·L。
# IntensityThreshold 由它按光程/温压自动反推，避免长光程下仍不可忽略的弱线被静默剔除。
TAU_LINE_MIN = 1e-4

# 辐射活性成分（体积混合比）
COMPONENTS = {
    'H2O': 0.01,      # 1% 水汽
    'CO2': 0.0004,    # 400 ppm
    'O3': 1e-6,       # 1 ppm
    'N2O': 3e-7,      # 0.3 ppm
    'CH4': 1.8e-6,    # 1.8 ppm
}

# 展宽气体（Diluent）—— 关键修正点
# HITRAN 标准 .par 谱线文件只提供 gamma_air / gamma_self 两列，
# 没有 gamma_N2 / gamma_O2 / gamma_Ar。若写成 {'N2':..,'O2':..,'Ar':..}，
# HAPI 查不到对应列 → 洛伦兹半宽 Gamma0 = 0 → 线型退化为纯 Doppler
# （HWHM≈0.014 cm⁻¹，比真实值小近一个量级），吸收被严重低估、透过率虚高。
# 而 gamma_air 本身就是按干燥空气（N2/O2/Ar 混合）实测标定的，
# 因此用 'air' 即等价于原先的 N2/O2/Ar 组合。
# 若需额外计入水汽自展宽，可改为 {'air': 0.99, 'self': 0.01}
DILUENT = {
    'air': 1.0,
}

# 分子下载清单（名称, HITRAN分子号, 同位素号），可手工编辑，适用于所有波段
molecule_list = [
    ('H2O', 1, 1),   # 主同位素 H₂¹⁶O
    ('CO2', 2, 1),   # ¹²C¹⁶O₂
    ('O3', 3, 1),    # ¹⁶O₃：HITRAN 逐线数据上限约 6997 cm⁻¹(≈1.43 μm)，更短波长无谱线属正常
    ('N2O', 4, 1),   # ¹⁴N₂¹⁶O
    ('CH4', 6, 1),   # ¹²CH₄
]

# 谱线数据缓存文件夹与备注文件
CACHE_DIR = 'hitran_data_cache'
INFO_FILE = os.path.join(CACHE_DIR, 'band_info.json')

# 由波长范围换算波数：ν̃ (cm⁻¹) = 10⁴ / λ (μm)
WAVENUMBER_MIN = 1e4 / WAVELENGTH_RANGE[1]
WAVENUMBER_MAX = 1e4 / WAVELENGTH_RANGE[0]

# ============================================================
# 第 1 步：导入 HAPI（请确保 hapi.py 与本脚本在同一目录）
# ============================================================
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

from hapi import *
import hapi

_orig_queryHITRAN = hapi.queryHITRAN


class NoSpectralDataError(Exception):
    """HITRAN 返回 HTTP 404：该同位素在所请求波数区间内没有任何谱线。
    这是数据库的权威答复，不是网络/权限故障，重试也不会改变结果。"""
    pass


def _fast_queryHITRAN(TableName, iso_id_list, numin, numax, pargroups=[], params=[], dotpar=True, head=False):
    ParameterList = hapi.prepareParlist(pargroups=pargroups, params=params, dotpar=dotpar)
    TableHeader = hapi.prepareHeader(ParameterList)
    TableHeader['table_name'] = TableName
    iso_id_list_str = ','.join([str(x) for x in iso_id_list])
    DataFileName = hapi.VARIABLES['BACKEND_DATABASE_NAME'] + '/' + TableName + '.data'
    HeaderFileName = hapi.VARIABLES['BACKEND_DATABASE_NAME'] + '/' + TableName + '.header'
    if pargroups or params:
        url = hapi.VARIABLES['GLOBAL_HOST'] + '/lbl/api?' + \
            'iso_ids_list=' + iso_id_list_str + '&' + \
            'numin=' + str(numin) + '&' + \
            'numax=' + str(numax) + '&' + \
            'head=' + str(head) + '&' + \
            'fixwidth=0&sep=[comma]&' + \
            'request_params=' + ','.join(ParameterList)
    else:
        url = hapi.VARIABLES['GLOBAL_HOST'] + '/lbl/api?' + \
            'iso_ids_list=' + iso_id_list_str + '&' + \
            'numin=' + str(numin) + '&' + \
            'numax=' + str(numax)
    if hapi.VARIABLES['DISPLAY_FETCH_URL']: print(url+'\n')
    try:
        if hapi.VARIABLES['PROXY']:
            print('Using proxy '+str(hapi.VARIABLES['PROXY']))
            proxy = hapi.urllib2.ProxyHandler(hapi.VARIABLES['PROXY'])
            opener = hapi.urllib2.build_opener(proxy)
            hapi.urllib2.install_opener(opener)
        req = hapi.urllib2.urlopen(url, timeout=120)
    except hapi.urllib2.HTTPError as err:
        if err.code == 403:
            raise Exception('You have exceeded the daily limit of API queries.')
        elif err.code == 404:
            # 该波数区间无谱线（例如 O3 在 >7000 cm⁻¹ 区间），与“下载失败”区分开
            raise NoSpectralDataError(
                'HTTP 404 — HITRAN 在 %.1f–%.1f cm⁻¹ 内没有该同位素的谱线数据'
                % (numin, numax))
        else:
            raise Exception('Failed to retrieve data for given parameters (HTTP %s).' % err.code)
    except hapi.urllib2.URLError:
        raise Exception('Cannot connect. Try again or edit GLOBAL_HOST variable.')
    CHUNK = 1024 * 1024  # 1MB (原版64KB，提速16倍+)
    total_bytes = 0
    chunk_count = 0
    print('BEGIN DOWNLOAD: '+TableName+' (chunk=1MB)')
    with hapi.open_(DataFileName,'w') as fp:
        while True:
            chunk = req.read(CHUNK)
            if not chunk: break
            fp.write(chunk.decode('utf-8'))
            total_bytes += len(chunk)
            chunk_count += 1
            print('  ... %d MB downloaded' % chunk_count)
        print('  TOTAL: %d bytes (%.1f MB) written' % (total_bytes, total_bytes/1048576.0))
    with open(HeaderFileName,'w') as fp:
        fp.write(json.dumps(TableHeader,indent=2))
        print('Header written to %s' % HeaderFileName)
    print('END DOWNLOAD')
    hapi.storage2cache(TableName)
    print('PROCESSED')
hapi.queryHITRAN = _fast_queryHITRAN

# 根据你的代理软件修改端口号：
#   Clash Verge / Clash for Windows 默认: 7890
#   V2RayN / V2Ray 默认: 10809
#   Shadowsocks 默认: 1080
_PROXY_CONFIG = {
    'http':  'http://127.0.0.1:7890',
    'https': 'http://127.0.0.1:7890'
}
# 由开头的 USE_PROXY 开关决定是否真正启用代理（{} 中内容始终保留）
hapi.VARIABLES['PROXY'] = _PROXY_CONFIG if USE_PROXY else {}
print(">>> 代理已启用:" if USE_PROXY else ">>> 代理已关闭（直连网络）:", hapi.VARIABLES['PROXY'])

# ============================================================
# 第 2 步：输入 HITRAN API Key（核心修改点）
# ============================================================
# 如果你从 HITRAN 官网获得了 API Key，请取消下一行的注释并填入。
# 若不需要 API Key（早期版本或本地数据），保持注释即可。
# ------------------------------------------------------------------
API_KEY = ""  # <--- 在这里填入你的 HITRAN API Key（原硬编码 Key 已从仓库移除）
# ------------------------------------------------------------------

# -----------------------------------------------------------
# 为 HAPI 注入 API Key（Monkey-Patch urllib2.urlopen）
# 原理：拦截 HAPI 内部的 HTTP 请求，在 URL 中自动追加 key 参数
# -----------------------------------------------------------
if 'API_KEY' in dir() and API_KEY and API_KEY != "YOUR_API_KEY_HERE":
    try:
        import urllib.request as _urllib2_mod  # Python 3
    except ImportError:
        import urllib2 as _urllib2_mod  # Python 2

    _original_urlopen = _urllib2_mod.urlopen

    def _urlopen_with_key(url, *args, **kwargs):
        # 仅对 HITRAN API 请求注入 Key，且避免重复追加
        if isinstance(url, str) and 'hitran.org' in url and 'key=' not in url:
            separator = '&' if ('?' in url and not url.endswith('&')) else '?'
            if '?' in url and not url.endswith('&') and not url.endswith('?'):
                separator = '&'
            elif '?' not in url:
                separator = '?'
            else:
                separator = '&'
            url = url + separator + 'key=' + API_KEY
            print(f"  [Injected API Key] URL suffix: ...&key={API_KEY[:4]}****")
        return _original_urlopen(url, *args, **kwargs)


    _urllib2_mod.urlopen = _urlopen_with_key
    print(">>> API Key 已注入 HAPI HTTP 模块\n")
else:
    print(">>> 未配置 API Key（若下载失败，请在代码中设置 API_KEY 变量）\n")

# ============================================================
# 第 3 步：初始化本地数据库并打印仿真参数
# ============================================================
db_begin(CACHE_DIR)

print("=" * 60)
print(f"仿真波长范围: {WAVELENGTH_RANGE[0]} - {WAVELENGTH_RANGE[1]} μm")
print(f"对应波数范围: {WAVENUMBER_MIN:.2f} - {WAVENUMBER_MAX:.2f} cm⁻¹")
print(f"计算步长: {WAVENUMBER_STEP} cm⁻¹")
print(f"仪器分辨率: {INSTRUMENT_RESOLUTION} cm⁻¹")
print(f"光程长度: {PATH_LENGTH / 100:.1f} m")
print("=" * 60)

# ============================================================
# 第 4 步：检查缓存，决定是否复用或重新下载
# ============================================================
def load_band_info():
    """读取缓存备注文件；不存在或损坏时返回 None。"""
    try:
        with open(INFO_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def write_band_info(status_map, old_info=None):
    """写入缓存备注文件：记录设计波段、实际谱线范围、各分子下载状态。

    status_map: {分子名: 'ok' | 'no_data' | 'failed'}
    old_info  : 旧的备注字典，用于沿用本次未涉及的分子状态
    """
    nu_min = None
    nu_max = None
    molecules = {}
    old_mols = (old_info or {}).get('molecules', {})
    for name, M, I in molecule_list:
        status = status_map.get(name)
        if status is None:
            rec = old_mols.get(name, {})
            status = rec.get('status')
            if status is None:            # 兼容只有 success 字段的旧版备注文件
                status = 'ok' if rec.get('success') else 'failed'
        nlines = 0
        if status == 'ok' and name in hapi.LOCAL_TABLE_CACHE:
            nu = hapi.LOCAL_TABLE_CACHE[name]['data']['nu']
            nlines = len(nu)
            if nlines > 0:
                lo = float(np.min(nu))
                hi = float(np.max(nu))
                nu_min = lo if nu_min is None else min(nu_min, lo)
                nu_max = hi if nu_max is None else max(nu_max, hi)
        molecules[name] = {'M': M, 'I': I, 'status': status, 'nlines': int(nlines)}
    info = {
        'wavelength_range_um': [float(WAVELENGTH_RANGE[0]), float(WAVELENGTH_RANGE[1])],
        # 设计波段 = 本次请求下载的波数范围（缓存复用判定以此为准）
        'wavenumber_range_cm-1': [float(WAVENUMBER_MIN), float(WAVENUMBER_MAX)],
        # 实际下载到的谱线波数范围（仅供参考）
        'actual_line_range_cm-1': [nu_min, nu_max],
        'molecules': molecules,
        'download_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(INFO_FILE, 'w', encoding='utf-8') as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    print(f"\n缓存备注文件已写入: {INFO_FILE}")
    print(f"  设计波段: {WAVENUMBER_MIN:.2f} - {WAVENUMBER_MAX:.2f} cm⁻¹")


info = load_band_info()
status_map = {}      # 本次运行确定的各分子状态
need_fetch = []      # 需向服务器请求的分子
reuse_list = []      # 可直接从本地缓存载入的分子

rng = (info or {}).get('wavenumber_range_cm-1')
band_ok = (isinstance(rng, (list, tuple)) and len(rng) == 2
           and rng[0] is not None and rng[1] is not None
           and rng[0] <= WAVENUMBER_MIN and rng[1] >= WAVENUMBER_MAX)

if info is None:
    print("\n>>> 未找到缓存备注文件，将从头下载谱线数据")
    need_fetch = list(molecule_list)
elif not band_ok:
    print("\n>>> 现有缓存波段未覆盖当前需求，将重新下载")
    need_fetch = list(molecule_list)
else:
    # 逐谱线数据只取决于波数范围（与分辨率、计算步长无关），故只校验波段覆盖
    old_mols = info.get('molecules', {})
    for name, M, I in molecule_list:
        rec = old_mols.get(name, {})
        st = rec.get('status')
        if st is None:                  # 兼容旧版备注文件
            st = 'ok' if rec.get('success') else 'failed'
        if st == 'ok':
            reuse_list.append((name, M, I))
        elif st == 'no_data':
            status_map[name] = 'no_data'   # 服务器已确认无谱线，重试无意义
        else:
            need_fetch.append((name, M, I))  # 上次失败的分子本次补下
    print("\n>>> 缓存波段覆盖当前需求：复用 %d 个，无数据跳过 %d 个，待补下 %d 个"
          % (len(reuse_list), len(status_map), len(need_fetch)))

# ============================================================
# 第 5 步：获取谱线数据（复用缓存或下载）
# ============================================================
if reuse_list:
    print("正在载入缓存谱线数据...")
    for name, M, I in reuse_list:
        try:
            hapi.storage2cache(name)
            n = len(hapi.LOCAL_TABLE_CACHE[name]['data']['nu'])
            status_map[name] = 'ok'
            print(f"  ✓ {name} (M={M}, I={I}) 缓存载入成功（{n} 条）")
        except Exception as e:
            print(f"  ✗ {name} 缓存载入失败，转为重新下载: {e}")
            need_fetch.append((name, M, I))

if need_fetch:
    print("\n开始下载 HITRAN 逐线数据...")
    for name, M, I in need_fetch:
        try:
            fetch(name, M, I, WAVENUMBER_MIN, WAVENUMBER_MAX)
            status_map[name] = 'ok'
            print(f"  ✓ {name} (M={M}, I={I}) 下载并缓存成功")
        except NoSpectralDataError as e:
            status_map[name] = 'no_data'
            print(f"  ○ {name} (M={M}, I={I}) 该波段无谱线数据（非故障）: {e}")
        except Exception as e:
            status_map[name] = 'failed'
            print(f"  ✗ {name} (M={M}, I={I}) 下载失败: {e}")

# 对“无数据”的分子给出物理层面的解释，而不是让用户误以为下载出错
no_data_names = [n for n, s in status_map.items() if s == 'no_data']
if no_data_names:
    print("\n说明：%s 在 %.1f–%.1f cm⁻¹ 内 HITRAN 不提供逐线数据，已自动跳过。"
          % (', '.join(no_data_names), WAVENUMBER_MIN, WAVENUMBER_MAX))
    if 'O3' in no_data_names:
        print("      臭氧逐线谱在 HITRAN 中仅覆盖到约 6997 cm⁻¹（≈1.43 μm）；")
        print("      其可见/近紫外的 Chappuis、Huggins、Hartley 带属连续吸收，")
        print("      需另行使用吸收截面（cross-section）数据，逐线计算无法体现。")

downloaded_tables = [name for name, M, I in molecule_list
                     if status_map.get(name) == 'ok']
failed_names = [n for n, s in status_map.items() if s == 'failed']

# 只要发生过下载（或备注文件缺失）就刷新备注，以便下次运行跳过/补下
if info is None or need_fetch:
    write_band_info(status_map, info)

if not downloaded_tables:
    raise RuntimeError("没有成功下载/载入任何光谱数据！请检查网络连接或 API Key 设置。")

print(f"\n成功加载的数据表: {downloaded_tables}")
if failed_names:
    print(f"⚠ 本次下载失败（下次运行会自动重试）: {failed_names}")

# 构造 HAPI 要求的 Components 格式：[(分子号, 同位素号, 混合比), ...]
components_arg = [(M, I, COMPONENTS[name])
                  for name, M, I in molecule_list
                  if name in downloaded_tables and name in COMPONENTS]

# ============================================================
# 第 6 步：计算吸收系数（高分辨率，仅计算一次）
# ============================================================
# 分子数密度（molecules/cm³）：既用于换算 HITRAN 单位，也用于反推强度阈值
num_density = volumeConcentration(ENV['p'], ENV['T'])

# IntensityThreshold 比较的是“按用户混合比缩放后的谱线强度 S”，
# 而单线的积分光学厚度 τ_line = S·N·L。给定可忽略的最小 τ 即可反推阈值。
# 阈值过松（如固定 1e-25）对 ppm 级气体等效于真实门限 S>1e-19，
# 会把 CO2/N2O/CH4 的全部谱线以及大部分弱 H2O 线剔除，导致透过率虚高。
INTENSITY_THRESHOLD = TAU_LINE_MIN / (num_density * PATH_LENGTH)

print("\n正在计算吸收系数（高分辨率）...")
print(f"  分子数密度 N = {num_density:.4e} cm⁻³")
print(f"  强度阈值 IntensityThreshold = {INTENSITY_THRESHOLD:.3e}"
      f"   （由 τ_line_min = {TAU_LINE_MIN:.0e} 自动反推）")

# 报告各分子实际参与计算的谱线数，避免弱线被静默丢弃而不自知
print("  各分子谱线参与情况（波段内 / 过阈值后）：")
for name, M, I in molecule_list:
    if name not in downloaded_tables:
        continue
    _nu = np.array(hapi.LOCAL_TABLE_CACHE[name]['data']['nu'])
    # HAPI 中谱线强度列名为 'sw'（不是 HITRAN .par 里的符号 S），写错会抛 KeyError: 's'
    _sw = np.array(hapi.LOCAL_TABLE_CACHE[name]['data']['sw'])
    _in = (_nu >= WAVENUMBER_MIN) & (_nu <= WAVENUMBER_MAX)
    # sw 已含自然丰度，需按 混合比/自然丰度 缩放后才与阈值比较（T = Tref = 296 K 时温度校正≈1）
    _keep = _in & (_sw * (COMPONENTS[name] / hapi.abundance(M, I)) >= INTENSITY_THRESHOLD)
    print(f"    {name:4s}: {_in.sum():6d} / {_keep.sum():6d}"
          f"    （剔除 {_in.sum() - _keep.sum():d} 条弱线）")

nu, coef = absorptionCoefficient_Voigt(
    SourceTables=downloaded_tables,
    Components=components_arg,
    Environment=ENV,
    WavenumberRange=(WAVENUMBER_MIN, WAVENUMBER_MAX),
    WavenumberStep=WAVENUMBER_STEP,
    WavenumberWing=WAVENUMBER_WING,
    WavenumberWingHW=0,               # 翼宽完全由 WAVENUMBER_WING 控制
    IntensityThreshold=INTENSITY_THRESHOLD,
    Diluent=DILUENT,
    HITRAN_units=False,  # 单位: cm⁻¹，transmittanceSpectrum 需要此单位
)
print(f"  高分辨率网格点数: {len(nu)}")

# 换算出 cm²/molecule（HITRAN 单位）并写文件：吸收系数除以分子数密度即可
coef_hitran_units = coef / num_density
np.savetxt('absorption_coefficient.txt',
           np.column_stack((nu, coef_hitran_units)),
           delimiter=' ',
           header='wavenumber_cm-1 absorption_coefficient_cm2_per_molecule',
           comments='# ')
print("  吸收系数（cm²/molecule）已写入 absorption_coefficient.txt（由 cm⁻¹ 换算）")

# ============================================================
# 第 7 步：计算透过率光谱
# ============================================================
print("正在计算透过率光谱...")
nu_trans, trans = transmittanceSpectrum(
    nu, coef,
    Environment={'l': PATH_LENGTH},
    File='transmittance_highres.txt'
)

# ============================================================
# 第 8 步：仪器分辨率卷积（模拟指定分辨率）
# ============================================================
print(f"正在进行仪器卷积（Resolution = {INSTRUMENT_RESOLUTION} cm⁻¹）...")
nu_conv, trans_conv, i1, i2, slit = convolveSpectrum(
    nu_trans, trans,
    Resolution=INSTRUMENT_RESOLUTION,
    AF_wing=10.0,
    SlitFunction=SLIT_GAUSSIAN  # 可选: SLIT_RECTANGULAR, SLIT_TRIANGULAR, SLIT_MICHELSON
)

# ============================================================
# 第 9 步：结果可视化与保存
# ============================================================
wavelength = 1e4 / nu_conv      # 卷积后波数 -> 波长 (μm)
wavelength_hr = 1e4 / nu        # 高分辨率波数 -> 波长 (μm)

# 结果摘要：分段平均透过率，便于快速核对是否存在异常偏高/偏低
print("\n" + "=" * 60)
print("透过率摘要（仪器卷积后）：")
print(f"  全波段：平均 {trans_conv.mean():.4f}   最小 {trans_conv.min():.4f}"
      f"   最大 {trans_conv.max():.4f}")
_edges = np.linspace(WAVELENGTH_RANGE[0], WAVELENGTH_RANGE[1], 21)
for _a, _b in zip(_edges[:-1], _edges[1:]):
    _m = (wavelength >= _a) & (wavelength < _b)
    if _m.any():
        print(f"    {_a:.3f}–{_b:.3f} μm : 平均 {trans_conv[_m].mean():.4f}"
              f"   最小 {trans_conv[_m].min():.4f}")
print("=" * 60)

fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

# --- 子图 1：高分辨率吸收系数 ---
ax1 = axes[0]
ax1.plot(wavelength_hr, coef, 'r-', linewidth=0.4, alpha=0.8)
ax1.set_ylabel('Absorption Coefficient (cm$^{-1}$)', fontsize=11)
ax1.set_title(f'Atmospheric Transmittance Simulation: {WAVELENGTH_RANGE[0]}–{WAVELENGTH_RANGE[1]} μm '
              f'({WAVENUMBER_MIN:.0f}–{WAVENUMBER_MAX:.0f} cm$^{{-1}}$)\n'
              f'T={ENV["T"]} K, p={ENV["p"]} atm, Path={PATH_LENGTH / 100:.0f} m', fontsize=13)
ax1.set_yscale('log')
ax1.grid(True, alpha=0.3)
ax1.set_xlim(WAVELENGTH_RANGE[0], WAVELENGTH_RANGE[1])

# --- 子图 2：高分辨率透过率 ---
ax2 = axes[1]
ax2.plot(wavelength_hr, trans, 'g-', linewidth=0.4, alpha=0.8)
ax2.set_ylabel('Transmittance (High-Res)', fontsize=11)
ax2.set_ylim(0, 1.05)
ax2.grid(True, alpha=0.3)

# --- 子图 3：仪器卷积后透过率 ---
ax3 = axes[2]
ax3.plot(wavelength, trans_conv, 'b-', linewidth=1.5,
         label=f'Convolved (Resolution={INSTRUMENT_RESOLUTION} cm$^{{-1}}$)')
ax3.set_xlabel('Wavelength (μm)', fontsize=12)
ax3.set_ylabel('Transmittance (Convolved)', fontsize=11)
ax3.set_ylim(0, 1.05)
ax3.grid(True, alpha=0.3)
ax3.legend(loc='lower left')

plt.tight_layout()
plt.savefig('atmospheric_transmittance.png', dpi=300)
plt.show()

# ============================================================
# 第 10 步：输出数据文件（CSV 格式，便于后续处理）
# ============================================================
output_csv = 'transmittance_final.csv'
np.savetxt(output_csv,
           np.column_stack((wavelength, 1e4 / wavelength, trans_conv)),
           delimiter=',',
           header='wavelength_um,wavenumber_cm-1,transmittance',
           comments='')

print("\n" + "=" * 60)
print("仿真完成！生成的文件：")
print("  1. atmospheric_transmittance.png   （结果图）")
print("  2. transmittance_final.csv         （透过率数据）")
print("  3. absorption_coefficient.txt      （高分辨吸收系数，cm²/molecule）")
print("  4. transmittance_highres.txt       （高分辨透过率）")
print("  5. hitran_data_cache/band_info.json（谱线数据备注文件）")
print("=" * 60)
