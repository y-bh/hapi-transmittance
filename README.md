# HAPI 大气透过率仿真

基于 HITRAN 数据库与 HAPI 库，计算指定波段的大气透过率曲线。提供图形界面、外部配置文件和运行日志，支持打包成免安装 exe，并可通过 GitHub Actions 自动构建与发布 Release。

## 功能

- 从 HITRAN 下载逐线光谱数据并本地缓存，自动复用。
- 计算高分辨率 Voigt 吸收系数、透过率谱，并按仪器分辨率做狭缝卷积。
- 结果输出到指定目录：PNG 图、CSV、TXT。
- 运行日志记录每个阶段与完整堆栈，出错便于排查。
- 配置全部外置到 `config.json`，GUI 可直接编辑。
- 程序目录无写权限时自动回退到 `%LOCALAPPDATA%\hapi-transmittance`。
- 推送 `v*` 标签即可自动打包并发布 Release（见下文）。

## 目录结构

```text
app.py                 命令行入口
app_gui.py             GUI 入口
core/                  配置、日志、下载、仿真、输出、流水线
hapi.py                HITRAN 官方 HAPI 库（第三方）
legacy/                旧版单文件脚本（参考）
config.example.json    配置模板
build.spec             PyInstaller 打包配置
.github/workflows/     Release 自动发布
```

## 快速开始（源码运行）

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app_gui.py            # 图形界面
python app.py                # 命令行
```

首次运行会在程序根目录自动生成 `config.json`。命令行可指定配置与输出目录：

```powershell
python app.py --config config.json --output D:/results
python app.py --mode pathscan          # 只计算光程-透过率矩阵
```

## GUI 使用

界面采用「左侧导航 + 右侧功能页」布局：

- 波长-透过率：波长-透过率光谱计算，编辑仿真参数、成分混合比、分子清单、网络与 API Key，开始 / 取消计算。
- 光程-透过率：独立计算全波段 T(λ, L)，编辑激光线宽、波长采样点数与距离范围，开始 / 取消计算。
- 输出目录：设置输出目录（浏览 / 检查可写 / 打开目录），查看最近运行结果。
- 日志预览：实时查看日志，按 INFO / WARNING / ERROR 过滤，支持清空与打开日志文件。
- 关于：版本信息与引用，打开程序目录或配置文件。

计算在后台线程执行，界面不会卡顿；“取消”可中断进行中的任务。状态栏常驻显示
“就绪 / 运行中 / 完成”与最近一次输出目录。

## 配置说明（config.json）

```json
{
  "band": { "wavelength_min_um": 8.9, "wavelength_max_um": 11.0 },
  "simulation": {
    "wavenumber_step": 0.01,
    "instrument_resolution": 0.5,
    "path_length_cm": 1000000.0,
    "temperature_k": 296.0,
    "pressure_atm": 1.0,
    "wavenumber_wing": 25.0,
    "tau_line_min": 0.0001,
    "slit_function": "gaussian"
  },
  "molecules": [
    { "name": "H2O", "M": 1, "I": 1 },
    { "name": "CO2", "M": 2, "I": 1 },
    { "name": "O3", "M": 3, "I": 1 },
    { "name": "N2O", "M": 4, "I": 1 },
    { "name": "CH4", "M": 6, "I": 1 }
  ],
  "components": {
    "H2O": 0.01,
    "CO2": 0.0004,
    "O3": 0.000001,
    "N2O": 0.0000003,
    "CH4": 0.0000018
  },
  "diluent": { "air": 1.0 },
  "network": {
    "use_proxy": false,
    "proxy": "http://127.0.0.1:7890",
    "api_key": "",
    "timeout_s": 120.0,
    "max_retries": 3
  },
  "paths": { "cache_dir": "hitran_data_cache", "output_dir": "output" },
  "output": {
    "timestamped_subdir": true,
    "save_png": true,
    "save_csv": true,
    "save_txt": true,
    "png_dpi": 300
  }
}
```

- `band`：波长范围（μm）。
- `simulation`：计算步长、仪器分辨率、光程、温压、谱线翼截断、最小光学厚度、狭缝函数（gaussian / rectangular / triangular / michelson / dispersion / cosinus / diffraction）。
- `molecules`：分子清单（name, M, I），M 为 HITRAN 分子号，I 为同位素号。
- `components`：各气体体积混合比。
- `diluent`：展宽气体，保持 `air: 1.0`，勿改成 N2/O2/Ar 组合。
- `network`：代理开关、代理地址、API Key、超时与重试。API Key 也可用环境变量 `HITRAN_API_KEY` 提供；不填会以游客模式访问（每日限额较低）。
- `paths`：谱线缓存目录与输出目录，相对路径以程序根目录为基准。
- `output`：是否按时间戳分目录、是否保存 PNG/CSV/TXT、PNG 分辨率。
- `path_scan`：全波段光程-透过率计算参数（见下）。

`path_scan` 示例：

```json
{
  "laser_linewidth_cm-1": 0.01,
  "linewidth_shape": "gaussian",
  "wavelength_points": 200,
  "distance_min_cm": 0.0,
  "distance_max_cm": 1000000.0,
  "distance_points": 200,
  "distance_scale": "linear"
}
```

“运行”页和“光程曲线”页相互独立：前者计算波长-透过率光谱，后者对每个波长用高斯线宽加权，
按 T(L)=∫g(ν)·exp(-k(ν)·L)dν 计算不同距离的透过率，结果分别输出到 `spectrum/` 与 `path_scan/` 子目录。

## 输出文件

每次运行在输出目录（或时间戳子目录）生成：

| 文件 | 内容 |
| --- | --- |
| `atmospheric_transmittance.png` | 三面板结果图 |
| `transmittance_final.csv` | 卷积后透过率（wavelength_um, wavenumber_cm-1, transmittance） |
| `absorption_coefficient.txt` | 高分辨吸收系数（cm²/molecule） |
| `transmittance_highres.txt` | 高分辨透过率 |
| `hitran_data_cache/band_info.json` | 谱线缓存备注 |
| `transmittance_vs_distance.csv` | 光程-透过率矩阵（行=距离，列=波长） |
| `transmittance_vs_distance.png` | T(λ, L) 热图 |

光谱结果写入 `output/spectrum/`，光程-透过率结果写入 `output/path_scan/`。

## 日志与排错

- GUI 日志：exe 旁的 `logs/run_*.log`；命令行日志：输出目录的 `logs/`。
- 常见问题：

| 现象 | 处理 |
| --- | --- |
| 没有成功加载任何光谱数据 | 检查网络 / 代理 / API Key |
| HTTP 403 | 超过当日查询限额，次日重试或换 Key |
| 某分子“该波段无谱线数据” | 正常（如 O₃ 逐线谱只到约 6997 cm⁻¹） |
| 仪器卷积结果为空 | 波段相对分辨率/翼宽太小，增大波段或减小分辨率 |
| 透过率异常偏高 | 检查 `diluent` 是否用了 `air` |

## 打包为 exe（--onedir）

```powershell
.venv\Scripts\Activate.ps1
pyinstaller build.spec --noconfirm --clean
```

产物在 `dist/hapi_transmittance/`，其中 `hapi_transmittance.exe` 为主程序，`_internal/` 为捆绑运行时，二者需一起分发。排查问题时可在 `build.spec` 中把 `console` 改为 `True` 重新打包。程序目录无写权限时会自动回退到 `%LOCALAPPDATA%\hapi-transmittance`。

## 自动发布 Release（GitHub Actions）

仓库已配置 `.github/workflows/release.yml`，在推送 `v*` 标签时自动完成：打包 exe → 压 zip → 创建 Release 并上传 zip。

用法：

1. 确认代码已提交到 `master`。
2. 打标签：

```powershell
git tag v1.0.0
```

3. 推送标签：

```powershell
git push origin v1.0.0
```

4. 到仓库 “Actions” 页查看构建进度，完成后在 “Releases” 页即可看到 `hapi_transmittance_win64_v1.0.0.zip`。

如果不想用自动发布，也可以手动：仓库页面 → Releases → Draft a new release → 上传 zip 附件，或用 `gh release create v1.0.0 dist/hapi_transmittance_win64.zip`。

> 注意：`dist/`、`*.zip`、`*.exe` 已被 `.gitignore` 忽略，不会提交进仓库；分发一律走 Release。

## 引用

R.V. Kochanov, I.E. Gordon, L.S. Rothman, P. Wcislo, C. Hill, J.S. Wilzewski, HITRAN Application Programming Interface (HAPI): A comprehensive approach to working with spectroscopic data, J. Quant. Spectrosc. Radiat. Transfer 177, 15-30 (2016). DOI: 10.1016/j.jqsrt.2016.03.005
